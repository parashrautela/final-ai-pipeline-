import unittest
from unittest.mock import AsyncMock, patch
import httpx
from app.services.ai import _request_with_retry
from app.services.chamak import call_openai_vision_analysis, compile_chamak_prompt, run_stage1_vision_analysis


def response(status, payload):
    return httpx.Response(status, json=payload, request=httpx.Request('POST', 'https://api.openai.com/v1/chat/completions'))


class AnalysisFailures(unittest.IsolatedAsyncioTestCase):
    async def test_analysis_retries_transient_rate_limit_then_succeeds(self):
        client = AsyncMock()
        client.request.side_effect = [response(429, {'error': {'type': 'rate_limit_error'}}), response(200, {'choices': [{'message': {'content': '{"content_flag":"ok"}'}}]})]
        context = AsyncMock()
        context.__aenter__.return_value = client
        with patch('app.services.chamak.httpx.AsyncClient', return_value=context), patch('app.services.ai.asyncio.sleep', AsyncMock()):
            result = await call_openai_vision_analysis(b'one', 'image/jpeg', b'two', 'image/png')
        self.assertEqual(result['content_flag'], 'ok')
        self.assertEqual(client.request.await_count, 2)
        self.assertEqual(len(client.request.call_args.kwargs['json']['messages'][1]['content']), 3)

    async def test_quota_error_does_not_retry_and_worker_saves_reason(self):
        quota = response(429, {'error': {'type': 'insufficient_quota', 'code': 'credit_balance_exhausted'}})
        client = AsyncMock()
        client.request.return_value = quota
        with self.assertRaises(httpx.HTTPStatusError) as raised:
            await _request_with_retry(client, 'POST', str(quota.url), headers={}, max_retries=3)
        self.assertEqual(client.request.await_count, 1)
        with patch('app.services.chamak.fetch_chamak_generation', AsyncMock(return_value={'source_image_1_url':'one','source_image_2_url':'two'})), patch('app.services.chamak.fetch_image_bytes_and_content_type', AsyncMock(return_value=(b'image','image/jpeg'))), patch('app.services.chamak.call_openai_vision_analysis', AsyncMock(side_effect=raised.exception)), patch('app.services.chamak.update_chamak_generation', AsyncMock()) as update, patch('app.services.chamak._refund_failed_generation', AsyncMock()):
            await run_stage1_vision_analysis('test-job', {'charged': 0})
        self.assertEqual(update.call_args.args[1]['status'], 'failed')
        self.assertIn('provider credits', update.call_args.args[1]['error_message'])
        self.assertIsNone(update.call_args_list[0].args[1]['error_message'])


class BlendContract(unittest.TestCase):
    def test_slider_snaps_to_five_distinct_steps(self):
        analysis = {'image1_strengths':['floral dome'], 'image2_weaknesses':['flat disc'], 'image2_strengths':['ruby setting']}
        steps = {
            0: 'use Design 1 (floral dome) exactly',
            .1: 'use Design 1 (floral dome) exactly',
            .2: 'keep Design 1 (floral dome) as the clear base; add only a subtle accent from Design 2 (flat disc)',
            .39: 'an even, visible blend of Design 1 (floral dome) and Design 2 (flat disc)',
            .6: 'an even, visible blend of Design 1 (floral dome) and Design 2 (flat disc)',
            .75: 'make Design 2 (flat disc) the clear base; keep only a subtle trace of Design 1 (floral dome)',
            .9: 'use Design 2 (flat disc) completely',
            1: 'use Design 2 (flat disc) completely',
        }
        for weight, expected in steps.items():
            prompt = compile_chamak_prompt(analysis, {'slider_weights': {'attr_0':weight}}, None)
            self.assertIn(expected, prompt)
            self.assertNotIn('% Design', prompt)
            self.assertNotIn('Prominently retain', prompt)

    def test_steps_are_symmetric(self):
        low = compile_chamak_prompt({}, {'slider_weights': {'x': .2}}, None)
        high = compile_chamak_prompt({}, {'slider_weights': {'x': .8}}, None)
        self.assertIn('subtle accent from Design 2', low)
        self.assertIn('subtle trace of Design 1', high)

    def test_strongest_sliders_lead(self):
        prompt = compile_chamak_prompt({}, {'slider_weights': {'shape': .5, 'stone': 1, 'chain': .2}}, None)
        self.assertLess(prompt.index('stone:'), prompt.index('chain:'))
        self.assertLess(prompt.index('chain:'), prompt.index('shape:'))

    def test_old_web_and_normalized_payloads_are_equivalent(self):
        old = compile_chamak_prompt({}, {'Silhouette': 75}, None)
        new = compile_chamak_prompt({}, {'slider_weights': {'Silhouette': .75}}, None)
        self.assertEqual(old, new)
        self.assertIn('Silhouette: make Design 2', new)

    def test_context_and_invalid_weights(self):
        prompt = compile_chamak_prompt({}, {'slider_weights': {'motif': .92, 'bad': float('nan')}, 'attribute_context':[{'id':'motif','attribute':'Motif','image1_feature':'floral','image2_feature':'geometric'}]}, 'Keep gold')
        self.assertIn('Motif: use Design 2 (geometric) completely', prompt)
        self.assertNotIn('nan%', prompt)
        self.assertIn('Keep gold', prompt)
