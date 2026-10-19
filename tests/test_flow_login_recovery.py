import unittest
from unittest.mock import AsyncMock, patch
from urllib.parse import parse_qs, urlparse

from src.services.flow_login_recovery import FLOW_URL, GoogleLoginRequired, recover_flow_identity


EMAIL = "account@example.com"
FLOW = {"host": "flow.google.com", "ready": "complete", "email": EMAIL}
ANONYMOUS = {"host": "flow.google.com", "ready": "complete", "email": ""}
PASSWORD = {"host": "accounts.google.com", "ready": "complete", "needs_login": True}


class FlowLoginRecoveryTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.sleep = patch("src.services.flow_login_recovery.asyncio.sleep", new_callable=AsyncMock)
        self.sleep.start()
        self.addCleanup(self.sleep.stop)

    async def test_valid_flow_does_not_visit_google_signin(self):
        navigate = AsyncMock()
        result = await recover_flow_identity(navigate, AsyncMock(return_value=FLOW), EMAIL)
        self.assertEqual(result["email"], EMAIL)
        navigate.assert_awaited_once_with(FLOW_URL)

    async def test_google_login_restores_flow_without_entering_credentials(self):
        navigate = AsyncMock()
        read = AsyncMock(side_effect=[ANONYMOUS] * 3 + [PASSWORD, FLOW])
        self.assertEqual((await recover_flow_identity(navigate, read, EMAIL))["email"], EMAIL)
        self.assertEqual(navigate.await_count, 2)
        signin = urlparse(navigate.call_args.args[0])
        self.assertEqual(signin.hostname, "accounts.google.com")
        query = parse_qs(signin.query)
        self.assertEqual(query["osid"], ["1"])
        self.assertEqual(query["continue"], [FLOW_URL])
        self.assertEqual(query["login_hint"], [EMAIL])

    async def test_password_or_verification_requires_manual_login(self):
        with self.assertRaises(GoogleLoginRequired):
            await recover_flow_identity(AsyncMock(), AsyncMock(side_effect=[ANONYMOUS] * 3 + [PASSWORD] * 3), EMAIL)

    async def test_wrong_account_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "账号与当前 Token 不一致"):
            await recover_flow_identity(AsyncMock(), AsyncMock(return_value=FLOW), "other@example.com")

    async def test_wrong_account_after_sso_is_rejected(self):
        with self.assertRaisesRegex(RuntimeError, "账号与当前 Token 不一致"):
            await recover_flow_identity(AsyncMock(), AsyncMock(side_effect=[ANONYMOUS] * 3 + [FLOW]), "other@example.com")

    async def test_incomplete_page_is_timeout_not_expired_cookie(self):
        with self.assertRaises(TimeoutError):
            await recover_flow_identity(AsyncMock(), AsyncMock(return_value={"host": "accounts.google.com", "ready": "loading"}), EMAIL)

    async def test_anonymous_flow_after_sso_is_not_google_login_failure(self):
        with self.assertRaises(TimeoutError):
            await recover_flow_identity(AsyncMock(), AsyncMock(return_value=ANONYMOUS), EMAIL)

    async def test_network_error_does_not_become_login_required(self):
        with self.assertRaises(ConnectionError):
            await recover_flow_identity(AsyncMock(side_effect=ConnectionError("connection reset")), AsyncMock(), EMAIL)
