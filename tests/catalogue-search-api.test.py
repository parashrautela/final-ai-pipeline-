import io
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import httpx
from fastapi import FastAPI
from PIL import Image
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from app.routers import catalogue_search as api

class Query:
    def __init__(self,status):self.status=status
    def select(self,*args):return self
    def eq(self,*args):return self
    def limit(self,*args):return self
    def execute(self):return SimpleNamespace(data=[dict(id="retailer",verification_status=self.status)])

class SearchAccess(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        app=FastAPI();app.include_router(api.router);app.state.limiter=api.limiter
        app.add_exception_handler(RateLimitExceeded,_rate_limit_exceeded_handler)
        self.client=httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url="https://app.test")
        output=io.BytesIO();Image.new("RGB",(30,30),"white").save(output,"JPEG");self.photo=output.getvalue()
    async def asyncTearDown(self):await self.client.aclose()
    async def call(self,authorization=None,photo=None):
        return await self.client.post("/api/retailer/image-search",headers={"Authorization":authorization} if authorization else {},
            data={"jewellery_type":"necklace"},files={"photo":("query.jpg",self.photo if photo is None else photo,"image/jpeg")})
    def db(self,role="retailer",status="verified"):
        user=SimpleNamespace(id="user",user_metadata=dict(role=role))
        return SimpleNamespace(auth=SimpleNamespace(get_user=lambda token:SimpleNamespace(user=user)),table=lambda table:Query(status))
    async def test_no_anonymous_bypass(self):
        self.assertEqual((await self.call()).status_code,401)
        self.assertEqual((await self.call("Bearer ")).status_code,401)
    async def test_wrong_role_or_unverified(self):
        for role,status in [("wholesaler","verified"),("employee","verified"),("retailer","pending")]:
            with patch.object(api,"get_supabase",return_value=self.db(role,status)):
                self.assertEqual((await self.call("Bearer test-session")).status_code,403)
    async def test_revoked_session(self):
        db=self.db();db.auth.get_user=lambda token:(_ for _ in ()).throw(ValueError("revoked"))
        with patch.object(api,"get_supabase",return_value=db):self.assertEqual((await self.call("Bearer revoked")).status_code,401)
    async def test_corrupt_photo_rejected(self):
        with patch.object(api,"get_supabase",return_value=self.db()):self.assertEqual((await self.call("Bearer test-session",b"broken")).status_code,400)
    async def test_authorized_readonly_query(self):
        rows=[dict(id="product",is_published=True,jewellery_type="necklace")]
        result=dict(matches=[dict(id="product",similarity=.99)],checked=1,total=1,skipped=0)
        with patch.object(api,"get_supabase",return_value=self.db()),patch.object(api,"fetch_rows",return_value=rows),patch.object(api.index,"search",new=AsyncMock(return_value=result)) as search:
            response=await self.call("Bearer test-session")
            self.assertEqual(response.status_code,200);self.assertEqual(response.json(),result)
            self.assertIn("no-store",response.headers["cache-control"]);search.assert_awaited_once()
    async def test_index_not_ready_is_retryable(self):
        rows=[dict(id="product",is_published=True,jewellery_type="necklace")]
        with patch.object(api,"get_supabase",return_value=self.db()),patch.object(api,"fetch_rows",return_value=rows),patch.object(api.index,"search",new=AsyncMock(side_effect=LookupError("Updating index"))):
            response=await self.call("Bearer test-session");self.assertEqual(response.status_code,503);self.assertEqual(response.headers['retry-after'],'10')

if __name__=="__main__":unittest.main()
