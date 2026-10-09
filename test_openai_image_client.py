"""Exercise the real OpenAI client and Chamak worker with a mocked HTTP boundary."""
import base64
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from uuid import uuid4
import httpx
from pydantic import ValidationError
from app.config import Settings, settings
from app.services.ai import OpenAIImageClient
from app.services.chamak import run_stage4_generation_openai

class OpenAIImageTests(unittest.IsolatedAsyncioTestCase):
    def test_timeout_has_default_and_accepts_deployment_override(self):
        self.assertEqual(Settings.model_fields['OPENAI_IMAGE_TIMEOUT_SECONDS'].default, 300.0)
        with patch.dict('os.environ', {'OPENAI_IMAGE_TIMEOUT_SECONDS': '420'}):
            self.assertEqual(Settings(_env_file=None).OPENAI_IMAGE_TIMEOUT_SECONDS, 420)
        with self.assertRaises(ValidationError):
            Settings(OPENAI_IMAGE_TIMEOUT_SECONDS=0)

    async def test_worker_submits_both_images_and_saves_result(self):
        generation_id, owner = str(uuid4()), str(uuid4())
        row = {'id': generation_id, 'wholesaler_id': owner,
               'source_image_1_url': 'https://example.com/one.jpg',
               'source_image_2_url': 'https://example.com/two.png',
               'stage1_analysis_json': {'jewelry_type': 'necklace'}}
        image_bytes = b'generated-image'
        response = httpx.Response(200, request=httpx.Request('POST', 'https://api.openai.com/v1/images/edits'), json={'data': [{'b64_json': base64.b64encode(image_bytes).decode()}]})
        http_client = AsyncMock()
        http_client.request.return_value = response
        context = AsyncMock()
        context.__aenter__.return_value = http_client
        with patch.object(settings, 'OPENAI_API_KEY', 'unit-test-key'), \
             patch('app.services.ai.httpx.AsyncClient', return_value=context) as http_factory, \
             patch('app.services.chamak.openai_image_client', OpenAIImageClient()), \
             patch('app.services.chamak.fetch_chamak_generation', AsyncMock(return_value=row)), \
             patch('app.services.chamak.fetch_image_bytes_and_content_type', AsyncMock(side_effect=[(b'one', 'image/jpeg'), (b'two', 'image/png')])), \
             patch('app.services.chamak.update_chamak_generation', AsyncMock()) as update, \
             patch('app.services.chamak.upload_chamak_output', return_value=SimpleNamespace(url=f'{owner}/{generation_id}.png', variants={})) as upload, \
             patch('app.services.chamak._refund_failed_generation', AsyncMock()) as refund:
            await run_stage4_generation_openai(generation_id)
            http_factory.assert_called_once_with(timeout=settings.OPENAI_IMAGE_TIMEOUT_SECONDS)
            files = http_client.request.call_args.kwargs['files']
            self.assertEqual(files, [('image[]', ('design1.jpg', b'one', 'image/jpeg')),
                                    ('image[]', ('design2.png', b'two', 'image/png'))])
            self.assertEqual(upload.call_args.kwargs['file_content'], image_bytes)
            self.assertEqual(update.call_args.args[1]['status'], 'done')
            refund.assert_not_awaited()

if __name__ == '__main__': unittest.main()
