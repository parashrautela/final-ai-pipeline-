"""Nano Banana 2 migration contract tests; no paid API requests."""
import unittest
from unittest.mock import AsyncMock, patch
import httpx
from app.config import Settings
from app.services.ai import NanobanaClient


class NanoBanana2Tests(unittest.IsolatedAsyncioTestCase):
    async def test_product_payload_preserves_long_prompt(self):
        client = NanobanaClient()
        prompt = 'PRODUCT RULES ' * 1000 + '\nSCENE 2 — CHARCOAL'
        response = httpx.Response(200, json={'data': {'taskId': 'product-task'}})
        with patch('app.services.ai._request_with_retry', new=AsyncMock(return_value=response)) as submit, \
             patch.object(client, '_await_task', new=AsyncMock(return_value=b'image')) as poll:
            self.assertEqual(await client.enhance_image('https://example.test/source.png', prompt=prompt), b'image')
            self.assertTrue(submit.call_args.args[2].endswith('/generate-2'))
            payload = submit.call_args.kwargs['json']
            self.assertEqual(payload, {'prompt': prompt, 'imageUrls': ['https://example.test/source.png'],
                                      'aspectRatio': '1:1', 'resolution': '2K',
                                      'googleSearch': False, 'outputFormat': 'png'})
            poll.assert_awaited_once_with('product-task', label='image')

    async def test_sets_keep_source_and_output_order_and_force_2k(self):
        client = NanobanaClient()
        sources = ['https://example.test/a.png', 'https://example.test/b.png']
        for count in range(1, 5):
            responses = [httpx.Response(200, json={'data': {'taskId': str(i)}}) for i in range(count)]
            async def finish(task_id, **kwargs):
                return task_id.encode()
            with patch('app.services.ai._request_with_retry', new=AsyncMock(side_effect=responses)) as submit, \
                 patch.object(client, '_await_task', new=AsyncMock(side_effect=finish)):
                images = await client.compose_set(sources, prompt='Preserve both source designs',
                                                  output_count=count, resolution='4K')
                self.assertEqual(images, [str(i).encode() for i in range(count)])
                for call in submit.call_args_list:
                    self.assertTrue(call.args[2].endswith('/generate-2'))
                    payload = call.kwargs['json']
                    self.assertEqual(payload['imageUrls'], sources)
                    self.assertEqual(payload['resolution'], '2K')
                    self.assertEqual(payload['aspectRatio'], '2:3')
                    self.assertNotIn('type', payload)

    async def test_invalid_inputs_rejected_before_submission(self):
        client = NanobanaClient()
        with patch('app.services.ai._request_with_retry', new=AsyncMock()) as submit:
            for prompt in ('', 'x' * 20001):
                with self.assertRaises(ValueError):
                    await client.enhance_image('https://example.test/a.png', prompt=prompt)
                with self.assertRaises(ValueError):
                    await client.compose_set(['https://example.test/a.png'], prompt=prompt)
            for sources in ([], ['https://example.test/a.png'] * 15):
                with self.assertRaises(ValueError):
                    await client.compose_set(sources, prompt='Preserve designs')
            submit.assert_not_awaited()
        client._validate_prompt('x' * 20000)

    async def test_terminal_failures_do_not_wait_for_timeout(self):
        for flag in (2, '2', 3, '3'):
            response = httpx.Response(200, json={'data': {'successFlag': flag, 'errorMessage': 'generation failed'}})
            with patch('app.services.ai._request_with_retry', new=AsyncMock(return_value=response)) as poll, \
                 patch('app.services.ai.asyncio.sleep', new=AsyncMock()):
                with self.assertRaisesRegex(RuntimeError, 'generation failed'):
                    await NanobanaClient()._await_task('failed-task')
                poll.assert_awaited_once()

    async def test_success_download(self):
        response = httpx.Response(200, json={'data': {'successFlag': 1,
                                   'response': {'resultImageUrl': 'https://example.test/result.png'}}})
        downloaded = httpx.Response(200, content=b'png-bytes', request=httpx.Request('GET', 'https://example.test/result.png'))
        with patch('app.services.ai._request_with_retry', new=AsyncMock(return_value=response)), \
             patch('app.services.ai.asyncio.sleep', new=AsyncMock()), \
             patch('app.services.ai.httpx.AsyncClient.get', new=AsyncMock(return_value=downloaded)) as download:
            self.assertEqual(await NanobanaClient()._await_task('successful-task'), b'png-bytes')
            download.assert_awaited_once_with('https://example.test/result.png', follow_redirects=True)

    def test_legacy_environment_settings_cannot_restore_4k(self):
        settings = Settings(NANOBANA_IMAGE_SIZE='4K', SET_CREATION_RESOLUTION='4K')
        self.assertEqual(settings.nanobana_resolution, '2K')
        self.assertEqual(settings.set_creation_resolution, '2K')


if __name__ == '__main__':
    unittest.main()
