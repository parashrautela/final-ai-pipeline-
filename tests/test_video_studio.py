"""Offline storyboard contract and atomic PostgreSQL checks; zero inference/network calls."""
import asyncio
import atexit
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch
from uuid import uuid4

import fasteners
from fastapi import FastAPI
import httpx
import jwt
import pgserver
from pgserver.postgres_server import PostgresServer
from psycopg.types.json import Jsonb

from app.config import settings
from app.db import video_studio as db
from app.routers import video_studio as api
from app.services import video_studio as studio, video_storage
from test_video_pilot import LocalDatabase, photo

ROOT=Path(__file__).resolve().parents[1]
OWNER="10000000-0000-4000-8000-000000000001"
OTHER="10000000-0000-4000-8000-000000000002"
PRODUCT="20000000-0000-4000-8000-000000000001"
BASE="https://studio-test.supabase.co/storage/v1/object/public/plant-images/products/processed/"


class StudioDatabase(LocalDatabase):
    async def project(self,owner,project_id):
        rows=await asyncio.to_thread(self.execute,"SELECT * FROM video_studio_projects WHERE owner_id=%s AND id=%s",(owner,project_id))
        return self.serial(rows[0]) if rows else None

    async def request(self,owner,key):
        rows=await asyncio.to_thread(self.execute,"SELECT * FROM video_studio_requests WHERE owner_id=%s AND idempotency_key=%s",(owner,key))
        return self.serial(rows[0]) if rows else None

    async def history(self,owner,product_id):
        rows=await asyncio.to_thread(self.execute,"SELECT * FROM video_studio_projects WHERE owner_id=%s AND product_id=%s ORDER BY created_at DESC LIMIT 50",(owner,product_id))
        return [self.serial(row) for row in rows]


class StudioTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory(prefix="jewel-studio-tests-",dir="/private/tmp")
        cls.addClassCleanup(cls.directory.cleanup)
        runtime=Path(cls.directory.name)
        patcher=patch.multiple(PostgresServer,runtime_path=runtime,_lock=fasteners.InterProcessLock(runtime/".server-lock"))
        patcher.start()
        cls.addClassCleanup(patcher.stop)
        cls.server=pgserver.get_server(runtime/"data",cleanup_mode="stop")
        atexit.unregister(cls.server._cleanup)
        cls.addClassCleanup(cls.server.cleanup)
        cls.database=StudioDatabase(cls.server.get_uri())
        cls.database.execute("""
            CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
            GRANT USAGE ON SCHEMA public TO service_role;
            CREATE TABLE products(id uuid PRIMARY KEY,wholesaler_id uuid,title text,jewellery_type text,
                generated_image_urls jsonb,ai_processing_state text);
            GRANT ALL ON products TO service_role;
        """)
        migration=(ROOT/"migrations/038_video_studio.sql").read_text()
        cls.database.execute(migration)
        cls.database.execute(migration)

    async def asyncSetUp(self):
        self.database.execute("TRUNCATE video_studio_requests,video_studio_projects,products")
        self.database.execute("INSERT INTO products VALUES(%s,%s,'Five-view necklace','necklace',%s,'ready')",
                              (PRODUCT,OWNER,Jsonb([BASE+f"{index}.png" for index in range(5)])))
        self.assets={"products/processed/"+f"{index}.png":photo() for index in range(5)}
        storage=SimpleNamespace()
        def bucket(name):
            return SimpleNamespace(download=lambda path:self.assets[path],
                upload=lambda path,data,options:self.assets.__setitem__(path,data),
                create_signed_url=lambda path,ttl:{"signedURL":"https://studio-test.supabase.co/private/"+path})
        storage.from_=bucket
        storage.get_bucket=lambda name:SimpleNamespace(public=False)
        patches=[patch.multiple(settings,ENVIRONMENT="test",VIDEO_STUDIO_ENABLED=True,VIDEO_STUDIO_RENDER_MODE="mock",
            VIDEO_STUDIO_ALLOW_OPENAI=False,SUPABASE_URL="https://studio-test.supabase.co",
            SUPABASE_JWT_SECRET="offline-studio-tests-only-secret-value",CREDITS_ENABLED=False),
            patch.object(api.limiter,"enabled",False),patch.object(db,"rpc",self.database.rpc),
            patch.object(db,"project",self.database.project),patch.object(db,"request",self.database.request),
            patch.object(db,"history",self.database.history),patch.object(db,"product",self.database.product),
            patch.object(video_storage,"get_supabase",lambda:SimpleNamespace(storage=storage))]
        for patcher in patches:
            patcher.start()
            self.addCleanup(patcher.stop)
        app=FastAPI()
        app.include_router(api.router)
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://localhost")
        self.addAsyncCleanup(self.http.aclose)
        original_send=httpx.AsyncClient.send
        async def local_only(client,request,*args,**kwargs):
            if request.url.host!="localhost":
                raise AssertionError("External HTTP is forbidden in offline storyboard tests")
            return await original_send(client,request,*args,**kwargs)
        patcher=patch.object(httpx.AsyncClient,"send",local_only)
        patcher.start()
        self.addCleanup(patcher.stop)

    def headers(self,owner=OWNER,key=None):
        token=jwt.encode({"sub":owner,"aud":"authenticated","exp":datetime.now(timezone.utc)+timedelta(hours=1)},
                         settings.SUPABASE_JWT_SECRET,algorithm="HS256")
        return {"Authorization":"Bearer "+token,"Idempotency-Key":key or str(uuid4())}

    def config(self,**changes):
        return {"product_id":PRODUCT,"image_indexes":[0,4],"main_image_index":4,
                "mode":"product_led","duration":10,**changes}

    async def create(self,**changes):
        response=await self.http.post("/api/video-studio/projects",json=self.config(**changes),headers=self.headers())
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    async def action(self,project,action,*,key=None,expected=None):
        return await self.http.post(f"/api/video-studio/projects/{project['id']}/{action}",
            json={"expected_version":expected or project["version"]},headers=self.headers(key=key))

    async def update(self,project,**changes):
        config={key:project["config"][key] for key in ("product_id","image_indexes","main_image_index","mode","duration","human")}
        return await self.http.patch("/api/video-studio/projects/"+project["id"],
            json={**config,"expected_version":project["version"],**changes},headers=self.headers())

    async def test_auth_feature_environment_ownership_and_dynamic_sources(self):
        path=f"/api/video-studio/products/{PRODUCT}/options"
        self.assertEqual((await self.http.get(path)).status_code,401)
        for attribute,value in (("VIDEO_STUDIO_ENABLED",False),("ENVIRONMENT","production")):
            with patch.object(settings,attribute,value):
                self.assertEqual((await self.http.get(path,headers=self.headers())).status_code,403)
        self.assertEqual((await self.http.get(path,headers=self.headers(OTHER))).status_code,404)
        options=(await self.http.get(path,headers=self.headers())).json()
        self.assertEqual([item["index"] for item in options["images"]],[0,1,2,3,4])
        self.assertEqual(options["supported_video_durations"],[])
        project=await self.create()
        self.assertEqual([item["image_index"] for item in project["references"]],[4,0])
        self.assertEqual(project["references"][0]["role"],"main")
        self.assertNotIn("path",project["references"][0])
        self.assertEqual((await self.http.get("/api/video-studio/projects/"+project["id"],headers=self.headers(OTHER))).status_code,404)

    async def test_sources_main_category_and_human_fields_are_validated(self):
        for changes in ({"image_indexes":[0,1,2]}, {"image_indexes":[0,0]}, {"main_image_index":3},
                        {"image_indexes":[0,99]}, {"image_indexes":[True]}, {"duration":True},
                        {"mode":"human_led"}, {"category":"ring"},
                        {"human":{"ethnicity":"Indian","skin_tone":"medium"}}):
            response=await self.http.post("/api/video-studio/projects",json=self.config(**changes),headers=self.headers())
            self.assertEqual(response.status_code,422,(changes,response.text))
        self.database.execute("UPDATE products SET ai_processing_state='processing'")
        response=await self.http.post("/api/video-studio/projects",json=self.config(),headers=self.headers())
        self.assertEqual(response.status_code,422)
        self.database.execute("UPDATE products SET ai_processing_state='ready',jewellery_type='unknown',generated_image_urls=%s",
                              (Jsonb([BASE+"0.png","https://external.example/image.png"]),))
        human={"ethnicity":"user choice","skin_tone":"user choice","appearance":""}
        response=await self.http.post("/api/video-studio/projects",json=self.config(image_indexes=[0],main_image_index=0,mode="human_led",human=human),headers=self.headers())
        self.assertEqual(response.status_code,422)
        project=await self.create(image_indexes=[0],main_image_index=0,mode="human_led",human=human,category="ring")
        self.assertEqual(project["config"]["placement"],"on a finger")

    async def test_storyboard_approval_edits_invalidation_and_mock_video_rejection(self):
        project=await self.create()
        self.assertEqual((await self.action(project,"approve")).status_code,409)
        self.assertEqual((await self.action(project,"video")).status_code,409)
        rendered=await self.action(project,"storyboard")
        self.assertEqual(rendered.status_code,200,rendered.text)
        project=rendered.json()
        self.assertEqual(len(project["shots"]),9)
        self.assertEqual(project["shots"][-1]["end_seconds"],10)
        self.assertTrue(project["storyboard"]["is_mock"])
        self.assertFalse(project["approval_valid"])
        approved=(await self.action(project,"approve")).json()
        self.assertTrue(approved["approval_valid"])
        self.assertFalse(approved["live_video_approval_valid"])
        blocked=await self.action(approved,"video")
        self.assertEqual(blocked.json()["detail"]["error"],"MOCK_STORYBOARD_NOT_RENDERABLE")
        changed=await self.update(approved,duration=5)
        self.assertEqual(changed.status_code,200,changed.text)
        changed=changed.json()
        self.assertIsNone(changed["approval"])
        self.assertIsNone(changed["storyboard"])
        self.assertEqual(changed["shots"][-1]["end_seconds"],5)
        stale=await self.action(changed,"approve",expected=approved["version"])
        self.assertEqual(stale.status_code,409)
        changed=(await self.action(changed,"storyboard")).json()
        approved=(await self.action(changed,"approve")).json()
        plan=[{"panel":item["panel"],"direction":item["direction"]} for item in approved["shots"]]
        plan[0]["direction"]="New front hero framing."
        edited=(await self.update(approved,shots=plan)).json()
        self.assertIsNone(edited["storyboard"])
        self.assertFalse(edited["approval_valid"])

    async def test_human_wearing_dependencies_and_regeneration(self):
        human={"ethnicity":"South Asian","skin_tone":"deep brown","appearance":"dark tied-back hair"}
        project=await self.create(mode="human_led",human=human)
        self.assertEqual((await self.action(project,"storyboard")).status_code,409)
        project=(await self.action(project,"wearing")).json()
        self.assertTrue(project["wearing"]["is_mock"])
        project=(await self.action(project,"storyboard")).json()
        project=(await self.action(project,"approve")).json()
        wearing_id=project["wearing"]["id"]
        timed=(await self.update(project,duration=5)).json()
        self.assertEqual(timed["wearing"]["id"],wearing_id)
        changed=(await self.update(timed,human={**human,"skin_tone":"medium brown"})).json()
        self.assertIsNone(changed["wearing"])
        changed=(await self.action(changed,"wearing")).json()
        changed=(await self.action(changed,"storyboard")).json()
        changed=(await self.action(changed,"approve")).json()
        regenerated=(await self.action(changed,"wearing")).json()
        self.assertIsNone(regenerated["storyboard"])
        self.assertIsNone(regenerated["approval"])

    async def test_idempotency_recovers_without_render_after_version_advances(self):
        key=str(uuid4())
        payload=self.config()
        first=await self.http.post("/api/video-studio/projects",json=payload,headers=self.headers(key=key))
        replay=await self.http.post("/api/video-studio/projects",json=payload,headers=self.headers(key=key))
        self.assertEqual(first.json()["id"],replay.json()["id"])
        conflict=await self.http.post("/api/video-studio/projects",json={**payload,"duration":5},headers=self.headers(key=key))
        self.assertEqual(conflict.status_code,409)
        original=first.json()
        render_key=str(uuid4())
        with patch.object(studio,"render",wraps=studio.render) as renderer:
            project=(await self.action(original,"storyboard",key=render_key)).json()
            project=(await self.update(project,duration=5)).json()
            duplicate=await self.action(original,"storyboard",key=render_key)
            self.assertEqual(duplicate.status_code,200,duplicate.text)
            self.assertEqual(duplicate.json()["version"],project["version"])
            self.assertEqual(renderer.await_count,1)
        stale=await self.update(original,duration=5)
        self.assertEqual(stale.status_code,409)

    async def test_concurrent_render_is_reserved_once_and_edits_are_blocked(self):
        project=await self.create()
        started,release=asyncio.Event(),asyncio.Event()
        original=studio.render
        async def delayed(*args):
            started.set()
            await release.wait()
            return await original(*args)
        key=str(uuid4())
        with patch.object(studio,"render",side_effect=delayed) as renderer:
            running=asyncio.create_task(self.action(project,"storyboard",key=key))
            await started.wait()
            duplicate=await self.action(project,"storyboard",key=key)
            self.assertTrue(duplicate.json()["operation_in_progress"])
            self.assertEqual((await self.update(duplicate.json(),duration=5)).status_code,409)
            release.set()
            result=await running
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(renderer.await_count,1)

    async def test_render_failure_is_visible_unlocked_and_same_key_never_retries(self):
        project=await self.create()
        key=str(uuid4())
        with patch.object(studio,"render",side_effect=TimeoutError("uncertain paid response")) as renderer:
            failed=await self.action(project,"storyboard",key=key)
            self.assertEqual(failed.json()["operation_status"],"needs_review")
            self.assertFalse(failed.json()["operation_in_progress"])
            self.assertIsNone(failed.json()["storyboard"])
            duplicate=await self.action(project,"storyboard",key=key)
            self.assertEqual(duplicate.json()["operation_status"],"needs_review")
            self.assertEqual(renderer.await_count,1)
        retried=await self.action(failed.json(),"storyboard")
        self.assertIsNotNone(retried.json()["storyboard"])

    async def test_openai_gates_reference_order_and_no_automatic_retry(self):
        project=await self.create()
        with patch.object(settings,"VIDEO_STUDIO_RENDER_MODE","openai"):
            self.assertEqual((await self.action(project,"storyboard")).status_code,403)
        image=photo()
        from app.services.ai import OpenAIImageClient
        with patch.multiple(settings,VIDEO_STUDIO_RENDER_MODE="openai",VIDEO_STUDIO_ALLOW_OPENAI=True,OPENAI_API_KEY="offline-key"), \
             patch.object(OpenAIImageClient,"fuse_images",new_callable=AsyncMock,return_value=image) as render:
            rendered=await self.action(project,"storyboard")
            self.assertEqual(rendered.status_code,200,rendered.text)
            self.assertEqual(render.call_args.kwargs["max_retries"],0)
            self.assertIn("Image 1 is the MAIN",render.call_args.kwargs["prompt"])
            self.assertEqual(len(render.call_args.args[0]),2)
            stored=await self.database.project(OWNER,project["id"])
            self.assertEqual(render.call_args.args[0][0][0],self.assets[stored["data"]["references"][0]["path"]])
            approved=(await self.action(rendered.json(),"approve")).json()
            self.assertFalse(approved["storyboard"]["is_mock"])
            unavailable=await self.action(approved,"video")
            self.assertEqual(unavailable.status_code,503)
            self.assertEqual(unavailable.json()["detail"]["error"],"VIDEO_RENDERER_UNAVAILABLE")

    async def test_crashed_or_unconfirmed_finish_remains_pending_without_resubmission(self):
        project=await self.create()
        key=str(uuid4())
        original_rpc=self.database.rpc
        async def uncertain_finish(name,params):
            if name=="video_studio_finish":
                raise OSError("database response unavailable")
            return await original_rpc(name,params)
        with patch.object(db,"rpc",side_effect=uncertain_finish), patch.object(studio,"render",wraps=studio.render) as renderer:
            response=await self.action(project,"storyboard",key=key)
            self.assertEqual(response.status_code,503,response.text)
            pending=(await self.http.get("/api/video-studio/projects/"+project["id"],headers=self.headers())).json()
            self.assertEqual(pending["operation_status"],"pending")
            self.assertEqual(pending["active_operation_id"],key)
            self.assertIn("operator",pending["pending_recovery_notice"])
            duplicate=await self.action(project,"storyboard",key=key)
            self.assertTrue(duplicate.json()["operation_in_progress"])
            self.assertEqual((await self.update(pending,duration=5)).status_code,409)
            self.assertEqual(renderer.await_count,1)

    async def test_client_cannot_forge_approval_and_stored_hash_must_match(self):
        project=await self.create()
        forged=await self.http.post(f"/api/video-studio/projects/{project['id']}/approve",
            json={"expected_version":project["version"],"approval":{"version":project["version"]}},headers=self.headers())
        self.assertEqual(forged.status_code,422)
        rendered=(await self.action(project,"storyboard")).json()
        approved=(await self.action(rendered,"approve")).json()
        stored=await self.database.project(OWNER,project["id"])
        stored["data"]["approval"]["storyboard_sha256"]="forged"
        self.database.execute("UPDATE video_studio_projects SET data=%s WHERE id=%s",(Jsonb(stored["data"]),project["id"]))
        rejected=await self.action(approved,"video")
        self.assertEqual(rejected.json()["detail"]["error"],"STORYBOARD_APPROVAL_REQUIRED")

    async def test_catalogue_changes_do_not_mutate_private_reference_snapshots(self):
        project=await self.create()
        stored=await self.database.project(OWNER,project["id"])
        snapshots=[self.assets[item["path"]] for item in stored["data"]["references"]]
        self.assets["products/processed/4.png"]=photo((700,700))
        self.database.execute("UPDATE products SET generated_image_urls=%s",(Jsonb([BASE+"0.png"]),))
        from app.services.ai import OpenAIImageClient
        with patch.multiple(settings,VIDEO_STUDIO_RENDER_MODE="openai",VIDEO_STUDIO_ALLOW_OPENAI=True,OPENAI_API_KEY="offline-key"), \
             patch.object(OpenAIImageClient,"fuse_images",new_callable=AsyncMock,return_value=photo()) as render:
            result=await self.action(project,"storyboard")
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual([item[0] for item in render.call_args.args[0]],snapshots)

    async def test_mock_human_preview_cannot_enter_openai_storyboard_and_has_no_fallback(self):
        project=await self.create(mode="human_led",human={"ethnicity":"selected","skin_tone":"selected"})
        project=(await self.action(project,"wearing")).json()
        from app.services.ai import OpenAIImageClient
        with patch.multiple(settings,VIDEO_STUDIO_RENDER_MODE="openai",VIDEO_STUDIO_ALLOW_OPENAI=True,OPENAI_API_KEY="offline-key"), \
             patch.object(OpenAIImageClient,"fuse_images",new_callable=AsyncMock) as render:
            response=await self.action(project,"storyboard")
            self.assertEqual(response.status_code,409)
            render.assert_not_awaited()

    async def test_openai_client_zero_retry_override_sends_only_one_request(self):
        from app.services.ai import OpenAIImageClient
        calls=[]
        def transport(request):
            calls.append(request)
            return httpx.Response(503,json={"error":"offline simulated outage"})
        original_client=httpx.AsyncClient
        def local_client(*args,**kwargs):
            return original_client(*args,**kwargs,transport=httpx.MockTransport(transport))
        with patch.object(settings,"OPENAI_API_KEY","offline-key"), \
             patch.object(OpenAIImageClient,"_EDITS_URL","http://localhost/offline-openai"), \
             patch("app.services.ai.httpx.AsyncClient",side_effect=local_client):
            with self.assertRaises(httpx.HTTPStatusError):
                await OpenAIImageClient().fuse_images([(photo(),"image/png")],prompt="Offline reference test",max_retries=0)
        self.assertEqual(len(calls),1)

    async def test_snapshot_tampering_and_media_outage_never_regenerate(self):
        project=await self.create()
        stored=await self.database.project(OWNER,project["id"])
        self.assets[stored["data"]["references"][0]["path"]]=photo((600,600))
        failed=(await self.action(project,"storyboard")).json()
        self.assertEqual(failed["operation_status"],"needs_review")
        with patch.object(video_storage,"signed_url",side_effect=OSError("signing unavailable")):
            response=await self.http.get("/api/video-studio/projects/"+project["id"],headers=self.headers())
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.json()["media_unavailable"])

    async def test_database_atomic_versions_keys_and_permissions(self):
        project=await self.create()
        stored=await self.database.project(OWNER,project["id"])
        params={"p_id":project["id"],"p_owner":OWNER,"p_key":str(uuid4()),"p_hash":"test-edit",
                "p_expected":1,"p_action":"edit","p_product":PRODUCT,"p_data":stored["data"],"p_render":False}
        def change(index):
            return self.database.call("video_studio_begin",{**params,"p_key":str(uuid4()),"p_hash":str(index)})
        with ThreadPoolExecutor(max_workers=5) as pool:
            results=list(pool.map(change,range(5)))
        self.assertEqual(sum("project" in row for row in results),1)
        self.assertEqual(sum(row.get("error")=="stale_version" for row in results),4)
        params.update(p_expected=2,p_render=True,p_action="storyboard")
        with ThreadPoolExecutor(max_workers=5) as pool:
            replays=list(pool.map(lambda _:self.database.call("video_studio_begin",params),range(5)))
        self.assertEqual(sum(not row["duplicate"] for row in replays),1)
        self.assertEqual({row["project"]["version"] for row in replays},{3})
        wrong=self.database.call("video_studio_finish",{"p_id":project["id"],"p_owner":OTHER,"p_key":params["p_key"],"p_data":{}})
        self.assertIn("error",wrong)
        for role in ("anon","authenticated"):
            for table in ("video_studio_projects","video_studio_requests"):
                with self.assertRaises(Exception):
                    self.database.execute("SELECT * FROM "+table,role=role)
            with self.assertRaises(Exception):
                self.database.execute("SELECT video_studio_finish(%s,%s,%s,'{}'::jsonb)",
                                      (project["id"],OWNER,params["p_key"]),role=role)

    async def test_isolated_entrypoint_rejects_hosted_or_production_config(self):
        from app.video_studio_local import app
        with self.assertRaises(RuntimeError):
            async with app.router.lifespan_context(app):
                pass
        with patch.object(settings,"SUPABASE_URL","http://127.0.0.1:54321"):
            async with app.router.lifespan_context(app):
                pass
            with patch.object(settings,"ENVIRONMENT","production"):
                with self.assertRaises(RuntimeError):
                    async with app.router.lifespan_context(app):
                        pass
