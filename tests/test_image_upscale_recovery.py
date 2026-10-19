"""Rejected upscale captchas must recover their exact tab before retrying."""

import types
import unittest
from unittest.mock import AsyncMock, patch

from src.services.flow_client import FlowClient


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
            return "captcha", None

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


if __name__ == "__main__":
    unittest.main()
