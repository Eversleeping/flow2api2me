"""A client stopping at the final result must not strand logs or accounting."""
import asyncio
import json
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.core.database import Database
from src.core.models import Token
from src.services.generation_handler import GenerationHandler


class GenerationCompletionTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(db_path=self.temp.name + '/flow.db')
        await self.db.init_db()
        self.token = Token(st='test', email='test@example.com', current_project_id='project')
        self.token.id = await self.db.add_token(self.token)
        self.handler = GenerationHandler.__new__(GenerationHandler)
        self.handler.db = self.db
        self.handler.flow_client = MagicMock()
        self.handler.flow_client.prefill_remote_browser_pool = AsyncMock()
        self.handler.load_balancer = types.SimpleNamespace(
            select_token=AsyncMock(return_value=self.token), release_pending=AsyncMock())
        self.handler.token_manager = types.SimpleNamespace(
            ensure_valid_token=AsyncMock(return_value=self.token),
            ensure_project_exists=AsyncMock(return_value='project'),
            record_usage=AsyncMock(), record_success=AsyncMock(), record_error=AsyncMock())
        self.pipeline_drained = False

        async def generate(*args, **kwargs):
            state = kwargs['request_log_state']
            await self.handler._update_request_log_progress(
                state, token_id=self.token.id, status_text='image_generated', progress=72)
            kwargs['response_state']['url'] = 'https://example.com/result.jpg'
            kwargs['response_state']['generated_assets'] = {'type': 'image', 'final_image_url': 'https://example.com/result.jpg'}
            self.handler._mark_generation_succeeded(kwargs['generation_result'])
            stream = args[5]
            yield (self.handler._create_stream_chunk('![Image](https://example.com/result.jpg)', finish_reason='stop')
                   if stream else self.handler._create_completion_response('https://example.com/result.jpg'))
            self.pipeline_drained = True

        self.handler._handle_image_generation = generate
        self.handler._handle_video_generation = generate

    async def asyncTearDown(self):
        self.temp.cleanup()

    async def _receive_final(self, model='gemini-3.0-pro-image-landscape', stream=True):
        generator = self.handler.handle_generation(model=model, prompt='test', stream=stream)
        with patch('src.services.generation_handler.supports_model_for_tier', return_value=True):
            async for chunk in generator:
                data = json.loads(chunk[6:] if chunk.startswith('data: ') else chunk)
                if data.get('choices', [{}])[0].get('finish_reason') == 'stop':
                    return generator
        self.fail('No final result')

    async def _assert_finalized(self):
        logs = await self.db.get_logs(limit=10, include_payload=True)
        self.assertEqual(len(logs), 1)
        log = logs[0]
        self.assertEqual(log['status_code'], 200)
        self.assertEqual(log['status_text'], 'completed')
        self.assertEqual(log['progress'], 100)
        self.assertGreater(log['duration'], 0)
        self.assertEqual(json.loads(log['response_body'])['url'], 'https://example.com/result.jpg')
        self.assertTrue(self.pipeline_drained)
        self.handler.token_manager.record_usage.assert_awaited_once()
        self.handler.token_manager.record_success.assert_awaited_once()
        self.handler.load_balancer.release_pending.assert_awaited_once()

    async def test_stream_is_finalized_before_client_receives_stop(self):
        generator = await self._receive_final()
        await self._assert_finalized()
        await generator.aclose()
        await self._assert_finalized()

    async def test_non_stream_is_finalized_before_result(self):
        generator = await self._receive_final(stream=False)
        await self._assert_finalized()
        await generator.aclose()

    async def test_video_stream_is_finalized_before_stop(self):
        generator = await self._receive_final(model='veo-3.1-fast')
        await self._assert_finalized()
        await generator.aclose()

    async def test_disconnect_after_result_cannot_replace_success_with_499(self):
        generator = await self._receive_final()
        with self.assertRaises(asyncio.CancelledError):
            await generator.athrow(asyncio.CancelledError())
        await self._assert_finalized()
        self.handler.token_manager.record_error.assert_not_awaited()


if __name__ == '__main__':
    unittest.main()
