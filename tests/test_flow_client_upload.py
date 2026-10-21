import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from src.services.flow_client import FlowClient
from src.services.flow_frontend import FlowAuthenticationError
from src.services.generation_handler import GenerationHandler


JPEG_BYTES = b"\xff\xd8\xff" + b"0" * 16
PROJECT_ID = "01234567-89ab-cdef-0123-456789abcdef"
MEDIA_ID = "11234567-89ab-cdef-0123-456789abcdef"


class FlowClientUploadImageTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_uses_current_frontend_rpc(self):
        client = FlowClient(proxy_manager=None)
        client._get_recaptcha_token = AsyncMock(return_value=("captcha-token", None))
        client._call_flow_frontend_rpc = AsyncMock(
            return_value=[
                [
                    MEDIA_ID,
                    PROJECT_ID,
                    f"https://flow-content.google/image/{MEDIA_ID}?Signature=1",
                ]
            ]
        )
        client._make_request = AsyncMock()

        media_id = await client.upload_image(
            at="frontend-cookie",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id=PROJECT_ID,
            google_cookies="SID=session-cookie",
        )

        self.assertEqual(media_id, MEDIA_ID)
        client._make_request.assert_not_awaited()
        request = client._call_flow_frontend_rpc.await_args.kwargs
        self.assertEqual(request["rpc_id"], "maseQ")
        self.assertEqual(request["argument"][0][5], PROJECT_ID)
        self.assertEqual(request["argument"][0][10], ["captcha-token", 1])
        self.assertEqual(request["argument"][2], "image/jpeg")

    async def test_upload_argument_matches_current_frontend_envelope(self):
        client = FlowClient(proxy_manager=None)
        client._get_recaptcha_token = AsyncMock(return_value=("captcha-token", None))
        client._call_flow_frontend_rpc = AsyncMock(
            return_value=[[MEDIA_ID, PROJECT_ID, f"https://flow-content.google/image/{MEDIA_ID}?Signature=1"]]
        )
        client._make_request = AsyncMock()

        await client.upload_image(
            at="frontend-cookie",
            image_bytes=JPEG_BYTES,
            aspect_ratio="IMAGE_ASPECT_RATIO_LANDSCAPE",
            project_id=PROJECT_ID,
            google_cookies="SID=session-cookie",
        )

        argument = client._call_flow_frontend_rpc.await_args.kwargs["argument"]
        self.assertEqual(len(argument), 12)
        # slot 3 is an integer enum; sending a bool makes the RPC reject with code=[3]
        self.assertIs(type(argument[3]), int)
        self.assertIsNone(argument[7])
        self.assertIsNone(argument[9])
        for slot in (10, 11):
            self.assertRegex(argument[slot], r"^[0-9A-F]{8}(-[0-9A-F]{4}){3}-[0-9A-F]{12}$")

    async def test_upload_requires_project_id(self):
        client = FlowClient(proxy_manager=None)

        with self.assertRaisesRegex(ValueError, "requires project_id"):
            await client.upload_image(
                at="frontend-cookie",
                image_bytes=JPEG_BYTES,
                project_id=None,
                google_cookies="SID=session-cookie",
            )


class _UploadTestCase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.client = FlowClient(None)
        self.runtime = types.SimpleNamespace(flow_max_retries=3)
        self.client._get_runtime_config = lambda: self.runtime
        self.events = []

        async def solve(*args, **kwargs):
            number = self.client._get_recaptcha_token.await_count
            self.client._set_captcha_request_context({"slot_id": f"slot-{number}"})
            self.events.append(f"solve-{number}")
            return f"captcha-{number}", f"browser-{number}"

        async def recover(**kwargs):
            context = self.client._get_captcha_request_context()
            self.events.append(f"recover-{context['slot_id']}")

        async def finish(browser_id):
            self.events.append(f"finish-{browser_id}")
            self.client._set_captcha_request_context(None)

        self.client._get_recaptcha_token = AsyncMock(side_effect=solve)
        self.client._notify_browser_captcha_error = AsyncMock(side_effect=recover)
        self.client._notify_browser_captcha_request_finished = AsyncMock(side_effect=finish)
        self.client._current_rpc = AsyncMock(return_value=[MEDIA_ID])
        self.sleep = self.enterContext(patch(
            "src.services.flow_client.asyncio.sleep", new_callable=AsyncMock,
        ))

    async def upload(self):
        return await self.client.upload_image(
            "access", JPEG_BYTES, project_id=PROJECT_ID, token_id=7,
        )


class UploadRecoveryTests(_UploadTestCase):
    async def test_timeout_recovers_failed_solve_and_retries_upload_with_fresh_token(self):
        self.client._current_rpc.side_effect = [
            RuntimeError("curl: (28) Operation timed out after 90000 milliseconds"),
            [MEDIA_ID],
        ]
        self.assertEqual(await self.upload(), MEDIA_ID)
        self.assertEqual(self.events, [
            "solve-1", "recover-slot-1", "finish-browser-1",
            "solve-2", "finish-browser-2",
        ])
        requests = self.client._current_rpc.await_args_list
        self.assertEqual([r.kwargs["rpc_id"] for r in requests], ["maseQ", "maseQ"])
        self.assertEqual(
            [r.kwargs["argument"][0][10] for r in requests],
            [["captcha-1", 1], ["captcha-2", 1]],
        )
        self.assertEqual(self.client._get_captcha_request_context(), {})

    async def test_exhausted_timeouts_preserve_cause_and_obey_retry_limit(self):
        error = RuntimeError("curl: (28) Operation timed out")
        self.client._current_rpc.side_effect = error
        with self.assertRaisesRegex(RuntimeError, "参考图上传失败") as caught:
            await self.upload()
        self.assertIs(caught.exception.__cause__, error)
        self.assertEqual(self.client._current_rpc.await_count, 3)
        self.assertEqual(self.client._notify_browser_captcha_error.await_count, 3)
        self.assertEqual(self.client._notify_browser_captcha_request_finished.await_count, 3)
        self.assertEqual(self.sleep.await_count, 2)

    async def test_permanent_rejection_is_not_retried(self):
        self.client._current_rpc.side_effect = RuntimeError("INVALID_ARGUMENT code=3")
        with self.assertRaisesRegex(RuntimeError, "INVALID_ARGUMENT"):
            await self.upload()
        self.client._current_rpc.assert_awaited_once()
        self.sleep.assert_not_awaited()

    async def test_single_attempt_setting_is_honored(self):
        self.runtime.flow_max_retries = 1
        self.client._current_rpc.side_effect = RuntimeError("curl: (28) timed out")
        with self.assertRaisesRegex(RuntimeError, "参考图上传失败"):
            await self.upload()
        self.client._current_rpc.assert_awaited_once()
        self.sleep.assert_not_awaited()

    async def test_success_does_not_recover_or_retry(self):
        self.assertEqual(await self.upload(), MEDIA_ID)
        self.client._notify_browser_captcha_error.assert_not_awaited()
        self.client._current_rpc.assert_awaited_once()
        self.client._notify_browser_captcha_request_finished.assert_awaited_once()

    async def test_cancelled_upload_releases_context_without_retry(self):
        self.client._current_rpc.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.upload()
        self.client._current_rpc.assert_awaited_once()
        self.client._notify_browser_captcha_error.assert_not_awaited()
        self.assertEqual(self.client._get_captcha_request_context(), {})


class UploadAuthenticationRecoveryTests(_UploadTestCase):
    def setUp(self):
        super().setUp()
        self.saved = types.SimpleNamespace(google_cookies="OSID=old; SID=account")
        self.client.db = types.SimpleNamespace(get_token=AsyncMock(return_value=self.saved))
        self.error = FlowAuthenticationError(
            "Flow 登录认证失败：Google Cookie 已失效 (rpc=maseQ, code=401)"
        )

        async def refresh(token_id, *, expected_google_cookies):
            self.assertEqual(self.client._get_captcha_request_context(), {})
            self.assertEqual(expected_google_cookies, "OSID=old; SID=account")
            self.events.append("refresh-credentials")
            self.saved.google_cookies = "OSID=new; SID=account"
            return True

        self.client._credential_refresh = AsyncMock(side_effect=refresh)

    async def test_401_releases_solve_refreshes_and_reuploads_with_new_cookie(self):
        self.client._current_rpc.side_effect = [self.error, [MEDIA_ID]]
        self.assertEqual(await self.upload(), MEDIA_ID)
        self.assertEqual(self.events, [
            "solve-1", "recover-slot-1", "finish-browser-1",
            "refresh-credentials", "solve-2", "finish-browser-2",
        ])
        requests = self.client._current_rpc.await_args_list
        self.assertEqual(
            [r.kwargs["google_cookies"] for r in requests],
            ["OSID=old; SID=account", "OSID=new; SID=account"],
        )
        self.assertEqual(
            [r.kwargs["argument"][0][10] for r in requests],
            [["captcha-1", 1], ["captcha-2", 1]],
        )
        self.client._credential_refresh.assert_awaited_once_with(
            7, expected_google_cookies="OSID=old; SID=account",
        )

    async def test_upload_uses_latest_cookie_after_captcha_instead_of_caller_snapshot(self):
        original_solve = self.client._get_recaptcha_token.side_effect

        async def solve(*args, **kwargs):
            result = await original_solve(*args, **kwargs)
            self.saved.google_cookies = "OSID=awakened; SID=account"
            return result

        self.client._get_recaptcha_token.side_effect = solve
        await self.client.upload_image(
            "access", JPEG_BYTES, project_id=PROJECT_ID, token_id=7,
            google_cookies="OSID=stale; SID=account",
        )
        self.assertEqual(
            self.client._current_rpc.await_args.kwargs["google_cookies"],
            "OSID=awakened; SID=account",
        )
        self.client._credential_refresh.assert_not_awaited()

    async def test_failed_refresh_stops_upload_and_preserves_original_cause(self):
        self.client._current_rpc.side_effect = self.error
        self.client._credential_refresh.side_effect = None
        self.client._credential_refresh.return_value = False
        with self.assertRaisesRegex(RuntimeError, "自动刷新凭证失败") as caught:
            await self.upload()
        self.assertIs(caught.exception.__cause__, self.error)
        self.client._current_rpc.assert_awaited_once()
        self.assertEqual(self.client._get_captcha_request_context(), {})

    async def test_refresh_exception_preserves_authentication_and_refresh_errors(self):
        self.client._current_rpc.side_effect = self.error
        self.client._credential_refresh.side_effect = TimeoutError("refresh timed out")
        with self.assertRaisesRegex(RuntimeError, "自动刷新凭证失败：refresh timed out") as caught:
            await self.upload()
        self.assertIs(caught.exception.__cause__, self.error)
        self.client._current_rpc.assert_awaited_once()

    async def test_repeated_401_refreshes_only_once(self):
        self.client._current_rpc.side_effect = self.error
        with self.assertRaisesRegex(RuntimeError, "code=401"):
            await self.upload()
        self.assertEqual(self.client._current_rpc.await_count, 2)
        self.client._credential_refresh.assert_awaited_once()

    async def test_401_obeys_single_attempt_setting(self):
        self.runtime.flow_max_retries = 1
        self.client._current_rpc.side_effect = self.error
        with self.assertRaisesRegex(RuntimeError, "code=401"):
            await self.upload()
        self.client._current_rpc.assert_awaited_once()
        self.client._credential_refresh.assert_not_awaited()

    async def test_401_without_bound_account_does_not_refresh(self):
        self.client._current_rpc.side_effect = self.error
        with self.assertRaisesRegex(RuntimeError, "code=401"):
            await self.client.upload_image(
                "access", JPEG_BYTES, project_id=PROJECT_ID,
                google_cookies="OSID=external; SID=account",
            )
        self.client._credential_refresh.assert_not_awaited()

    async def test_cancelled_refresh_does_not_resubmit(self):
        self.client._current_rpc.side_effect = self.error
        self.client._credential_refresh.side_effect = asyncio.CancelledError()
        with self.assertRaises(asyncio.CancelledError):
            await self.upload()
        self.client._current_rpc.assert_awaited_once()
        self.assertEqual(self.client._get_captcha_request_context(), {})

    async def test_multi_image_generation_and_upscale_use_recovered_credentials(self):
        # Exercise the real handler and RPC cookie resolver with a stale token
        # snapshot, including the next reference image and the paid RPCs.
        self.runtime.flow_image_request_timeout = 90
        self.runtime.upsample_timeout = 90
        self.client._current_rpc = FlowClient._current_rpc.__get__(self.client)
        self.client._call_flow_frontend_rpc = AsyncMock(side_effect=[
            self.error, [MEDIA_ID], [MEDIA_ID], [],
            [f"https://flow-content.google/image/{MEDIA_ID}"],
        ])
        self.client._normalize_frontend_image_generation_response = lambda payload: {
            "media": [{"name": MEDIA_ID, "image": {"generatedImage": {
                "fifeUrl": f"https://flow-content.google/image/{MEDIA_ID}",
            }}}],
        }
        handler = GenerationHandler.__new__(GenerationHandler)
        handler.flow_client = self.client
        handler._update_request_log_progress = AsyncMock()
        token = types.SimpleNamespace(
            id=7, at="access", google_cookies="OSID=old; SID=account",
            image_concurrency=1, user_paygate_tier=None,
        )
        result = {}
        with patch("src.services.generation_handler.config", types.SimpleNamespace(
            flow_max_retries=3, cache_enabled=False,
        )):
            chunks = [chunk async for chunk in handler._handle_image_generation(
                token, PROJECT_ID, {
                    "model_name": "GEM_PIX_2",
                    "aspect_ratio": "IMAGE_ASPECT_RATIO_LANDSCAPE",
                    "upsample": "IMAGE_UPSAMPLE_RESOLUTION_2K",
                }, "test", [JPEG_BYTES, JPEG_BYTES], False,
                generation_result=result,
            )]
        requests = self.client._call_flow_frontend_rpc.await_args_list
        self.assertEqual(
            [r.kwargs["rpc_id"] for r in requests],
            ["maseQ", "maseQ", "maseQ", "ogiZ0b", "SPrCad"],
        )
        self.assertEqual(
            [r.kwargs["cookie_storage"] for r in requests],
            ["OSID=old; SID=account"] + ["OSID=new; SID=account"] * 4,
        )
        self.assertTrue(result["success"])
        self.assertTrue(chunks)


class UploadAuthenticationClassificationTests(unittest.TestCase):
    def test_rpc_401_is_typed_as_authentication_failure(self):
        with self.assertRaises(FlowAuthenticationError):
            FlowClient._raise_batchexecute_error(
                [[["er", "maseQ", None, None, None, 401]]], "maseQ",
            )

    def test_permanent_rpc_rejection_is_not_typed_as_authentication_failure(self):
        with self.assertRaises(RuntimeError) as caught:
            FlowClient._raise_batchexecute_error(
                [[["er", "maseQ", None, None, None, 3]]], "maseQ",
            )
        self.assertNotIsInstance(caught.exception, FlowAuthenticationError)


if __name__ == "__main__":
    unittest.main()
