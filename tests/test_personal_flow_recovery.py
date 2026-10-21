"""Upstream rejections survive successful CAPTCHA solves and tab reloads."""

import types
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

from src.services.browser_captcha_personal import (
    BrowserCaptchaService, ResidentTabInfo, _PersonalBrowserPoolService,
)
from src.services.flow_client import FlowClient


class PersonalFlowRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.service = BrowserCaptchaService()
        self.slot = "b1-slot-1"
        self.info = ResidentTabInfo(object(), self.slot, "project", token_id=7)
        self.info.created_at = 100.0
        self.service._resident_tabs[self.slot] = self.info
        self.service._initialized = True
        self.service.browser = types.SimpleNamespace(stopped=False)
        for name in (
            "_clear_browser_cache", "_clear_tab_site_storage", "_tab_reload",
            "_wait_for_document_ready", "_wait_for_recaptcha",
            "_refresh_last_fingerprint", "_cache_session_cookies_for_computed",
        ):
            setattr(self.service, name, AsyncMock(return_value=True))
        for name in (
            "_reset_local_recaptcha_asset_caches", "_remember_project_affinity",
            "_remember_token_affinity", "_record_browser_solve_success",
            "_mark_browser_health", "_remember_fingerprint",
            "_remember_solve_bundle_snapshot", "_build_solve_bundle",
        ):
            setattr(self.service, name, MagicMock())
        self.service._restart_browser_for_project = AsyncMock(return_value=True)

    async def reject(self, issued_at):
        await self.service.report_flow_error(
            "project", "Google 风控拒绝",
            "Flow frontend RPC rejected: rpc=SPrCad, error=PUBLIC_ERROR_UNUSUAL_ACTIVITY",
            token_id=7, slot_id=self.slot, issued_at=issued_at,
        )

    async def test_repeated_rejections_escalate_after_successful_reload_and_solve(self):
        await self.reject(200.0)
        self.service._tab_reload.assert_awaited_once()
        self.assertEqual(self.service._resident_error_streaks[self.slot], 1)
        await self.service._finalize_resident_solve_locked(
            token="fresh-captcha", slot_id=self.slot, project_id="project",
            action="IMAGE_GENERATION", resident_info=self.info,
        )
        self.assertEqual(self.service._resident_error_streaks[self.slot], 1)
        await self.reject(201.0)
        self.service._restart_browser_for_project.assert_awaited_once_with(
            "project", token_id=7,
        )

    async def test_accepted_rpc_resets_streak_before_next_rejection(self):
        await self.reject(200.0)
        client = FlowClient(None)
        client._call_flow_frontend_rpc = AsyncMock(return_value=["accepted"])
        client._set_captcha_request_context({
            "method": "personal", "project_id": "project", "token_id": 7,
            "slot_id": self.slot, "issued_at": 201.0,
        })
        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService.get_instance",
            AsyncMock(return_value=self.service),
        ):
            self.assertEqual(await client._current_rpc(
                rpc_id="SPrCad", argument=[], project_id="project", token_id=7,
                google_cookies="OSID=test",
            ), ["accepted"])
        self.assertNotIn(self.slot, self.service._resident_error_streaks)
        await self.reject(202.0)
        self.assertEqual(self.service._resident_error_streaks[self.slot], 1)
        self.service._restart_browser_for_project.assert_not_awaited()

    async def test_rebuilding_same_account_tab_preserves_upstream_failures(self):
        await self.reject(200.0)
        replacement = ResidentTabInfo(object(), self.slot, "project", token_id=7)
        replacement.created_at = 201.0
        self.service._create_resident_tab = AsyncMock(return_value=replacement)
        self.service._dispose_browser_context_quietly = AsyncMock()
        self.service._close_tab_quietly = AsyncMock()
        self.assertTrue(await self.service._recreate_resident_tab(
            "project", token_id=7, slot_id=self.slot,
        ))
        self.assertEqual(self.service._resident_error_streaks[self.slot], 1)
        await self.reject(202.0)
        self.service._restart_browser_for_project.assert_awaited_once()

    async def test_stale_or_other_account_success_cannot_clear_failure(self):
        await self.reject(200.0)
        for token_id, slot, issued_at in (
            (7, self.slot, 199.0), (8, self.slot, 201.0),
            (7, "other-slot", 201.0), (7, self.slot, 99.0),
        ):
            with self.subTest(token_id=token_id, slot=slot, issued_at=issued_at):
                await self.service.report_flow_success(
                    "project", token_id=token_id, slot_id=slot, issued_at=issued_at,
                )
                self.assertEqual(self.service._resident_error_streaks[self.slot], 1)

    async def test_unrelated_rpc_does_not_reset_protected_request_failures(self):
        client = FlowClient(None)
        client._call_flow_frontend_rpc = AsyncMock(return_value=[])
        client._set_captcha_request_context({
            "method": "personal", "project_id": "project", "token_id": 7,
            "slot_id": self.slot, "issued_at": 201.0,
        })
        with patch(
            "src.services.browser_captcha_personal.BrowserCaptchaService.get_instance",
            AsyncMock(return_value=self.service),
        ) as get_service:
            await client._current_rpc(rpc_id="nzlxg", argument=[], token_id=7)
        get_service.assert_not_awaited()

    async def test_pool_success_targets_only_the_worker_that_issued_the_solve(self):
        pool = _PersonalBrowserPoolService.__new__(_PersonalBrowserPoolService)
        pool._ensure_workers = AsyncMock()
        pool._workers = [types.SimpleNamespace(report_flow_success=AsyncMock())
                         for _ in range(2)]
        await pool.report_flow_success(
            "project", token_id=7, slot_id="b2-slot-1", issued_at=201.0,
        )
        pool._workers[0].report_flow_success.assert_not_awaited()
        pool._workers[1].report_flow_success.assert_awaited_once_with(
            "project", token_id=7, slot_id="b2-slot-1", issued_at=201.0,
        )
