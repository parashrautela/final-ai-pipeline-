"""Offline API/worker tests using real disposable PostgreSQL; no paid API calls.

Run: .venv/bin/python -m pytest tests/test_video_pilot.py -q
"""
from __future__ import annotations

import asyncio
import atexit
import base64
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from itertools import product
import hashlib
import io
import json
import re
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch
from uuid import UUID, uuid4

from fastapi import FastAPI
import httpx
import jwt
import pgserver
from pgserver.postgres_server import PostgresServer
import fasteners
from PIL import Image
import psycopg
from psycopg.types.json import Jsonb

from app.config import settings
from app.db import videos as db
from app.routers import videos as api
from app.services import video_prompts, video_storage, video_pipeline
from app.services.kling import KlingClient, SubmissionRejected, SubmissionUnknown, billing_units, units, PROMPT_LIMITS

ROOT = Path(__file__).resolve().parents[1]
OWNER = "10000000-0000-4000-8000-000000000001"
OTHER = "10000000-0000-4000-8000-000000000002"
PRODUCT = "20000000-0000-4000-8000-000000000001"
URL = "https://video-test.supabase.co/storage/v1/object/public/plant-images/products/processed/test_v1.png"
EXPIRES = (datetime.now(timezone.utc)+timedelta(days=20)).isoformat()
MP4 = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2"


def photo(size=(512,512),mode="RGB"):
    stream = io.BytesIO()
    Image.new(mode,size,(190,150,50) if mode=="RGB" else (190,150,50,0)).save(stream,"PNG")
    return stream.getvalue()


class PromptTests(unittest.TestCase):
    def test_all_categories_and_both_shots_fit_shared_provider_limit(self):
        for kind in ("earring","jhumkas","ring","bangle","mangalsutra","chain","other","nose ring"):
            for shot in ("option_1","option_2"):
                result=video_prompts.compose(kind,shot,{},"Keep the movement subtle.")
                self.assertLessEqual(len(result["prompt"]),PROMPT_LIMITS["kling-3.0"])
                self.assertEqual(set(result["parts"]),{"base","global","category","local","user_note"})

    def test_category_forms_and_rules_differ(self):
        earrings=video_prompts.compose("earrings","option_1",{"subject_motion":"subtle_sway"})
        ring=video_prompts.compose("ring","option_1",{"focus":"setting"})
        self.assertIn("natural sway",earrings["prompt"])
        self.assertIn("band geometry as seen in perspective",ring["prompt"])
        with self.assertRaises(ValueError):
            video_prompts.compose("ring","option_1",{"subject_motion":"subtle_sway"})
        with self.assertRaises(ValueError):
            video_prompts.compose("earrings","option_1",{"focus":"pendant"})

    def test_prompt_edits_change_version_and_long_overrides_fail(self):
        first=video_prompts.compose("ring","option_1",{})
        with patch.object(settings,"VIDEO_GLOBAL_PROMPT","Keep the design exact."):
            second=video_prompts.compose("ring","option_1",{})
        self.assertNotEqual(first["version"],second["version"])
        with patch.object(settings,"VIDEO_BASE_PROMPT","x"*2501):
            with self.assertRaises(ValueError):
                video_prompts.compose("ring","option_1",{})

    def test_reference_presets_have_valid_duration_specific_storyboards(self):
        subtle=video_prompts.compose("necklace","option_1",{})
        self.assertFalse(subtle["multi_shot"])
        self.assertIn("no cuts",subtle["prompt"].lower())
        for duration,shots in ((5,2),(10,3)):
            result=video_prompts.compose("earrings","option_2",{},duration=duration)
            timings=re.findall(r"shot \d+, (\d+),",result["prompt"])
            self.assertEqual(len(timings),shots)
            self.assertEqual(sum(map(int,timings)),duration)
            segments=[segment.strip() for segment in result["parts"]["local"].splitlines()[-1].split(";") if segment.strip()]
            self.assertEqual(len(segments),shots)
            self.assertTrue(all(segment.startswith("shot ") for segment in segments))
            self.assertTrue(result["multi_shot"])
            self.assertIn("same scene",result["prompt"])
            self.assertIn("5 degrees",result["prompt"])
            self.assertIn("focus hunting",result["prompt"])

    def test_all_form_combinations_keep_full_notes_within_provider_limit(self):
        for kind,shot,duration in product(video_prompts.CATEGORIES,("option_1","option_2"),(5,10)):
            fields=video_prompts.form_fields(kind)
            for values in product(*(field["choices"] for field in fields)):
                inputs=dict(zip((field["id"] for field in fields),values))
                result=video_prompts.compose(kind,shot,inputs,"x"*400,duration)
                self.assertLessEqual(len(result["prompt"]),PROMPT_LIMITS["kling-3.0"],(kind,shot,duration,inputs))
                self.assertTrue(result["prompt"].endswith("x"*400))
                if shot=="option_1":
                    turbo=video_prompts.compose(kind,shot,inputs,"x"*400,duration,"kling-2.5-turbo")
                    self.assertLessEqual(len(turbo["prompt"]),PROMPT_LIMITS["kling-2.5-turbo"])

    def test_product_and_scene_locks_survive_every_default_preset(self):
        for shot in ("option_1","option_2"):
            result=video_prompts.compose("earrings",shot,{})
            for rule in ("DESIGN LOCK","SCENE LOCK","stone's count","prongs, settings, motifs and engravings","Product accuracy overrides motion, style and notes"):
                self.assertIn(rule,result["prompt"])
            self.assertIn("Do not invent a matching partner",result["parts"]["category"])
            self.assertNotIn("both earrings",result["prompt"])
            self.assertTrue(result["version"].startswith("video-v3-"))

    def test_new_master_direction_is_reflected_in_compiled_campaign(self):
        result=video_prompts.compose("necklace","option_2",{},duration=10)
        for instruction in ("immutable product reference","gold/silver tone","contact shadows and exposure", "controlled shallow depth of field", "slightly tighter complete view", "hold the final moment"):
            self.assertIn(instruction,result["prompt"])
        self.assertEqual([int(x) for x in re.findall(r"shot \d+, (\d+),",result["prompt"])],[4,3,3])

    def test_rates_and_invalid_model(self):
        self.assertEqual(units("kling-3.0",5),Decimal("4.0"))
        self.assertEqual(units("kling-3.0",10),Decimal("8.0"))
        self.assertEqual(units("kling-2.5-turbo",5),Decimal("2.5"))
        self.assertEqual(units("kling-2.5-turbo",10),Decimal("5.0"))
        for model,duration in (("kling-4.0",5),("kling-3.0",True),("kling-3.0",6)):
            with self.assertRaises(ValueError): units(model,duration)

    def test_storage_validation_never_crops_or_accepts_cutouts(self):
        self.assertEqual(video_storage.normalize(photo())[0],photo())
        for data in (b"not an image",photo((20,20)),photo((1000,300)),photo(mode="RGBA")):
            with self.assertRaises(ValueError): video_storage.normalize(data)

    def test_billing_is_explicit_and_finite(self):
        self.assertIsNone(billing_units({}))
        self.assertEqual(billing_units({"billing":[{"charge_type":"unit","package_type":"video","amount":"0"}]}),0)
        for amount in ("NaN","Infinity","-1"):
            with self.assertRaises(ValueError): billing_units({"billing":[{"charge_type":"unit","package_type":"video","amount":amount}]})
        with self.assertRaises(ValueError): billing_units({"billing":[{"charge_type":"cash","amount":"1"}]})


class ProviderTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        patcher=patch.object(settings,"VIDEO_ALLOW_PAID_GENERATION",True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def job(self,model="kling-3.0"):
        return {"id":str(uuid4()),"model":model,"duration":5,"prompt":"Keep the product still."}

    async def test_both_models_use_current_endpoints_and_only_first_frame(self):
        captured=[]
        def transport(req):
            captured.append(req)
            return httpx.Response(200,json={"code":0,"data":{"id":"task-1"}})
        client=KlingClient("test-key",httpx.MockTransport(transport))
        try:
            for model in ("kling-3.0","kling-2.5-turbo"):
                self.assertEqual(await client.submit(self.job(model),"aW1hZ2U="),"task-1")
            self.assertEqual([request.url.path for request in captured],["/image-to-video/kling-3.0","/image-to-video/kling-2.5-turbo"])
            for req in captured:
                body=json.loads(req.content)
                self.assertEqual([part["type"] for part in body["contents"]],["prompt","first_frame"])
                self.assertNotIn("callback_url",body["options"])
            self.assertEqual(json.loads(captured[0].content)["settings"]["audio"],"off")
            self.assertFalse(json.loads(captured[0].content)["settings"]["multi_shot"])
            self.assertNotIn("audio",json.loads(captured[1].content)["settings"])
        finally: await client.close()

    async def test_timeout_and_server_error_never_retry_paid_post(self):
        for outcome in ("timeout",500):
            calls=[]
            def transport(req):
                calls.append(req)
                if outcome=="timeout": raise httpx.ReadTimeout("offline timeout",request=req)
                return httpx.Response(outcome,text="failure")
            client=KlingClient("test-key",httpx.MockTransport(transport))
            try:
                with self.assertRaises(SubmissionUnknown): await client.submit(self.job(),"aW1hZ2U=")
                self.assertEqual(len(calls),1)
            finally: await client.close()

    async def test_explicit_rejection_is_distinct(self):
        client=KlingClient("test-key",httpx.MockTransport(lambda req:httpx.Response(401,json={"code":1001})))
        try:
            with self.assertRaises(SubmissionRejected): await client.submit(self.job(),"aW1hZ2U=")
        finally: await client.close()

    async def test_paid_lock_blocks_even_direct_provider_submission(self):
        requests=[]
        client=KlingClient("test-key",httpx.MockTransport(lambda request:requests.append(request)))
        try:
            with patch.object(settings,"VIDEO_ALLOW_PAID_GENERATION",False):
                with self.assertRaises(SubmissionRejected): await client.submit(self.job(),"aW1hZ2U=")
            self.assertFalse(requests)
        finally: await client.close()

    async def test_oversized_prompts_never_reach_provider(self):
        requests=[]
        client=KlingClient("test-key",httpx.MockTransport(lambda request:requests.append(request)))
        try:
            for model,limit in PROMPT_LIMITS.items():
                job={**self.job(model),"prompt":"x"*(limit+1)}
                with self.assertRaises(ValueError): await client.submit(job,"aW1hZ2U=")
            self.assertFalse(requests)
        finally: await client.close()

    async def test_malformed_success_is_uncertain_not_a_free_rejection(self):
        for body in ({},{"code":"0"},{"code":0,"data":{}}):
            client=KlingClient("test-key",httpx.MockTransport(lambda req:httpx.Response(200,json=body)))
            try:
                with self.assertRaises(SubmissionUnknown): await client.submit(self.job(),"aW1hZ2U=")
            finally: await client.close()

    async def test_query_recovers_by_external_id_and_checks_identity(self):
        job=self.job()
        def transport(req):
            self.assertEqual(req.url.params["external_task_ids"],job["id"])
            return httpx.Response(200,json={"code":0,"data":[{"id":"task-1","external_id":job["id"],"status":"processing"}]})
        client=KlingClient("test-key",httpx.MockTransport(transport))
        try: self.assertEqual((await client.query(job))["id"],"task-1")
        finally: await client.close()
        client=KlingClient("test-key",httpx.MockTransport(lambda req:httpx.Response(200,json={"code":0,"data":[{"external_id":"somebody-else"}]})))
        try:
            with self.assertRaises(ValueError): await client.query(job)
        finally: await client.close()

    async def test_cdn_download_has_no_key_and_rejects_unsafe_redirect(self):
        seen=[]
        def transport(req):
            seen.append(req)
            self.assertNotIn("Authorization",req.headers)
            return httpx.Response(200,content=MP4)
        client=KlingClient("secret-test-key",httpx.MockTransport(transport))
        try:
            self.assertEqual(await client.download("https://v16.klingai.com/result.mp4"),MP4)
            for url in ("http://v16.klingai.com/a","https://127.0.0.1/a","https://klingai.com.evil.test/a","https://name@v16.klingai.com/a"):
                with self.assertRaises(ValueError): await client.download(url)
            self.assertEqual(len(seen),1)
        finally: await client.close()
        client=KlingClient("key",httpx.MockTransport(lambda req:httpx.Response(302,headers={"location":"http://127.0.0.1/private"})))
        try:
            with self.assertRaises(ValueError): await client.download("https://v16.klingai.com/result.mp4")
        finally: await client.close()


class LocalDatabase:
    def __init__(self,uri): self.uri=uri

    def execute(self,sql,params=(),*,role=None):
        with psycopg.connect(self.uri,autocommit=True) as conn:
            if role: conn.execute("SET ROLE "+role)
            with conn.cursor() as cur:
                cur.execute(sql,params)
                if not cur.description: return []
                return [dict(zip([item[0] for item in cur.description],row)) for row in cur.fetchall()]

    @staticmethod
    def serial(row):
        return {key:str(value) if isinstance(value,UUID) else float(value) if isinstance(value,Decimal) else value.isoformat() if isinstance(value,datetime) else value for key,value in row.items()}

    def call(self,name,params):
        values=[]
        casts={"p_job":"jsonb","p_patch":"jsonb","p_id":"uuid","p_owner":"uuid","p_token":"uuid","p_limit":"numeric","p_expires":"timestamptz"}
        arguments=[]
        for key,value in params.items():
            values.append(Jsonb(value) if isinstance(value,dict) else value)
            arguments.append(key+" => %s"+("::"+casts[key] if key in casts else ""))
        return self.execute("SELECT public."+name+"("+",".join(arguments)+") AS result",values,role="service_role")[0]["result"]

    async def rpc(self,name,params): return await asyncio.to_thread(self.call,name,params)

    async def product(self,product_id,owner_id):
        rows=await asyncio.to_thread(self.execute,"SELECT * FROM products WHERE id=%s AND wholesaler_id=%s",(product_id,owner_id))
        return self.serial(rows[0]) if rows else None

    async def find(self,owner_id,*,job_id=None,key=None):
        field,value=("id",job_id) if job_id else ("idempotency_key",key)
        rows=await asyncio.to_thread(self.execute,"SELECT * FROM video_generations WHERE owner_id=%s AND "+field+"=%s",(owner_id,value))
        return self.serial(rows[0]) if rows else None

    async def history(self,owner_id,product_id):
        return [self.serial(row) for row in await asyncio.to_thread(self.execute,"SELECT * FROM video_generations WHERE owner_id=%s AND product_id=%s ORDER BY created_at DESC",(owner_id,product_id))]


class PilotTests(unittest.IsolatedAsyncioTestCase):
    @classmethod
    def setUpClass(cls):
        cls.directory=tempfile.TemporaryDirectory(prefix="jewel-video-tests-",dir="/private/tmp")
        cls.addClassCleanup(cls.directory.cleanup)
        # Keep pgserver's lock/socket runtime in the disposable test directory,
        # rather than its default user Library cache outside the workspace.
        runtime=Path(cls.directory.name)
        cls.runtime_patch=patch.multiple(PostgresServer,runtime_path=runtime,_lock=fasteners.InterProcessLock(runtime/".server-lock"))
        cls.runtime_patch.start()
        cls.addClassCleanup(cls.runtime_patch.stop)
        cls.server=pgserver.get_server(runtime/"data",cleanup_mode="stop")
        atexit.unregister(cls.server._cleanup)
        cls.addClassCleanup(cls.server.cleanup)
        cls.database=LocalDatabase(cls.server.get_uri())
        cls.database.execute("""
            CREATE ROLE anon; CREATE ROLE authenticated; CREATE ROLE service_role BYPASSRLS;
            GRANT USAGE ON SCHEMA public TO service_role;
            CREATE TABLE products(id uuid PRIMARY KEY,wholesaler_id uuid,title text,jewellery_type text,
                generated_image_urls jsonb,ai_processing_state text);
            GRANT ALL ON products TO service_role;
            CREATE SCHEMA storage;
            GRANT USAGE ON SCHEMA storage TO anon,authenticated,service_role;
            CREATE TABLE storage.buckets(id text PRIMARY KEY,name text,public boolean);
            CREATE TABLE storage.objects(id uuid PRIMARY KEY,bucket_id text);
            ALTER TABLE storage.objects ENABLE ROW LEVEL SECURITY;
            GRANT ALL ON storage.objects TO anon,authenticated,service_role;
            CREATE POLICY existing_broad_access ON storage.objects FOR ALL TO anon,authenticated
                USING(true) WITH CHECK(true);
        """)
        migration=(ROOT/"migrations/037_video_generation_pilot.sql").read_text()
        cls.database.execute(migration)
        cls.database.execute(migration) # Re-applying cannot reset units or grants.

    async def asyncSetUp(self):
        self.database.execute("TRUNCATE video_generations,products")
        self.database.execute("INSERT INTO products VALUES(%s,%s,'Gold earrings','earrings',%s,'ready')",(PRODUCT,OWNER,Jsonb([URL])))
        self.assets={"products/processed/test_v1.png":photo()}
        self.references=tempfile.TemporaryDirectory(prefix="jewel-reference-test-",dir="/private/tmp")
        self.addCleanup(self.references.cleanup)
        self.upload_failures=0
        self.provider_calls=[]
        self.provider_state="succeeded"
        self.submit_timeout=False
        self.query_missing=False
        self.query_failure=False
        self.submit_rejection=False
        self.reported_units="4"
        self.tasks={}
        patches={"VIDEO_ENABLED":True,"VIDEO_ALLOW_PAID_GENERATION":True,"VIDEO_PILOT_USER_IDS":OWNER,"KLING_API_KEY":"offline-key",
                 "VIDEO_MODEL":"kling-3.0","VIDEO_BUDGET_KEY":"test-pack","VIDEO_BUDGET_UNITS":100,
                 "VIDEO_PACK_EXPIRES_AT":EXPIRES,"SUPABASE_URL":"https://video-test.supabase.co",
                 "SUPABASE_JWT_SECRET":"local-offline-video-tests-only-secret","CREDITS_ENABLED":False,
                 "VIDEO_BASE_PROMPT":"","VIDEO_GLOBAL_PROMPT":"","VIDEO_SHOT_1_PROMPT":"","VIDEO_SHOT_2_PROMPT":"",
                 "VIDEO_SHOT_1_PREVIEW_URL":"","VIDEO_SHOT_2_PREVIEW_URL":"","VIDEO_LOCAL_REFERENCE_DIR":self.references.name,"ENVIRONMENT":"test"}
        self.patchers=[patch.multiple(settings,**patches),patch.object(api.limiter,"enabled",False),
                       patch.object(db,"rpc",self.database.rpc),patch.object(db,"product",self.database.product),
                       patch.object(db,"find",self.database.find),patch.object(db,"history",self.database.history)]
        storage=SimpleNamespace()
        def bucket(name):
            def download(path): return self.assets[path]
            def upload(path,data,options):
                if path.endswith("result.mp4") and self.upload_failures:
                    self.upload_failures-=1
                    raise OSError("simulated save failure")
                self.assets[path]=data
            def signed(path,ttl): return {"signedURL":"https://video-test.supabase.co/signed/"+path}
            def remove(paths):
                for path in paths: self.assets.pop(path,None)
            return SimpleNamespace(download=download,upload=upload,create_signed_url=signed,remove=remove)
        storage.from_=bucket
        storage.get_bucket=lambda name:SimpleNamespace(public=False)
        self.patchers.append(patch.object(video_storage,"get_supabase",lambda:SimpleNamespace(storage=storage)))
        for patcher in self.patchers:
            patcher.start()
            self.addCleanup(patcher.stop)
        app=FastAPI()
        app.include_router(api.router)
        self.http=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="http://localhost")
        self.addAsyncCleanup(self.http.aclose)
        self.provider=KlingClient("offline-key",httpx.MockTransport(self.provider_transport))
        self.addAsyncCleanup(self.provider.close)

    def headers(self,owner=OWNER,key=None):
        token=jwt.encode({"sub":owner,"aud":"authenticated","exp":datetime.now(timezone.utc)+timedelta(hours=1)},settings.SUPABASE_JWT_SECRET,algorithm="HS256")
        result={"Authorization":"Bearer "+token}
        if key: result["Idempotency-Key"]=key
        return result

    def provider_transport(self,request):
        self.provider_calls.append(request)
        if request.url.host!="api-singapore.klingai.com":
            self.assertNotIn("Authorization",request.headers)
            return httpx.Response(200,content=MP4)
        if request.method=="POST":
            if self.submit_rejection: return httpx.Response(401,json={"code":1001})
            body=json.loads(request.content)
            external=body["options"]["external_task_id"]
            self.assertEqual(base64.b64decode(body["contents"][1]["url"]),photo())
            self.tasks[external]="provider-"+external
            if self.submit_timeout: raise httpx.ReadTimeout("response lost",request=request)
            return httpx.Response(200,json={"code":0,"data":{"id":self.tasks[external]}})
        if self.query_failure: return httpx.Response(503,text="temporary outage")
        if self.query_missing: return httpx.Response(200,json={"code":0,"data":[]})
        external=request.url.params.get("external_task_ids")
        if not external:
            external=next((key for key,value in self.tasks.items() if value==request.url.params.get("task_ids")),None)
        data=[] if not external or external not in self.tasks else [{"id":self.tasks[external],"external_id":external,"status":self.provider_state,
            "outputs":[{"type":"video","url":"https://v16.klingai.com/video.mp4","duration":5}],
            "billing":[] if self.reported_units is None else [{"charge_type":"unit","package_type":"video","amount":self.reported_units}]}]
        return httpx.Response(200,json={"code":0,"data":data})

    async def quotation(self,**changes):
        payload={"product_id":PRODUCT,**changes}
        result=await self.http.post("/api/videos/quote",json=payload,headers=self.headers())
        self.assertEqual(result.status_code,200,result.text)
        return payload,result.json()

    async def generate(self,**changes):
        payload,quote=await self.quotation(**changes)
        payload["quote_hash"]=quote["quote_hash"]
        key=str(uuid4())
        response=await self.http.post("/api/videos/generate",json=payload,headers=self.headers(key=key))
        self.assertEqual(response.status_code,202,response.text)
        return response.json(),payload,key

    async def tick(self):
        self.database.execute("UPDATE video_generations SET next_poll_at=now()-interval '1 second'")
        job=await db.rpc("video_claim",{"p_concurrency":2,"p_lease_seconds":180})
        if job: await video_pipeline.process_attempt(job,self.provider)
        return job

    def count_posts(self): return sum(req.method=="POST" for req in self.provider_calls)

    async def test_complete_conversion_quote_generate_poll_history_and_download(self):
        options=await self.http.get(f"/api/videos/products/{PRODUCT}/options",headers=self.headers())
        self.assertTrue(options.json()["eligible"])
        self.assertEqual(len(options.json()["shots"]),2)
        self.assertTrue(all(shot["reference_pending"] for shot in options.json()["shots"]))
        job,_,_=await self.generate(category_inputs={"subject_motion":"subtle_sway"})
        self.assertEqual(job["estimated_units"],4)
        self.assertEqual(job["status"],"queued")
        self.assertEqual(self.count_posts(),0)
        await self.tick()
        await self.tick()
        response=await self.http.get("/api/videos/"+job["id"],headers=self.headers())
        result=response.json()
        self.assertEqual(result["status"],"ready")
        self.assertTrue(result["video_url"].endswith("result.mp4"))
        self.assertEqual(result["billed_units"],4)
        self.assertNotIn("provider_task_id",result)
        self.assertEqual(self.assets[f"{OWNER}/{job['id']}/result.mp4"],MP4)
        history=await self.http.get(f"/api/videos/products/{PRODUCT}/history",headers=self.headers())
        self.assertEqual(len(history.json()["videos"]),1)
        self.assertEqual(self.count_posts(),1)

    async def test_authentication_required_even_when_image_credit_metering_is_off(self):
        for headers in ({},{"Authorization":"Bearer "},{"Authorization":"Bearer bad-token"}):
            response=await self.http.get(f"/api/videos/products/{PRODUCT}/options",headers=headers)
            self.assertEqual(response.status_code,401)
        response=await self.http.get(f"/api/videos/products/{PRODUCT}/options",headers=self.headers(OTHER))
        self.assertEqual(response.status_code,403)
        with patch.object(settings,"VIDEO_ENABLED",False):
            response=await self.http.get(f"/api/videos/products/{PRODUCT}/options",headers=self.headers())
            self.assertEqual(response.status_code,403)

    async def test_owned_product_and_job_are_mandatory(self):
        job,_,_=await self.generate()
        with patch.object(settings,"VIDEO_PILOT_USER_IDS",OWNER+","+OTHER):
            for path in (f"/api/videos/products/{PRODUCT}/options",f"/api/videos/products/{PRODUCT}/history","/api/videos/"+job["id"]):
                self.assertEqual((await self.http.get(path,headers=self.headers(OTHER))).status_code,404)
            self.assertEqual((await self.http.post("/api/videos/"+job["id"]+"/cancel",headers=self.headers(OTHER))).status_code,404)

    async def test_raw_upload_and_pending_generation_cannot_start_video(self):
        self.database.execute("UPDATE products SET generated_image_urls='[]',ai_processing_state='pending'")
        options=await self.http.get(f"/api/videos/products/{PRODUCT}/options",headers=self.headers())
        self.assertFalse(options.json()["eligible"])
        response=await self.http.post("/api/videos/quote",json={"product_id":PRODUCT},headers=self.headers())
        self.assertEqual(response.status_code,409)
        self.assertFalse(self.provider_calls)

    async def test_all_four_outputs_are_selectable_and_index_is_validated(self):
        urls=[URL.replace("test_v1",f"test_v{i}") for i in range(1,5)]
        for i,url in enumerate(urls): self.assets[f"products/processed/test_v{i+1}.png"]=photo()
        self.database.execute("UPDATE products SET generated_image_urls=%s",(Jsonb(urls),))
        for i in range(4):
            _,quote=await self.quotation(image_index=i)
            self.assertEqual(quote["source_image_url"],urls[i])
        for bad in (-1,4,True,"0"):
            response=await self.http.post("/api/videos/quote",json={"product_id":PRODUCT,"image_index":bad},headers=self.headers())
            self.assertEqual(response.status_code,422)

    async def test_duration_category_form_and_model_validation(self):
        for changes in ({"duration":True},{"duration":5.0},{"duration":"5"},{"duration":6},{"model":"kling-4.0"},{"shot_id":"custom"},{"category_inputs":{"focus":"setting"}},{"note":"x"*401},{"arbitrary_url":"https://evil.test/a"}):
            response=await self.http.post("/api/videos/quote",json={"product_id":PRODUCT,**changes},headers=self.headers())
            self.assertEqual(response.status_code,422,response.text)
        _,q=await self.quotation(duration=10,model="kling-2.5-turbo")
        self.assertEqual(Decimal(q["estimated_units"]),5)

    async def test_quote_invalidated_by_prompt_or_image_changes(self):
        payload,q=await self.quotation()
        payload["quote_hash"]=q["quote_hash"]
        with patch.object(settings,"VIDEO_GLOBAL_PROMPT","New preservation instructions."):
            response=await self.http.post("/api/videos/generate",json=payload,headers=self.headers(key=str(uuid4())))
            self.assertEqual(response.status_code,409)
        self.assets["products/processed/test_v1.png"]=photo((600,600))
        response=await self.http.post("/api/videos/generate",json=payload,headers=self.headers(key=str(uuid4())))
        self.assertEqual(response.status_code,409)
        self.assertEqual(len(self.database.execute("SELECT id FROM video_generations")),0)

    async def test_unsafe_source_url_is_rejected_before_download(self):
        for url in ("https://evil.test/a.png",URL.replace("products/processed/","raw/"),URL.replace("products/processed/test_v1.png","products/processed/../raw.png")):
            self.database.execute("UPDATE products SET generated_image_urls=%s",(Jsonb([url]),))
            response=await self.http.post("/api/videos/quote",json={"product_id":PRODUCT},headers=self.headers())
            self.assertEqual(response.status_code,422)

    async def test_idempotency_replays_same_attempt_and_rejects_changed_settings(self):
        job,payload,key=await self.generate()
        response=await self.http.post("/api/videos/generate",json=payload,headers=self.headers(key=key))
        self.assertEqual(response.status_code,202)
        self.assertEqual(response.json()["id"],job["id"])
        payload["duration"]=10
        response=await self.http.post("/api/videos/generate",json=payload,headers=self.headers(key=key))
        self.assertEqual(response.status_code,409)
        self.assertEqual((await db.budget())["accounted_units"],4)

    async def test_paid_lock_preserves_free_quotes_and_blocks_new_jobs(self):
        with patch.object(settings,"VIDEO_ALLOW_PAID_GENERATION",False):
            payload,quote=await self.quotation()
            response=await self.http.post("/api/videos/generate",json={**payload,"quote_hash":quote["quote_hash"]},headers=self.headers(key=str(uuid4())))
            self.assertEqual(response.status_code,403)
            self.assertEqual((await db.budget())["accounted_units"],0)
            self.assertEqual(len(await db.history(OWNER,PRODUCT)),0)
            self.assertEqual(self.count_posts(),0)

    async def test_existing_attempt_is_readable_when_paid_lock_is_reenabled(self):
        job,payload,key=await self.generate()
        with patch.object(settings,"VIDEO_ALLOW_PAID_GENERATION",False):
            response=await self.http.post("/api/videos/generate",json=payload,headers=self.headers(key=key))
            self.assertEqual(response.status_code,202)
            self.assertEqual(response.json()["id"],job["id"])
            self.assertEqual((await db.budget())["accounted_units"],4)

    async def test_cancel_before_dispatch_refunds_reservation_but_not_after(self):
        job,_,_=await self.generate()
        path="/api/videos/"+job["id"]+"/cancel"
        for _ in range(2): self.assertEqual((await self.http.post(path,headers=self.headers())).status_code,200)
        self.assertEqual((await db.budget())["available_units"],100)
        self.assertIsNone(await self.tick())
        second,_,_=await self.generate()
        await self.tick()
        response=await self.http.post("/api/videos/"+second["id"]+"/cancel",headers=self.headers())
        self.assertEqual(response.status_code,409)

    async def test_unknown_submission_is_recovered_without_second_paid_call(self):
        job,_,_=await self.generate()
        self.submit_timeout=True
        await self.tick()
        stored=await db.find(OWNER,job_id=job["id"])
        self.assertEqual(stored["status"],"submitting")
        await self.tick()
        self.assertEqual((await db.find(OWNER,job_id=job["id"]))["status"],"ready")
        self.assertEqual(self.count_posts(),1)

    async def test_worker_restart_after_claim_does_not_post_an_unconfirmed_job(self):
        job,_,_=await self.generate()
        first=await db.rpc("video_claim",{"p_concurrency":2,"p_lease_seconds":180})
        self.assertTrue(first["fresh_submission"])
        self.database.execute("UPDATE video_generations SET lease_until=now()-interval '1 second',created_at=now()-interval '10 minutes'")
        await self.tick()
        stored=await db.find(OWNER,job_id=job["id"])
        self.assertEqual(stored["status"],"needs_review")
        self.assertEqual((await db.budget())["accounted_units"],4)
        self.assertEqual(self.count_posts(),0)

    async def test_save_failure_retries_saving_and_keeps_original_source(self):
        job,_,_=await self.generate()
        self.assets["products/processed/test_v1.png"]=photo((600,600))
        await self.tick()
        self.upload_failures=1
        await self.tick()
        self.assertEqual((await db.find(OWNER,job_id=job["id"]))["status"],"saving")
        await self.tick()
        self.assertEqual((await db.find(OWNER,job_id=job["id"]))["status"],"ready")
        self.assertEqual(self.count_posts(),1)

    async def test_rejection_releases_units_and_provider_failure_does_not_guess_refund(self):
        job,_,_=await self.generate()
        self.submit_rejection=True
        await self.tick()
        self.assertEqual((await db.find(OWNER,job_id=job["id"]))["status"],"failed")
        self.assertEqual((await db.budget())["available_units"],100)
        self.submit_rejection=False
        self.provider_state="failed"
        self.reported_units=None
        second,_,_=await self.generate()
        await self.tick()
        await self.tick()
        stored=await db.find(OWNER,job_id=second["id"])
        self.assertIsNone(stored["billed_units"])
        self.assertEqual(stored["accounted_units"],4)

    async def test_poll_outage_does_not_resubmit(self):
        job,_,_=await self.generate()
        await self.tick()
        self.query_failure=True
        await self.tick()
        self.assertEqual((await db.find(OWNER,job_id=job["id"]))["status"],"generating")
        self.query_failure=False
        await self.tick()
        self.assertEqual((await db.find(OWNER,job_id=job["id"]))["status"],"ready")
        self.assertEqual(self.count_posts(),1)

    async def test_in_progress_billing_never_releases_reserved_units(self):
        job,_,_=await self.generate()
        await self.tick()
        self.provider_state="processing"
        self.reported_units="0"
        await self.tick()
        stored=await db.find(OWNER,job_id=job["id"])
        self.assertIsNone(stored["billed_units"])
        self.assertEqual((await db.budget())["accounted_units"],4)
        self.provider_state="failed"
        await self.tick()
        self.assertEqual((await db.budget())["available_units"],100)

    async def test_long_running_task_pauses_for_review_without_resubmission(self):
        job,_,_=await self.generate()
        await self.tick()
        self.database.execute("UPDATE video_generations SET created_at=now()-interval '31 minutes'")
        self.provider_state="processing"
        await self.tick()
        stored=await db.find(OWNER,job_id=job["id"])
        self.assertEqual(stored["status"],"needs_review")
        self.assertEqual(self.count_posts(),1)

    async def test_media_link_outage_preserves_job_and_public_bucket_blocks_submission(self):
        job,_,_=await self.generate()
        with patch.object(video_storage,"signed_url",side_effect=OSError("storage unavailable")):
            response=await self.http.get("/api/videos/"+job["id"],headers=self.headers())
            self.assertEqual(response.status_code,200)
            self.assertTrue(response.json()["media_unavailable"])
            self.assertEqual(response.json()["status"],"queued")
        payload,q=await self.quotation()
        storage=SimpleNamespace(get_bucket=lambda name:SimpleNamespace(public=True))
        with patch.object(video_storage,"get_supabase",lambda:SimpleNamespace(storage=storage)):
            response=await self.http.post("/api/videos/generate",json={**payload,"quote_hash":q["quote_hash"]},headers=self.headers(key=str(uuid4())))
            self.assertEqual(response.status_code,503)
        self.assertEqual(self.count_posts(),0)
        self.assertEqual(len(await db.history(OWNER,PRODUCT)),1)

    async def test_budget_limit_and_pack_expiry_block_generation(self):
        with patch.object(settings,"VIDEO_BUDGET_UNITS",4):
            await self.generate()
            payload,q=await self.quotation()
            response=await self.http.post("/api/videos/generate",json={**payload,"quote_hash":q["quote_hash"]},headers=self.headers(key=str(uuid4())))
            self.assertEqual(response.status_code,402)
        for expires in ("", "2020-01-01T00:00:00+00:00","2026-12-01T00:00:00"):
            with patch.object(settings,"VIDEO_PACK_EXPIRES_AT",expires):
                payload,q=await self.quotation()
                response=await self.http.post("/api/videos/generate",json={**payload,"quote_hash":q["quote_hash"]},headers=self.headers(key=str(uuid4())))
                self.assertIn(response.status_code,(409,503))

    async def test_queued_job_expiry_and_source_tampering_never_call_provider(self):
        job,_,_=await self.generate()
        self.database.execute("UPDATE video_generations SET pack_expires_at=now()-interval '1 day'")
        await self.tick()
        self.assertEqual(self.count_posts(),0)
        self.assertEqual((await db.budget())["available_units"],100)
        second,_,_=await self.generate()
        self.assets[f"{OWNER}/{second['id']}/source.png"]=b"changed"
        await self.tick()
        self.assertEqual(self.count_posts(),0)
        self.assertEqual((await db.budget())["available_units"],100)

    async def test_new_variation_preserves_previous_attempt(self):
        first,_,_=await self.generate()
        second,_,_=await self.generate(parent_id=first["id"],shot_id="option_2")
        self.assertEqual(second["parent_id"],first["id"])
        self.assertNotEqual(first["id"],second["id"])
        self.assertEqual((await db.budget())["accounted_units"],8)

    async def test_energetic_reference_uses_storyboard_and_supported_model(self):
        job,_,_=await self.generate(shot_id="option_2")
        await self.tick()
        body=json.loads(next(req.content for req in self.provider_calls if req.method=="POST"))
        self.assertTrue(body["settings"]["multi_shot"])
        self.assertIn("shot 1, 3,",body["contents"][0]["text"])
        response=await self.http.post("/api/videos/quote",json={"product_id":PRODUCT,"shot_id":"option_2","model":"kling-2.5-turbo"},headers=self.headers())
        self.assertEqual(response.status_code,422)

    async def test_reference_previews_are_local_authenticated_and_not_provider_inputs(self):
        Path(self.references.name,"option_1.mp4").write_bytes(MP4)
        options=await self.http.get(f"/api/videos/products/{PRODUCT}/options",headers=self.headers())
        first=options.json()["shots"][0]
        self.assertFalse(first["reference_pending"])
        self.assertEqual(first["preview_url"],"/api/videos/references/option_1")
        response=await self.http.get(first["preview_url"],headers=self.headers())
        self.assertEqual(response.content,MP4)
        self.assertEqual(response.headers["content-type"],"video/mp4")
        self.assertEqual((await self.http.get(first["preview_url"])).status_code,401)
        with patch.object(settings,"ENVIRONMENT","production"):
            self.assertEqual((await self.http.get(first["preview_url"],headers=self.headers())).status_code,404)
        self.assertFalse(self.provider_calls)

    async def test_local_entry_point_rejects_hosted_database_and_production_environment(self):
        from app.video_local import app as local_app
        with self.assertRaises(RuntimeError):
            async with local_app.router.lifespan_context(local_app): pass
        with patch.object(settings,"SUPABASE_URL","http://127.0.0.1:54321"),patch.object(settings,"VIDEO_ENABLED",False):
            async with local_app.router.lifespan_context(local_app):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=local_app),base_url="http://localhost") as client:
                    result=await client.get("/health")
                    self.assertEqual(result.json()["mode"],"local-video-only")
            with patch.object(settings,"ENVIRONMENT","production"):
                with self.assertRaises(RuntimeError):
                    async with local_app.router.lifespan_context(local_app): pass

    async def test_local_storage_http_is_allowed_only_for_loopback_in_development(self):
        local="http://127.0.0.1:54321/storage/v1/object/public/plant-images/products/processed/test.png"
        with patch.object(settings,"SUPABASE_URL","http://127.0.0.1:54321"):
            self.assertEqual(video_storage.source_location(local),("plant-images","products/processed/test.png"))
            with patch.object(settings,"ENVIRONMENT","production"):
                with self.assertRaises(ValueError): video_storage.source_location(local)
        with patch.object(settings,"SUPABASE_URL","http://hosted.test"):
            with self.assertRaises(ValueError): video_storage.source_location(local.replace("127.0.0.1:54321","hosted.test"))

    async def test_postgres_budget_and_idempotency_are_atomic(self):
        first,_,_=await self.generate()
        row=await db.find(OWNER,job_id=first["id"])
        self.database.execute("DELETE FROM video_generations")
        row["idempotency_key"]=str(uuid4())
        params={"p_job":row,"p_limit":4,"p_expires":EXPIRES}
        with ThreadPoolExecutor(max_workers=6) as pool:
            same=list(pool.map(lambda _:self.database.call("video_reserve",params),range(6)))
        self.assertEqual(len({item["id"] for item in same}),1)
        self.assertEqual((await db.budget())["accounted_units"],4)
        self.database.execute("DELETE FROM video_generations")
        def compete(_):
            candidate={**row,"id":str(uuid4()),"idempotency_key":str(uuid4())}
            return self.database.call("video_reserve",{**params,"p_job":candidate})
        with ThreadPoolExecutor(max_workers=6) as pool: distinct=list(pool.map(compete,range(6)))
        self.assertEqual(sum("error" not in result for result in distinct),1)
        self.assertEqual((await db.budget())["accounted_units"],4)

    async def test_claim_leases_concurrency_and_stale_writer_are_enforced(self):
        for _ in range(3): await self.generate()
        claim=lambda _:self.database.call("video_claim",{"p_concurrency":2,"p_lease_seconds":180})
        with ThreadPoolExecutor(max_workers=3) as pool: claimed=list(pool.map(claim,range(3)))
        active=[job for job in claimed if job]
        self.assertEqual(len(active),2)
        self.assertNotEqual(active[0]["id"],active[1]["id"])
        old=active[0]
        self.database.execute("UPDATE video_generations SET lease_until=now()-interval '1 second' WHERE id=%s",(old["id"],))
        new=self.database.call("video_claim",{"p_concurrency":2,"p_lease_seconds":180})
        self.assertEqual(new["id"],old["id"])
        self.assertFalse(new["fresh_submission"])
        self.assertIsNone(self.database.call("video_update",{"p_id":old["id"],"p_token":old["lease_token"],"p_patch":{"status":"ready"},"p_release":True}))

    async def test_client_roles_cannot_read_jobs_or_run_budget_functions(self):
        for role in ("anon","authenticated"):
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.database.execute("SELECT * FROM video_generations",role=role)
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.database.execute("SELECT video_budget('test-pack',100)",role=role)

    async def test_private_video_bucket_blocks_broad_client_storage_policies(self):
        self.assertFalse(self.database.execute("SELECT public FROM storage.buckets WHERE id='video-outputs'")[0]["public"])
        video_id,other_id=str(uuid4()),str(uuid4())
        self.database.execute("INSERT INTO storage.objects VALUES(%s,'video-outputs'),(%s,'existing-images')",(video_id,other_id),role="service_role")
        for role in ("anon","authenticated"):
            visible=self.database.execute("SELECT id FROM storage.objects WHERE id IN (%s,%s)",(video_id,other_id),role=role)
            self.assertEqual([str(row["id"]) for row in visible],[other_id])
            with self.assertRaises(psycopg.errors.InsufficientPrivilege):
                self.database.execute("INSERT INTO storage.objects VALUES(%s,'video-outputs')",(str(uuid4()),),role=role)
            self.assertFalse(self.database.execute("UPDATE storage.objects SET bucket_id='existing-images' WHERE id=%s RETURNING id",(video_id,),role=role))
            self.assertFalse(self.database.execute("DELETE FROM storage.objects WHERE id=%s RETURNING id",(video_id,),role=role))

    async def test_postgres_enforces_each_models_prompt_limit(self):
        job,_,_=await self.generate()
        row=await db.find(OWNER,job_id=job["id"])
        for model,limit in PROMPT_LIMITS.items():
            candidate={**row,"id":str(uuid4()),"idempotency_key":str(uuid4()),"model":model,"prompt":"x"*limit}
            result=self.database.call("video_reserve",{"p_job":candidate,"p_limit":100,"p_expires":EXPIRES})
            self.assertEqual(result["id"],candidate["id"])
            candidate.update(id=str(uuid4()),idempotency_key=str(uuid4()),prompt="x"*(limit+1))
            with self.assertRaises(psycopg.errors.CheckViolation):
                self.database.call("video_reserve",{"p_job":candidate,"p_limit":100,"p_expires":EXPIRES})


if __name__=="__main__": unittest.main()
