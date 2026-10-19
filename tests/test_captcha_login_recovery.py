import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.flow_client import FlowClient


class CaptchaLoginRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def run_solve(self, bundles, recovered=True):
        client = FlowClient(None)
        client._browser_session_recovery = AsyncMock(return_value=recovered)
        service = MagicMock()
        service.get_token_bundle = AsyncMock(side_effect=bundles)
        with (
            patch("src.services.flow_client.config") as config,
            patch("src.services.browser_captcha_personal.BrowserCaptchaService.get_instance", AsyncMock(return_value=service)),
        ):
            config.captcha_method = "personal"
            result = await client._get_recaptcha_token("project", action="UPLOAD_IMAGE", token_id=9)
        return client, service, result

    async def test_empty_captcha_recovers_original_browser_and_retries(self):
        bundle = {"token": "captcha", "fingerprint": {"user_agent": "Chrome/test"}, "session_id": "session"}
        client, service, result = await self.run_solve([None, bundle])
        self.assertEqual(result, ("captcha", "personal:session"))
        client._browser_session_recovery.assert_awaited_once_with(9)
        self.assertEqual(service.get_token_bundle.await_count, 2)
        self.assertEqual(client.get_request_fingerprint()["user_agent"], "Chrome/test")

    async def test_valid_captcha_does_not_replace_browser(self):
        client, service, result = await self.run_solve([{"token": "captcha", "fingerprint": {"user_agent": "Chrome/test"}}])
        self.assertEqual(result[0], "captcha")
        client._browser_session_recovery.assert_not_awaited()

    async def test_unavailable_saved_profile_does_not_loop(self):
        client, service, result = await self.run_solve([None], recovered=False)
        self.assertIsNone(result[0])
        service.get_token_bundle.assert_awaited_once()
        client._browser_session_recovery.assert_awaited_once()

    async def test_retry_without_captcha_stops_after_one_recovery(self):
        client, service, result = await self.run_solve([None, None])
        self.assertIsNone(result[0])
        self.assertEqual(service.get_token_bundle.await_count, 2)
        client._browser_session_recovery.assert_awaited_once()
