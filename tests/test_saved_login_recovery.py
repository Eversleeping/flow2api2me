import asyncio
import json
import time
import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.browser_login import BrowserLoginService
from src.services.flow_login_recovery import GoogleLoginRequired


EMAIL = "account@example.com"


class SavedLoginRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.service = BrowserLoginService()
        self.token = types.SimpleNamespace(id=9, email=EMAIL, login_account=EMAIL)
        self.profile = patch("src.services.browser_login.find_retained_login_profile", return_value="saved-profile")
        self.profile_mock = self.profile.start()
        self.addCleanup(self.profile.stop)
        self.service._dispose = AsyncMock()
        self.browser = MagicMock()
        self.browser.cookies.get_all = AsyncMock(return_value=[
            {"name": "OSID", "value": "new", "domain": "flow.google.com", "path": "/", "secure": True},
            {"name": "ignored", "value": "secret", "domain": "example.com", "path": "/"},
        ])
        self.tab = MagicMock()
        self.tab.get = AsyncMock()
        self.tab.evaluate = AsyncMock(return_value=json.dumps({"host": "flow.google.com", "ready": "complete", "email": EMAIL}))

        async def launch(session):
            session.state = "ready"
            session.tab = self.tab
            session.browser = self.browser
            session.profile = "saved-profile"
        self.service._launch = AsyncMock(side_effect=launch)

    async def test_restores_only_saved_account_and_cleans_up_browser(self):
        result = await self.service.recover_saved_login(self.token)
        self.assertEqual(result["email"], EMAIL)
        self.assertEqual([c["name"] for c in json.loads(result["google_cookies"])], ["OSID"])
        self.profile_mock.assert_called_once_with(9, EMAIL)
        self.service._dispose.assert_awaited_once()

    async def test_no_saved_profile_does_not_start_empty_browser(self):
        self.profile_mock.return_value = None
        self.assertIsNone(await self.service.recover_saved_login(self.token))
        self.service._launch.assert_not_awaited()

    async def test_manual_login_dialog_is_left_untouched(self):
        self.service._session = types.SimpleNamespace(state="ready", deadline=time.monotonic() + 60)
        self.assertIsNone(await self.service.recover_saved_login(self.token))
        self.profile_mock.assert_not_called()
        self.service._launch.assert_not_awaited()

    async def test_wrong_account_never_exports_cookies(self):
        self.tab.evaluate.return_value = json.dumps({"host": "flow.google.com", "ready": "complete", "email": "other@example.com"})
        with self.assertRaisesRegex(RuntimeError, "账号与当前 Token 不一致"):
            await self.service.recover_saved_login(self.token)
        self.browser.cookies.get_all.assert_not_awaited()
        self.service._dispose.assert_awaited_once()

    async def test_cancelled_recovery_always_closes_browser(self):
        self.tab.get.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.service.recover_saved_login(self.token)
        self.service._dispose.assert_awaited_once()

    async def test_google_challenge_is_preserved_for_caller(self):
        with patch("src.services.browser_login.recover_flow_identity", AsyncMock(side_effect=GoogleLoginRequired("二次验证"))):
            with self.assertRaises(GoogleLoginRequired):
                await self.service.recover_saved_login(self.token)
        self.browser.cookies.get_all.assert_not_awaited()
        self.service._dispose.assert_awaited_once()

    async def test_validated_browser_is_handed_to_captcha_instead_of_closed(self):
        manager = MagicMock()
        manager.db.get_token = AsyncMock()
        async def save(token_id, token, result):
            manager.db.get_token.return_value = types.SimpleNamespace(google_cookies=result["google_cookies"])
            return "new-st"
        manager._save_refreshed_cookies = AsyncMock(side_effect=save)
        captcha = MagicMock()
        captcha.adopt_login_browser = AsyncMock()
        with patch("src.services.browser_captcha_personal.BrowserCaptchaService.get_instance", AsyncMock(return_value=captcha)):
            result = await self.service.recover_saved_login(self.token, manager=manager)
        self.assertEqual(result["_saved_st"], "new-st")
        manager._save_refreshed_cookies.assert_awaited_once()
        captcha.adopt_login_browser.assert_awaited_once_with(self.browser, self.tab, "saved-profile", manager.db.get_token.return_value)
        disposed_session = self.service._dispose.call_args.args[0]
        self.assertIsNone(disposed_session.browser)
        self.assertIsNone(disposed_session.profile)

    async def test_failed_validation_never_hands_browser_to_captcha(self):
        manager = MagicMock()
        manager._save_refreshed_cookies = AsyncMock(side_effect=RuntimeError("rpc=401"))
        with patch("src.services.browser_captcha_personal.BrowserCaptchaService.get_instance", new_callable=AsyncMock) as get_captcha:
            with self.assertRaisesRegex(RuntimeError, "401"):
                await self.service.recover_saved_login(self.token, manager=manager)
        get_captcha.assert_not_awaited()
        self.assertIs(self.service._dispose.call_args.args[0].browser, self.browser)
