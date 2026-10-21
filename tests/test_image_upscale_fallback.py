"""Upscale failures must preserve the base image and report actual delivery."""
import json
import types
import unittest
from unittest.mock import AsyncMock, Mock, patch

from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler


class ImageUpscaleFallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.handler = GenerationHandler.__new__(GenerationHandler)
        self.handler._update_request_log_progress = AsyncMock()
        self.handler._get_base_url = Mock(return_value='https://example.com')
        self.handler.file_cache = types.SimpleNamespace(
            cache_base64_image=AsyncMock(return_value='upscaled_2K.jpg'))
        self.media = {'name': 'media-id', 'image': {'generatedImage': {
            'fifeUrl': 'https://example.com/original.jpg'}}}
        self.handler.flow_client = types.SimpleNamespace(
            generate_image=AsyncMock(return_value=({'media': [self.media]}, 'session', {})),
            upsample_image=AsyncMock(),
            _get_retry_reason=FlowClient(None)._get_retry_reason)
        self.token = types.SimpleNamespace(id=1, at='test', user_paygate_tier=None,
                                           image_concurrency=1)
        self.state, self.perf, self.result = {}, {}, {}

    async def run_generation(self, stream=False, upsample=True):
        model = {'model_name': 'BELUGA', 'aspect_ratio': 'IMAGE_ASPECT_RATIO_LANDSCAPE'}
        if upsample:
            model['upsample'] = 'IMAGE_UPSAMPLE_RESOLUTION_2K'
        with patch('src.services.generation_handler.config',
                   types.SimpleNamespace(flow_max_retries=2, cache_enabled=False)):
            chunks = [chunk async for chunk in self.handler._handle_image_generation(
                self.token, 'project', model, 'test', None, stream,
                perf_trace=self.perf, response_state=self.state,
                generation_result=self.result)]
        final = json.loads(chunks[-1][6:] if stream else chunks[-1])
        return (final['choices'][0]['delta']['content'] if stream
                else final['choices'][0]['message']['content'])

    def assert_fallback(self, content, error):
        up = self.state['generated_assets']['upscaled_image']
        self.assertTrue(up['failed'])
        self.assertEqual(up['delivery_mode'], 'origin_fallback')
        self.assertIn(error, up['error'])
        self.assertEqual(self.state['url'], 'https://example.com/original.jpg')
        self.assertTrue(self.result['success'])
        self.assertIn('2K 放大失败，已返回原始分辨率图片', content)
        self.assertIn('![Generated Image](https://example.com/original.jpg)', content)

    async def test_upstream_exception_is_saved_with_original_nonstream(self):
        self.handler.flow_client.upsample_image.side_effect = RuntimeError(
            'Flow frontend RPC rejected: rpc=SPrCad, code=7')
        content = await self.run_generation()
        self.assert_fallback(content, 'rpc=SPrCad, code=7')
        up = self.state['generated_assets']['upscaled_image']
        self.assertEqual(up['attempt_count'], 1)
        attempt = self.perf['image_generation']['upsample_attempts'][0]
        self.assertEqual(attempt['status'], 'failed')
        self.assertEqual(attempt['error'], up['error'])
        self.assertGreaterEqual(attempt['duration_ms'], 0)
        self.handler.file_cache.cache_base64_image.assert_not_awaited()

    async def test_empty_result_is_saved_without_extra_paid_retry_stream(self):
        self.handler.flow_client.upsample_image.return_value = ''
        content = await self.run_generation(stream=True)
        self.assert_fallback(content, '放大接口返回空结果')
        self.handler.flow_client.upsample_image.assert_awaited_once()
        self.assertEqual(self.state['generated_assets']['upscaled_image']['attempt_count'], 1)

    async def test_retry_then_success_preserves_attempts_and_delivers_upscaled_image(self):
        self.handler.flow_client.upsample_image.side_effect = [
            RuntimeError('Failed to obtain reCAPTCHA token for image upsample'), 'base64-data']
        with patch('src.services.generation_handler.asyncio.sleep', new_callable=AsyncMock):
            content = await self.run_generation()
        attempts = self.perf['image_generation']['upsample_attempts']
        self.assertEqual([a['status'] for a in attempts], ['failed', 'success'])
        self.assertIn('reCAPTCHA', attempts[0]['error'])
        self.assertEqual(self.state['url'], 'https://example.com/tmp/upscaled_2K.jpg')
        self.assertNotIn('failed', self.state['generated_assets']['upscaled_image'])
        self.assertNotIn('放大失败', content)
        self.handler.file_cache.cache_base64_image.assert_awaited_once_with('base64-data', '2K')

    async def test_exhausted_retries_save_last_error(self):
        self.handler.flow_client.upsample_image.side_effect = [
            RuntimeError('HTTP 403 first'), RuntimeError('HTTP 403 last')]
        with patch('src.services.generation_handler.asyncio.sleep', new_callable=AsyncMock):
            content = await self.run_generation()
        self.assert_fallback(content, 'HTTP 403 last')
        self.assertEqual(self.state['generated_assets']['upscaled_image']['attempt_count'], 2)

    async def test_missing_media_id_cannot_silently_claim_upscale(self):
        self.media.pop('name')
        content = await self.run_generation()
        self.assert_fallback(content, '缺少 media ID')
        self.handler.flow_client.upsample_image.assert_not_awaited()

    async def test_connect_failures_obey_retry_limit_and_keep_original(self):
        error = 'Failed to perform, curl: (7) Could not connect to server'
        self.handler.flow_client.upsample_image.side_effect = RuntimeError(error)
        with patch('src.services.generation_handler.asyncio.sleep', new_callable=AsyncMock):
            content = await self.run_generation()
        self.assert_fallback(content, error)
        self.assertEqual(self.handler.flow_client.upsample_image.await_count, 2)
        self.handler.flow_client.generate_image.assert_awaited_once()

    async def test_frontend_upscaled_url_is_not_reported_as_fallback(self):
        self.handler.flow_client.upsample_image.return_value = 'https://example.com/2k.jpg'
        content = await self.run_generation(stream=True)
        self.assertEqual(self.state['url'], 'https://example.com/2k.jpg')
        self.assertEqual(self.perf['image_generation']['upsample_attempts'][0]['status'], 'success')
        self.assertNotIn('放大失败', content)

    async def test_base_resolution_request_has_no_upscale_warning(self):
        content = await self.run_generation(upsample=False)
        self.assertNotIn('upscaled_image', self.state['generated_assets'])
        self.assertNotIn('放大失败', content)
        self.handler.flow_client.upsample_image.assert_not_awaited()

    async def test_failed_reference_upload_records_stage_without_submitting_generation(self):
        self.handler.flow_client.upload_image = AsyncMock(side_effect=[
            'first-media', RuntimeError('参考图上传失败：curl: (28) timed out'),
        ])
        model = {'model_name': 'GEM_PIX_2', 'aspect_ratio': 'IMAGE_ASPECT_RATIO_LANDSCAPE'}
        with self.assertRaisesRegex(RuntimeError, '参考图上传失败'):
            async for _ in self.handler._handle_image_generation(
                self.token, 'project', model, 'test', [b'first', b'second'], False,
                perf_trace=self.perf, response_state=self.state,
            ):
                pass
        trace = self.perf['image_generation']
        self.assertEqual(trace['upload_failed_image_index'], 2)
        self.assertEqual(trace['uploaded_image_count'], 1)
        self.assertGreaterEqual(trace['upload_images_ms'], 0)
        self.assertNotIn('generate_api_ms', trace)
        self.handler.flow_client.generate_image.assert_not_awaited()
        self.handler.flow_client.upsample_image.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
