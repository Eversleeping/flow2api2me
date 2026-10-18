import asyncio
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from src.api import admin
from src.api.browser_login import router, InputRequest
from src.core.database import Database
from src.core.config import config
from src.core.models import Token
from src.services.browser_login import BrowserLoginService, BrowserLoginError, LoginSession
from src.services.token_manager import TokenManager


EMAIL = "account@example.com"
COOKIES = [
    {"name": "OSID", "value": "new-session", "domain": "flow.google.com", "path": "/", "secure": True, "httpOnly": True},
    {"name": "SID", "value": "new-account", "domain": ".google.com", "path": "/", "secure": True},
    {"name": "irrelevant", "value": "excluded", "domain": ".example.com"},
]


class BrowserLoginTests(unittest.IsolatedAsyncioTestCase):
    def test_google_login_requests_flow_session_and_explicit_return_targets(self):
        parsed = urlparse(BrowserLoginService.SIGNIN_URL)
        self.assertEqual(parsed.hostname, "accounts.google.com")
        query = parse_qs(parsed.query)
        self.assertEqual(query["osid"], ["1"])
        for field in ("continue", "followup"):
            self.assertEqual(query[field], ["https://flow.google.com/projects"])

    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.db = Database(db_path=self.temp.name + "/flow.db")
        await self.db.init_db()
        self.token_id = await self.db.add_token(Token(
            st="old-st", at="old-at", email=EMAIL, login_account=EMAIL,
            google_cookies="OSID=old-session; SID=old-account", is_active=False,
            current_project_id="existing-project", credits=11,
        ))
        flow = MagicMock()
        flow.get_request_fingerprint.return_value = None
        flow.st_to_at = AsyncMock(return_value={
            "access_token": "flow-frontend-cookie", "expires": "2027-01-01T00:00:00Z",
            "credits": 42, "userPaygateTier": "PAYGATE_TIER_ONE",
        })
        self.manager = TokenManager(self.db, flow)
        self.service = BrowserLoginService()
        self.session = LoginSession("session-1", "owner-1", self.token_id, EMAIL,
                                    time.monotonic() + 120, state="ready")
        self.session.tab = MagicMock()
        self.session.tab.evaluate = AsyncMock(return_value={"host": "flow.google.com", "email": EMAIL})
        self.session.browser = MagicMock()
        self.session.browser.cookies.get_all = AsyncMock(return_value=COOKIES)
        self.session.browser.aclose = AsyncMock()
        self.session.browser._process = None
        self.service._session = self.session
        self.service._active_tab = AsyncMock(return_value=self.session.tab)

    async def asyncTearDown(self):
        await self.service.close()
        self.temp.cleanup()

    async def test_other_admin_cannot_view_or_control_login(self):
        for operation in (
            self.service.status("session-1", "other"),
            self.service.screenshot("session-1", "other"),
            self.service.input("session-1", "other", InputRequest(kind="text", text="private")),
            self.service.complete("session-1", "other", self.manager),
            self.service.cancel("session-1", "other"),
        ):
            with self.assertRaises(BrowserLoginError) as error:
                await operation
            self.assertEqual(error.exception.status_code, 404)
        self.session.tab.evaluate.assert_not_awaited()

    async def test_expired_session_is_rejected(self):
        self.session.deadline = time.monotonic() - 1
        with self.assertRaises(BrowserLoginError) as error:
            await self.service.screenshot("session-1", "owner-1")
        self.assertEqual(error.exception.status_code, 410)

    async def test_current_page_domain_is_visible_without_login_parameters(self):
        self.session.tab.evaluate.return_value = "https://openai.com/?private=secret"
        self.session.tab.send = AsyncMock(return_value="anBlZw==")
        self.assertEqual(await self.service.screenshot("session-1", "owner-1"), b"jpeg")
        state = await self.service.status("session-1", "owner-1")
        self.assertEqual(state["page_origin"], "https://openai.com")
        self.assertNotIn("secret", json.dumps(state))

    async def test_duplicate_start_reuses_session_but_other_owner_is_denied(self):
        result = await self.service.start("owner-1", self.token_id, self.db)
        self.assertEqual(result["id"], "session-1")
        self.assertNotIn("owner", result)
        with self.assertRaises(BrowserLoginError) as error:
            await self.service.start("other", self.token_id, self.db)
        self.assertEqual(error.exception.status_code, 409)

    async def test_wrong_google_account_does_not_read_or_save_cookies(self):
        self.session.tab.evaluate.return_value["email"] = "other@example.com"
        with self.assertRaises(BrowserLoginError):
            await self.service.complete("session-1", "owner-1", self.manager)
        self.session.browser.cookies.get_all.assert_not_awaited()
        token = await self.db.get_token(self.token_id)
        self.assertFalse(token.is_active)
        self.assertEqual(token.at, "old-at")

    async def test_anonymous_or_other_origin_cannot_be_saved(self):
        for identity in ({"host": "accounts.google.com", "email": EMAIL},
                         {"host": "flow.google.com", "email": ""}):
            self.session.tab.evaluate.return_value = identity
            with self.assertRaises(BrowserLoginError):
                await self.service.complete("session-1", "owner-1", self.manager)
        self.manager.flow_client.st_to_at.assert_not_awaited()

    async def test_rpc_rejection_preserves_disabled_account_and_old_cookie(self):
        self.manager.flow_client.st_to_at.side_effect = RuntimeError("401")
        with self.assertRaises(RuntimeError):
            await self.service.complete("session-1", "owner-1", self.manager)
        token = await self.db.get_token(self.token_id)
        self.assertFalse(token.is_active)
        self.assertEqual(token.google_cookies, "OSID=old-session; SID=old-account")
        self.assertEqual(self.session.state, "ready")

    async def test_success_validates_and_saves_full_scope_then_enables_account(self):
        concurrency = MagicMock(reset_token=AsyncMock())
        browser = self.session.browser
        result = await self.service.complete("session-1", "owner-1", self.manager, concurrency)
        token = await self.db.get_token(self.token_id)
        self.assertTrue(token.is_active)
        self.assertEqual(token.credits, 42)
        self.assertEqual(token.last_st_refresh_result, "success")
        self.assertEqual(token.current_project_id, "existing-project")
        stored = json.loads(token.google_cookies)
        self.assertEqual({c["name"] for c in stored}, {"OSID", "SID"})
        self.assertEqual(next(c for c in stored if c["name"] == "OSID")["domain"], "flow.google.com")
        self.assertEqual(self.manager.flow_client.st_to_at.call_args.kwargs["google_cookies"], token.google_cookies)
        self.assertEqual(result["email"], EMAIL)
        self.assertNotIn("google_cookies", result)
        browser.stop.assert_called_once()
        self.assertIsNone(self.session.browser)
        concurrency.reset_token.assert_awaited_once()

    async def test_new_login_for_existing_email_updates_instead_of_duplicating(self):
        self.session.token_id = None
        self.session.expected_email = ""
        result = await self.service.complete("session-1", "owner-1", self.manager)
        self.assertEqual(result["token_id"], self.token_id)
        self.assertEqual(len(await self.db.get_all_tokens()), 1)

    async def test_personal_save_transfers_original_browser_and_profile(self):
        self.session.profile = tempfile.mkdtemp(dir=self.temp.name)
        profile, browser, tab = self.session.profile, self.session.browser, self.session.tab
        worker = MagicMock(adopt_login_browser=AsyncMock())
        with patch.object(type(config), "captcha_method", new_callable=PropertyMock, return_value="personal"), patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService.get_instance",
            new=AsyncMock(return_value=worker),
        ):
            result = await self.service.complete("session-1", "owner-1", self.manager)
        args = worker.adopt_login_browser.await_args.args
        self.assertIs(args[0], browser)
        self.assertIs(args[1], tab)
        self.assertEqual(args[2], profile)
        self.assertEqual(args[3].id, self.token_id)
        self.assertTrue(Path(profile).exists())
        self.assertIsNone(self.session.browser)
        self.assertIsNone(self.session.profile)
        browser.stop.assert_not_called()
        await self.service.cancel("session-1", "owner-1")
        await self.service.close_owner("owner-1")
        browser.stop.assert_not_called()
        self.assertIn("原登录浏览器", result["message"])

    async def test_failed_handoff_keeps_source_browser_available_for_retry(self):
        self.session.profile = tempfile.mkdtemp(dir=self.temp.name)
        profile, browser = self.session.profile, self.session.browser
        worker = MagicMock(adopt_login_browser=AsyncMock(side_effect=RuntimeError("busy")))
        with patch.object(type(config), "captcha_method", new_callable=PropertyMock, return_value="personal"), patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService.get_instance",
            new=AsyncMock(return_value=worker),
        ):
            with self.assertRaises(BrowserLoginError) as error:
                await self.service.complete("session-1", "owner-1", self.manager)
        self.assertEqual(error.exception.status_code, 409)
        self.assertEqual(self.session.state, "ready")
        self.assertIs(self.session.browser, browser)
        self.assertTrue(Path(profile).exists())
        browser.stop.assert_not_called()

    async def test_new_cookie_push_after_save_prevents_stale_browser_handoff(self):
        self.session.profile = tempfile.mkdtemp(dir=self.temp.name)
        async def push(*args):
            await self.db.update_token(self.token_id, google_cookies="OSID=newer-push")
        concurrency = MagicMock(reset_token=AsyncMock(side_effect=push))
        with patch.object(type(config), "captcha_method", new_callable=PropertyMock, return_value="personal"), patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService.get_instance",
            new=AsyncMock(),
        ) as get_worker:
            with self.assertRaises(BrowserLoginError) as error:
                await self.service.complete("session-1", "owner-1", self.manager, concurrency)
        self.assertEqual(error.exception.status_code, 409)
        get_worker.assert_not_awaited()
        self.assertEqual((await self.db.get_token(self.token_id)).google_cookies, "OSID=newer-push")
        self.assertEqual(self.session.state, "ready")

    async def test_chromium_stringified_identity_is_accepted(self):
        self.session.tab.evaluate.return_value = json.dumps({"host": "flow.google.com", "email": EMAIL})
        result = await self.service.complete("session-1", "owner-1", self.manager)
        self.assertEqual(result["token_id"], self.token_id)

    async def test_new_account_uses_normal_account_creation(self):
        self.session.token_id = None
        self.session.expected_email = ""
        self.session.tab.evaluate.return_value["email"] = "new@example.com"
        self.manager.add_token = AsyncMock(return_value=Token(id=99, st="new", email="new@example.com"))
        result = await self.service.complete("session-1", "owner-1", self.manager)
        self.assertEqual(result["token_id"], 99)
        self.assertEqual(self.manager.add_token.call_args.kwargs["login_account"], "new@example.com")

    async def test_cookie_push_during_validation_is_not_overwritten(self):
        async def push(*args, **kwargs):
            await self.db.update_token(self.token_id, google_cookies="OSID=newer")
            return {"access_token": "validated"}
        self.manager.flow_client.st_to_at.side_effect = push
        with self.assertRaises(BrowserLoginError):
            await self.service.complete("session-1", "owner-1", self.manager)
        token = await self.db.get_token(self.token_id)
        self.assertEqual(token.google_cookies, "OSID=newer")
        self.assertFalse(token.is_active)

    async def test_cancel_and_expiry_remove_profile_and_close_browser(self):
        self.session.profile = tempfile.mkdtemp(dir=self.temp.name)
        profile = Path(self.session.profile)
        (profile / "private-cookie").write_text("private")
        browser = self.session.browser
        await self.service.cancel("session-1", "owner-1")
        self.assertFalse(profile.exists())
        browser.stop.assert_called_once()
        self.assertEqual(self.session.state, "closed")

    async def test_timeout_closes_session_without_frontend_polling(self):
        self.session.deadline = time.monotonic()
        await self.service._expire(self.session)
        self.assertIsNone(self.session.browser)

    async def test_logout_only_closes_owners_session(self):
        await self.service.close_owner("other")
        self.assertIsNotNone(self.session.browser)
        await self.service.close_owner("owner-1")
        self.assertIsNone(self.session.browser)

    async def test_cancel_while_starting_cancels_boot_and_expiry_tasks(self):
        await self.service.close()
        self.service._session = None
        booting = asyncio.Event()
        async def launch(session):
            booting.set()
            await asyncio.sleep(100)
        self.service._launch = launch
        state = await self.service.start("owner-1", self.token_id, self.db)
        await booting.wait()
        session = self.service._session
        await self.service.cancel(state["id"], "owner-1")
        self.assertTrue(session.boot_task.cancelled())
        self.assertTrue(session.expiry_task.cancelled())
        self.assertEqual(session.state, "closed")

    async def test_cancel_during_chromium_start_closes_partial_process_owner(self):
        await self.service.close()
        self.service._session = None
        starting = asyncio.Event()
        async def start_browser():
            starting.set()
            await asyncio.sleep(100)
        browser = MagicMock()
        browser.start = AsyncMock(side_effect=start_browser)
        browser.aclose = AsyncMock()
        browser._process = None
        profile = tempfile.mkdtemp(dir=self.temp.name)
        with (
            patch.dict('os.environ', {'DISPLAY': ':99', 'BROWSER_EXECUTABLE_PATH': sys.executable}),
            patch('nodriver.Browser', return_value=browser),
            patch('src.services.browser_captcha_personal._patch_nodriver_runtime'),
            patch('src.services.browser_login.tempfile.mkdtemp', return_value=profile),
        ):
            state = await self.service.start('owner-1', self.token_id, self.db)
            await asyncio.wait_for(starting.wait(), 2)
            await self.service.cancel(state['id'], 'owner-1')
        browser.stop.assert_called_once()
        self.assertFalse(Path(profile).exists())


class BrowserLoginAPITests(unittest.TestCase):
    def setUp(self):
        self.app = FastAPI()
        self.app.include_router(router)
        self.client = TestClient(self.app)
        self.owner = "test-admin-browser-login"
        admin.active_admin_tokens.add(self.owner)
        self.headers = {"Authorization": "Bearer " + self.owner, "X-Browser-Login": "1", "Origin": "http://testserver"}

    def tearDown(self):
        admin.active_admin_tokens.discard(self.owner)

    def test_unauthenticated_control_is_denied(self):
        response = self.client.post('/api/browser-login/start', json={})
        self.assertEqual(response.status_code, 401)

    def test_cross_origin_is_denied_even_for_authenticated_admin(self):
        headers = {**self.headers, "Origin": "https://other.example"}
        response = self.client.get('/api/browser-login/s/status', headers=headers)
        self.assertEqual(response.status_code, 403)

    def test_missing_control_header_is_denied(self):
        response = self.client.get('/api/browser-login/s/screenshot', headers={"Authorization": "Bearer " + self.owner})
        self.assertEqual(response.status_code, 403)

    def test_https_origin_works_behind_tls_reverse_proxy(self):
        headers = {**self.headers, "Origin": "https://testserver", "X-Forwarded-Proto": "https"}
        with patch('src.api.browser_login.browser_login_service.status', AsyncMock(return_value={"state": "ready"})):
            response = self.client.get('/api/browser-login/s/status', headers=headers)
        self.assertEqual(response.status_code, 200)

    def test_forwarded_host_cannot_authorize_another_origin(self):
        headers = {**self.headers, "Origin": "https://other.example",
                   "X-Forwarded-Proto": "https", "X-Forwarded-Host": "other.example"}
        response = self.client.get('/api/browser-login/s/status', headers=headers)
        self.assertEqual(response.status_code, 403)

    def test_screenshots_are_private_uncached_bytes(self):
        with patch('src.api.browser_login.browser_login_service.screenshot', AsyncMock(return_value=b'jpeg')) as call:
            response = self.client.get('/api/browser-login/s/screenshot', headers=self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b'jpeg')
        self.assertEqual(response.headers['cache-control'], 'no-store')
        self.assertEqual(response.headers['content-type'], 'image/jpeg')
        call.assert_awaited_once_with('s', self.owner)

    def test_input_payload_cannot_supply_urls_or_javascript_commands(self):
        for payload in ({"kind": "navigate", "url": "http://127.0.0.1/"},
                        {"kind": "click", "x": 1300}, {"kind": "text", "text": "a" * 4097}):
            response = self.client.post('/api/browser-login/s/input', json=payload, headers=self.headers)
            self.assertEqual(response.status_code, 422)

    def test_backend_error_details_are_not_exposed(self):
        with patch('src.api.browser_login.browser_login_service.complete', AsyncMock(side_effect=RuntimeError('cookie=private-secret'))):
            response = self.client.post('/api/browser-login/s/complete', json={}, headers=self.headers)
        self.assertEqual(response.status_code, 502)
        self.assertNotIn('private-secret', response.text)


if __name__ == '__main__':
    unittest.main()
