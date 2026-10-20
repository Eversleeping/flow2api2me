import asyncio
import types
import unittest
from unittest.mock import AsyncMock, patch

from src.services.flow_client import FlowClient


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


class UploadRecoveryTests(unittest.IsolatedAsyncioTestCase):
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


if __name__ == "__main__":
    unittest.main()
