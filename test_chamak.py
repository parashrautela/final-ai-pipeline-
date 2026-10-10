import asyncio
import json
import unittest
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

from app.config import settings
from app.services.chamak import (
    compile_chamak_prompt,
    run_stage1_vision_analysis,
    run_stage4_generation,
)
from app.validation import ChamakGenerationRequest, ValidationError, validate_uuid


class TestChamakPipeline(unittest.TestCase):
    def test_validate_uuid(self):
        valid_uuid = str(uuid4())
        self.assertEqual(validate_uuid(valid_uuid, "generation_id"), valid_uuid)

        with self.assertRaises(ValidationError):
            validate_uuid("invalid-uuid", "generation_id")

        with self.assertRaises(ValidationError):
            validate_uuid("", "generation_id")

    def test_chamak_generation_request_model(self):
        valid_id = str(uuid4())
        req = ChamakGenerationRequest(generation_id=valid_id)
        self.assertEqual(req.generation_id, valid_id)

        with self.assertRaises(Exception):
            ChamakGenerationRequest(generation_id="not-a-valid-uuid")

    def test_compile_chamak_prompt_defaults(self):
        analysis = {
            "jewelry_type": "necklace",
            "image1_strengths": ["intricate filigree craftsmanship", "radiant 22k yellow gold luster"],
            "image2_weaknesses": ["clunky base geometry", "overly heavy clasp"],
            "image2_strengths": ["emerald centerpiece"],
        }
        form_input = {
            "slider_weights": {
                "filigree": 0.8,
                "weight": 0.2,
            }
        }
        note = "Make the filigree extra delicate around the center drop."

        prompt = compile_chamak_prompt(analysis, form_input, note)

        self.assertIn("necklace", prompt)
        self.assertIn("intricate filigree craftsmanship", prompt)
        self.assertIn("clunky base geometry", prompt)
        self.assertIn("emerald centerpiece", prompt)
        self.assertIn("filigree", prompt)
        self.assertIn("Make the filigree extra delicate", prompt)
        self.assertIn("Hyper-realistic luxury fine jewelry", prompt)
        self.assertIn("85mm macro lens", prompt)

    def test_compile_chamak_prompt_empty_inputs(self):
        prompt = compile_chamak_prompt(None, None, None)
        self.assertIsInstance(prompt, str)
        self.assertTrue(len(prompt) > 50)
        self.assertIn("Hyper-realistic luxury fine jewelry", prompt)

    def test_run_stage1_vision_analysis_success(self):
        gen_id = str(uuid4())
        mock_row = {
            "id": gen_id,
            "source_image_1_url": "https://example.com/img1.jpg",
            "source_image_2_url": "https://example.com/img2.jpg",
        }
        mock_analysis = {
            "jewelry_type": "ring",
            "image1_strengths": ["solitaire sparkle", "tapered band"],
            "image1_weaknesses": [],
            "image2_strengths": [],
            "image2_weaknesses": ["thick bezel"],
            "near_identical": False,
            "type_mismatch": False,
            "content_flag": "ok",
        }

        async def run_test():
            with patch("app.services.chamak.fetch_chamak_generation", new_callable=AsyncMock) as mock_fetch, \
                 patch("app.services.chamak.update_chamak_generation", new_callable=AsyncMock) as mock_update, \
                 patch("app.services.chamak.fetch_image_bytes_and_content_type", new_callable=AsyncMock) as mock_img, \
                 patch("app.services.chamak.call_openai_vision_analysis", new_callable=AsyncMock) as mock_vision:

                mock_fetch.return_value = mock_row
                mock_img.side_effect = [(b"fake_bytes_1", "image/jpeg"), (b"fake_bytes_2", "image/jpeg")]
                mock_vision.return_value = mock_analysis

                await run_stage1_vision_analysis(gen_id)

                # Check update calls
                mock_update.assert_any_call(gen_id, {"status": "analyzing"})
                mock_update.assert_called_with(
                    gen_id,
                    {
                        "stage1_analysis_json": mock_analysis,
                        "content_flag_hit": "ok",
                        "status": "awaiting_input",
                    },
                )

        asyncio.run(run_test())

    def test_run_stage1_vision_analysis_content_flag_failed(self):
        gen_id = str(uuid4())
        mock_row = {
            "id": gen_id,
            "source_image_1_url": "https://example.com/img1.jpg",
            "source_image_2_url": "https://example.com/img2.jpg",
        }
        mock_analysis = {
            "jewelry_type": "unknown",
            "image1_strengths": [],
            "image1_weaknesses": [],
            "image2_strengths": [],
            "image2_weaknesses": [],
            "near_identical": False,
            "type_mismatch": False,
            "content_flag": "not_jewelry",
        }

        async def run_test():
            with patch("app.services.chamak.fetch_chamak_generation", new_callable=AsyncMock) as mock_fetch, \
                 patch("app.services.chamak.update_chamak_generation", new_callable=AsyncMock) as mock_update, \
                 patch("app.services.chamak.fetch_image_bytes_and_content_type", new_callable=AsyncMock) as mock_img, \
                 patch("app.services.chamak.call_openai_vision_analysis", new_callable=AsyncMock) as mock_vision, \
                 patch("app.services.chamak._refund_failed_generation", new_callable=AsyncMock) as mock_refund:

                mock_fetch.return_value = mock_row
                mock_img.side_effect = [(b"fake_bytes_1", "image/jpeg"), (b"fake_bytes_2", "image/jpeg")]
                mock_vision.return_value = mock_analysis

                await run_stage1_vision_analysis(gen_id)
                mock_refund.assert_awaited_once()

                mock_update.assert_called_with(
                    gen_id,
                    {
                        "stage1_analysis_json": mock_analysis,
                        "content_flag_hit": "not_jewelry",
                        "status": "failed",
                    },
                )

        asyncio.run(run_test())

    def test_run_stage4_generation_success(self):
        gen_id = str(uuid4())
        wholesaler_id = str(uuid4())
        mock_row = {
            "id": gen_id,
            "wholesaler_id": wholesaler_id,
            "source_image_1_url": "https://example.com/img1.jpg",
            "source_image_2_url": "https://example.com/img2.jpg",
            "stage1_analysis_json": {"jewelry_type": "bangle", "image1_strengths": ["smooth edges"]},
            "wholesaler_form_json": {"slider_weights": {"finish": 0.9}},
            "note_text": "High polish 22k gold",
        }

        async def run_test():
            with patch("app.services.chamak.fetch_chamak_generation", new_callable=AsyncMock) as mock_fetch, \
                 patch("app.services.chamak.update_chamak_generation", new_callable=AsyncMock) as mock_update, \
                 patch("app.services.chamak.nanobana_client.enhance_image", new_callable=AsyncMock) as mock_enhance, \
                 patch("app.services.chamak.upload_chamak_output") as mock_upload:

                mock_fetch.return_value = mock_row
                mock_enhance.return_value = b"rendered_image_bytes"
                mock_upload.return_value = f"{wholesaler_id}/{gen_id}.png"

                await run_stage4_generation(gen_id)

                mock_update.assert_any_call(gen_id, {"status": "generating"})
                mock_upload.assert_called_once_with(
                    file_content=b"rendered_image_bytes",
                    wholesaler_id=wholesaler_id,
                    generation_id=gen_id,
                )
                # Verify final update call
                last_call = mock_update.call_args_list[-1]
                updated_fields = last_call[0][1]
                self.assertEqual(updated_fields["status"], "done")
                self.assertEqual(updated_fields["output_image_url"], f"{wholesaler_id}/{gen_id}.png")
                self.assertEqual(updated_fields["prompt_version"], "v1.0-chamak")
                self.assertTrue("compiled_prompt_text" in updated_fields)
                self.assertTrue("completed_at" in updated_fields)

    def test_fastapi_endpoints(self):
        import datetime as dt
        import httpx
        import jwt
        from app.main import app, limiter

        owner = str(uuid4())
        secret = "chamak-unit-test-secret-at-least-32-characters"
        token = jwt.encode({"sub": owner, "aud": "authenticated",
                            "exp": dt.datetime.now(dt.timezone.utc) + dt.timedelta(minutes=5)},
                           secret, algorithm="HS256")
        headers = {"Authorization": f"Bearer {token}", "Idempotency-Key": "endpoint-test"}
        charge = {"ok": True, "replayed": False, "ledger_id": str(uuid4())}

        async def run_endpoint_tests():
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                gen_id = str(uuid4())

                # Authentication happens before looking up a generation.
                with patch("app.main.fetch_chamak_generation", new_callable=AsyncMock) as fetch:
                    for method, path in [("POST", "/api/chamak/analyze"),
                                         ("POST", "/api/chamak/generate"),
                                         ("GET", f"/api/chamak/{gen_id}")]:
                        kwargs = {"json": {"generation_id": gen_id}} if method == "POST" else {}
                        response = await client.request(method, path, **kwargs)
                        self.assertEqual(response.status_code, 401)
                    fetch.assert_not_awaited()

                with patch("app.main.fetch_chamak_generation", new_callable=AsyncMock) as fetch:
                    fetch.return_value = None
                    response = await client.post("/api/chamak/analyze", headers=headers,
                                                 json={"generation_id": gen_id})
                    self.assertEqual(response.status_code, 404)

                # A signed-in caller cannot access another owner's generation.
                with patch("app.main.fetch_chamak_generation", new_callable=AsyncMock) as fetch:
                    fetch.return_value = {"id": gen_id, "wholesaler_id": str(uuid4()), "status": "queued"}
                    response = await client.post("/api/chamak/analyze", headers=headers,
                                                 json={"generation_id": gen_id})
                    self.assertEqual(response.status_code, 404)

                for endpoint, initial, expected, task in [
                    ("analyze", "queued", "analyzing", "run_stage1_vision_analysis"),
                    ("generate", "awaiting_input", "generating", "run_stage4_generation"),
                ]:
                    with patch("app.main.fetch_chamak_generation", new_callable=AsyncMock) as fetch, \
                         patch("app.main.update_chamak_generation", new_callable=AsyncMock) as update, \
                         patch(f"app.main.{task}", new_callable=AsyncMock) as job, \
                         patch("app.main.spend_credits", new_callable=AsyncMock, return_value=charge) as spend, \
                         patch("app.main.count_prior_debits", new_callable=AsyncMock, return_value=0):
                        fetch.return_value = {"id": gen_id, "wholesaler_id": owner, "status": initial}
                        response = await client.post(f"/api/chamak/{endpoint}", headers=headers,
                                                     json={"generation_id": gen_id})
                        self.assertEqual(response.status_code, 202)
                        self.assertEqual(response.json()["generation_id"], gen_id)
                        self.assertEqual(response.json()["status"], expected)
                        update.assert_awaited_once_with(gen_id, {"status": expected})
                        spend.assert_awaited_once()
                        job.assert_awaited_once_with(gen_id, charge)

                with patch("app.main.fetch_chamak_generation", new_callable=AsyncMock) as fetch:
                    fetch.return_value = {"id": gen_id, "wholesaler_id": owner, "status": "done",
                                          "output_image_url": f"{owner}/{gen_id}.png"}
                    response = await client.get(f"/api/chamak/{gen_id}", headers=headers)
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json()["status"], "done")
                    self.assertEqual(response.json()["output_image_url"], f"{owner}/{gen_id}.png")

                response = await client.post("/api/chamak/analyze", headers=headers,
                                             json={"generation_id": "invalid-uuid"})
                self.assertEqual(response.status_code, 422)

        with patch.object(settings, "SUPABASE_JWT_SECRET", secret), \
             patch.object(settings, "CREDITS_ENABLED", True), \
             patch.object(limiter, "enabled", False):
            asyncio.run(run_endpoint_tests())


if __name__ == "__main__":
    unittest.main()
