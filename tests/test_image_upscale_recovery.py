"""Rejected upscale captchas must recover their exact tab before retrying."""

import types
import unittest
from unittest.mock import AsyncMock, patch

from src.services.flow_client import FlowClient
from src.services.generation_handler import GenerationHandler


class ImageUpscaleRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FlowClient(None)
        self.client._get_runtime_config = lambda: types.SimpleNamespace(
            upsample_timeout=90
        )
        self.events = []
        self.service = types.SimpleNamespace(report_flow_error=AsyncMock())

        async def solve(*args, **kwargs):
            self.client._set_captcha_request_context({
                "method": "personal", "browser_ref": None,
                "token_id": 7, "project_id": "project",
                "slot_id": "b0-slot-1", "issued_at": 100.0,
            })
            return f"captcha-{self.client._get_recaptcha_token.await_count}", None

        async def report(**kwargs):
            self.events.append("recover")
            self.assertEqual(
                self.client._get_captcha_request_context()["slot_id"],
                "b0-slot-1",
            )

        self.service.report_flow_error.side_effect = report
        self.client._get_recaptcha_token = AsyncMock(side_effect=solve)
        self.client._current_rpc = AsyncMock()

    async def upsample(self):
        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService.get_instance",
            new=AsyncMock(return_value=self.service),
        ):
            return await self.client.upsample_image(
                "access", "project", "media", token_id=7
            )

    async def test_rejection_recovers_exact_solve_before_context_is_cleared(self):
        error = RuntimeError(
            "Flow frontend RPC rejected: rpc=SPrCad, "
            "error=PUBLIC_ERROR_UNUSUAL_ACTIVITY"
        )
        self.client._current_rpc.side_effect = error
        with self.assertRaises(RuntimeError) as caught:
            await self.upsample()
        self.assertIs(caught.exception, error)
        self.service.report_flow_error.assert_awaited_once_with(
            project_id="project", error_reason="Google 风控拒绝",
            error_message=str(error), token_id=7, slot_id="b0-slot-1",
            issued_at=100.0,
        )
        self.assertEqual(self.events, ["recover"])
        self.assertEqual(self.client._get_captcha_request_context(), {})
        # The outer generation handler owns the retry budget.
        self.client._current_rpc.assert_awaited_once()

    async def test_success_does_not_recover_browser(self):
        url = "https://flow.google.com/api/content/image/media-id"
        self.client._current_rpc.return_value = [url]
        self.assertEqual(await self.upsample(), url)
        self.service.report_flow_error.assert_not_awaited()
        self.assertEqual(self.client._get_captcha_request_context(), {})

    async def test_unusual_activity_has_specific_retry_reason(self):
        self.assertEqual(self.client._get_retry_reason(
            "PUBLIC_ERROR_UNUSUAL_ACTIVITY_TOO_MUCH_TRAFFIC"
        ), "Google 风控拒绝")

    async def test_connect_failure_retries_upscale_with_fresh_captcha(self):
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = self.client
        handler._update_request_log_progress = AsyncMock()
        media = {"name": "media", "image": {"generatedImage": {
            "fifeUrl": "https://flow-content.google/image/original",
        }}}
        self.client.generate_image = AsyncMock(return_value=({"media": [media]}, "session", {}))
        self.client._current_rpc.side_effect = [
            RuntimeError("Failed to perform, curl: (7) Failed to connect to flow.google.com port 443: Could not connect to server"),
            ["https://flow-content.google/image/upscaled"],
        ]
        token = types.SimpleNamespace(id=7, at="access", image_concurrency=1,
                                      user_paygate_tier=None)
        state, perf = {}, {}
        with (
            patch("src.services.browser_captcha_personal.BrowserCaptchaService.get_instance",
                  AsyncMock(return_value=self.service)),
            patch("src.services.generation_handler.config", types.SimpleNamespace(
                flow_max_retries=3, cache_enabled=False,
            )),
            patch("src.services.generation_handler.asyncio.sleep", AsyncMock()),
        ):
            chunks = [chunk async for chunk in handler._handle_image_generation(
                token, "project", {
                    "model_name": "GEM_PIX_2",
                    "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
                    "upsample": "IMAGE_UPSAMPLE_RESOLUTION_2K",
                }, "test", None, False, response_state=state, perf_trace=perf,
            )]
        self.assertTrue(chunks)
        self.assertEqual(state["url"], "https://flow-content.google/image/upscaled")
        self.assertEqual(
            [call.kwargs["argument"][2][10][0]
             for call in self.client._current_rpc.await_args_list],
            ["captcha-1", "captcha-2"],
        )
        self.assertEqual(perf["image_generation"]["upsample_attempts"][0]["retry_reason"],
                         "网络/TLS错误")
        self.client.generate_image.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
