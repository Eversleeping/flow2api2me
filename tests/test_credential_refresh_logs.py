"""Manual credential refresh outcomes must be visible in persistent admin logs."""

import json
import tempfile
import types
import unittest
from unittest.mock import AsyncMock, PropertyMock, patch

import httpx
from fastapi import FastAPI, HTTPException

from src.api import admin
from src.core.database import Database
from src.core.models import Token


class CredentialRefreshLogTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.db = Database(self.temp.name + "/flow.db")
        await self.db.init_db()
        self.token_id = await self.db.add_token(Token(
            st="private-session", at="private-access", email="account@example.com",
            google_cookies="SID=private-cookie", login_password="private-password",
        ))
        self.manager = types.SimpleNamespace(
            get_token=self.db.get_token,
            refresh_cookies=AsyncMock(return_value=True),
        )
        self.enterContext(patch.object(admin, "db", self.db))
        self.enterContext(patch.object(admin, "token_manager", self.manager))
        self.enterContext(patch.object(
            type(admin.config), "debug_enabled", new_callable=PropertyMock,
            return_value=False,
        ))
        self.app = FastAPI()
        self.app.include_router(admin.router)

        async def authenticated():
            return "fixture-admin"

        self.app.dependency_overrides[admin.verify_admin_token] = authenticated
        self.client = await self.enterAsyncContext(httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app), base_url="http://localhost",
        ))

    async def refresh(self, token_id=None):
        return await self.client.post(f"/api/tokens/{token_id or self.token_id}/refresh-at")

    async def assert_log(self, status_code, error=None):
        rows = await self.db.get_logs(include_payload=True)
        self.assertEqual(len(rows), 1)
        row = rows[0]
        self.assertEqual(row["operation"], "refresh_credentials")
        self.assertEqual(row["token_id"], self.token_id)
        self.assertEqual(row["token_email"], "account@example.com")
        self.assertEqual(row["status_code"], status_code)
        self.assertEqual(row["status_text"], "completed" if status_code == 200 else "failed")
        self.assertGreater(row["duration"], 0)
        self.assertEqual(json.loads(row["request_body"]), {
            "token_id": self.token_id, "source": "manual",
        })
        if error:
            self.assertIn(error, json.loads(row["response_body"])["error"])
        for secret in ("private-session", "private-access", "private-cookie", "private-password"):
            self.assertNotIn(secret, row["request_body"] + row["response_body"])
        return row

    async def test_success_is_persisted_without_debug_and_visible_in_log_api(self):
        before = await self.db.get_dashboard_stats()
        response = await self.refresh()
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["success"])
        row = await self.assert_log(200)
        self.assertEqual(row["progress"], 100)
        self.assertEqual(json.loads(row["response_body"]), response.json())
        listed = (await self.client.get("/api/logs")).json()
        self.assertEqual(listed[0]["id"], row["id"])
        detail = (await self.client.get(f"/api/logs/{row['id']}")).json()
        self.assertIn("Cookie 刷新并验证成功", detail["response_body"])
        self.assertEqual(await self.db.get_dashboard_stats(), before)

    async def test_failed_refresh_records_reason_in_list_and_detail(self):
        self.manager.refresh_cookies.return_value = False
        await self.db.update_token(self.token_id, last_st_refresh_result="GoogleLoginRequired: 需要重新登录")
        response = await self.refresh()
        self.assertEqual(response.status_code, 500)
        row = await self.assert_log(500, "需要重新登录")
        listed = (await self.client.get("/api/logs")).json()
        self.assertIn("需要重新登录", listed[0]["error_summary"])
        detail = (await self.client.get(f"/api/logs/{row['id']}")).json()
        self.assertIn("需要重新登录", detail["error_summary"])
        self.assertTrue((await self.db.get_token(self.token_id)).is_active)

    async def test_unexpected_exception_is_recorded_once(self):
        self.manager.refresh_cookies.side_effect = RuntimeError("connection timed out")
        response = await self.refresh()
        self.assertEqual(response.status_code, 500)
        await self.assert_log(500, "connection timed out")

    async def test_http_exception_preserves_status_and_is_recorded(self):
        self.manager.refresh_cookies.side_effect = HTTPException(503, "刷新服务暂不可用")
        response = await self.refresh()
        self.assertEqual(response.status_code, 503)
        await self.assert_log(503, "刷新服务暂不可用")

    async def test_missing_token_can_be_logged_without_foreign_key_error(self):
        self.manager.refresh_cookies.return_value = False
        response = await self.refresh(token_id=9999)
        self.assertEqual(response.status_code, 500)
        rows = await self.db.get_logs(include_payload=True)
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0]["token_id"])
        self.assertEqual(json.loads(rows[0]["request_body"])["token_id"], 9999)

    async def test_log_storage_failure_does_not_change_refresh_outcome(self):
        with patch.object(self.db, "add_request_log", AsyncMock(side_effect=RuntimeError("storage unavailable"))):
            response = await self.refresh()
            self.assertEqual(response.status_code, 200)
            self.assertTrue(response.json()["success"])
            self.manager.refresh_cookies.side_effect = RuntimeError("network error")
            response = await self.refresh()
            self.assertEqual(response.status_code, 500)
            self.assertIn("network error", response.json()["detail"])

    async def test_unauthenticated_request_does_not_refresh_or_create_a_log(self):
        self.app.dependency_overrides.clear()
        response = await self.refresh()
        self.assertEqual(response.status_code, 401)
        self.manager.refresh_cookies.assert_not_awaited()
        self.assertEqual(await self.db.get_logs(), [])


if __name__ == "__main__":
    unittest.main()
