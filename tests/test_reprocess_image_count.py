"""Re-upload counts must reach the pipeline for both native request formats."""
import sys
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import httpx
import app.main as main

PRODUCT_ID = "00000000-0000-4000-8000-000000000001"


class ReprocessCountTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pipeline = AsyncMock()
        self.product = {"id": PRODUCT_ID, "title": "Ring", "image_url": "https://test/raw.jpg"}
        for target, replacement in [
            ("fetch_job_by_id", AsyncMock(return_value=self.product)),
            ("_run_product_pipeline", self.pipeline),
            ("upload_raw_image", MagicMock(return_value="https://test/replacement.jpg")),
            ("update_product_image_url", AsyncMock()),
            ("get_supabase", MagicMock()),
        ]:
            patcher = patch.object(main, target, replacement)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(main.limiter, "enabled", False)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=main.app), base_url="http://test")
        self.addAsyncCleanup(self.client.aclose)

    async def test_existing_photo_json_count_reaches_pipeline(self):
        for count in (1, 2, 3, 4):
            response = await self.client.post(f"/process/{PRODUCT_ID}", json={"image_count": count})
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(self.pipeline.await_args.args[1], count)

    async def test_replacement_photo_multipart_count_reaches_pipeline(self):
        response = await self.client.post(
            f"/process/{PRODUCT_ID}", data={"image_count": "3"},
            files={"file": ("ring.jpg", b"test-photo", "image/jpeg")},
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.pipeline.await_args.args[1], 3)
        self.assertEqual(self.pipeline.await_args.args[0]["raw_image_url"], "https://test/replacement.jpg")

    async def test_invalid_counts_never_start_processing(self):
        for count in (0, 5, True, 1.5, "3"):
            response = await self.client.post(f"/process/{PRODUCT_ID}", json={"image_count": count})
            self.assertEqual(response.status_code, 422, response.text)
        self.pipeline.assert_not_awaited()

    async def test_legacy_client_keeps_server_default(self):
        response = await self.client.post(f"/process/{PRODUCT_ID}", json={})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertIsNone(self.pipeline.await_args.args[1])


if __name__ == "__main__":
    unittest.main()
