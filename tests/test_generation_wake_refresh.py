import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.token_manager import TokenManager


class GenerationWakeRefreshTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.old = types.SimpleNamespace(id=7, current_project_id="project", is_active=True)
        self.fresh = types.SimpleNamespace(id=7, current_project_id="project", is_active=True)
        self.db = types.SimpleNamespace(get_token=AsyncMock(return_value=self.fresh))
        self.manager = TokenManager(self.db, MagicMock())
        self.manager.ensure_valid_token = AsyncMock(return_value=self.fresh)
        self.worker = types.SimpleNamespace(prepare_generation=AsyncMock())

    async def prepare(self, method="personal"):
        with (
            patch("src.services.token_manager.config") as config,
            patch("src.services.browser_captcha_personal.BrowserCaptchaService.get_instance", AsyncMock(return_value=self.worker)),
        ):
            config.captcha_method = method
            return await self.manager.prepare_generation_token(self.old)

    async def test_generation_reads_credentials_after_wake_refresh(self):
        async def wake(*args):
            self.db.get_token.assert_not_awaited()
            self.manager.ensure_valid_token.assert_not_awaited()

        self.worker.prepare_generation.side_effect = wake
        self.assertIs(await self.prepare(), self.fresh)
        self.worker.prepare_generation.assert_awaited_once_with(7, "project")
        self.manager.ensure_valid_token.assert_awaited_once_with(self.fresh)

    async def test_refresh_error_prevents_generation_preparation(self):
        self.worker.prepare_generation.side_effect = RuntimeError("credential refresh failed")
        with self.assertRaisesRegex(RuntimeError, "credential refresh failed"):
            await self.prepare()
        self.db.get_token.assert_not_awaited()
        self.manager.ensure_valid_token.assert_not_awaited()

    async def test_disabled_account_after_refresh_is_rejected(self):
        self.fresh.is_active = False
        self.assertIsNone(await self.prepare())
        self.manager.ensure_valid_token.assert_not_awaited()

    async def test_other_captcha_methods_keep_existing_validation(self):
        await self.prepare("browser")
        self.worker.prepare_generation.assert_not_awaited()
        self.manager.ensure_valid_token.assert_awaited_once_with(self.old)
