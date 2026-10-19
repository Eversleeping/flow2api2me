"""Admin-owned, short-lived interactive Google login browsers."""

import asyncio
import base64
import hashlib
import json
import os
import secrets
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

from .browser_cookie_utils import validate_flow_cookie_storage
from .browser_login_profiles import find_retained_login_profile, is_retained_login_profile
from .flow_login_recovery import IDENTITY_SCRIPT, recover_flow_identity
from ..core.config import config


class BrowserLoginError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


@dataclass
class LoginSession:
    id: str
    owner: str
    token_id: Optional[int]
    expected_email: str
    deadline: float
    state: str = "starting"
    message: str = "正在启动登录浏览器…"
    browser: object = None
    tab: object = None
    profile: Optional[str] = None
    boot_task: object = None
    expiry_task: object = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    page_origin: str = ""


class BrowserLoginService:
    WIDTH, HEIGHT = 1280, 800
    SESSION_SECONDS = 20 * 60
    # Match Flow's own sign-in link: request its OSID session and specify both
    # redirect fields, instead of relying on a previous Google application's flow.
    SIGNIN_URL = (
        "https://accounts.google.com/ServiceLogin?osid=1"
        "&continue=https%3A%2F%2Fflow.google.com%2Fprojects"
        "&followup=https%3A%2F%2Fflow.google.com%2Fprojects"
    )

    def __init__(self):
        self._session = None
        self._lock = asyncio.Lock()

    def _owned(self, session_id, owner):
        session = self._session
        if not session or session.id != session_id or session.owner != owner:
            raise BrowserLoginError("登录窗口不存在或已关闭，请重新打开", 404)
        if time.monotonic() >= session.deadline:
            raise BrowserLoginError("登录窗口已超时，请重新打开", 410)
        return session

    @staticmethod
    def _public(session):
        return {
            "id": session.id, "state": session.state,
            "message": session.message, "token_id": session.token_id,
            "expected_email": session.expected_email,
            "width": BrowserLoginService.WIDTH, "height": BrowserLoginService.HEIGHT,
            "expires_in": max(0, int(session.deadline - time.monotonic())),
            "page_origin": session.page_origin,
        }

    async def start(self, owner, token_id, db):
        expected_email = ""
        if token_id is not None:
            token = await db.get_token(token_id)
            if not token:
                raise BrowserLoginError("账号不存在", 404)
            expected_email = (token.login_account or token.email or "").strip().lower()
        async with self._lock:
            previous = self._session
            if previous and previous.state not in {"closed", "error"} and time.monotonic() < previous.deadline:
                if previous.owner == owner and previous.token_id == token_id:
                    return self._public(previous)
                raise BrowserLoginError("已有登录窗口正在使用，请先关闭或等待超时", 409)
            if previous:
                await self._dispose(previous)
            session = LoginSession(
                secrets.token_urlsafe(24), owner, token_id, expected_email,
                time.monotonic() + self.SESSION_SECONDS,
            )
            self._session = session
            session.boot_task = asyncio.create_task(self._launch(session))
            session.expiry_task = asyncio.create_task(self._expire(session))
            return self._public(session)

    async def _launch(self, session):
        try:
            if os.name != "nt" and not os.environ.get("DISPLAY"):
                raise BrowserLoginError("未找到显示环境，请使用项目的 headed Docker 镜像")
            import nodriver as uc
            from .browser_captcha_personal import _patch_nodriver_runtime

            _patch_nodriver_runtime()
            executable = os.environ.get("BROWSER_EXECUTABLE_PATH")
            if not executable or not os.path.isfile(executable):
                from playwright.async_api import async_playwright
                async with async_playwright() as playwright:
                    executable = playwright.chromium.executable_path
            if not executable or not os.path.isfile(executable):
                raise BrowserLoginError("未安装 Chromium，请使用项目的 headed Docker 镜像")
            root = Path("data/browser-login")
            root.mkdir(parents=True, exist_ok=True, mode=0o700)
            retained = find_retained_login_profile(session.token_id, session.expected_email)
            session.profile = retained or tempfile.mkdtemp(prefix="session-", dir=root)
            os.chmod(session.profile, 0o700)
            if retained:
                for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
                    (Path(retained) / name).unlink(missing_ok=True)
            # Only sign in to the Google website. Browser account sign-in can
            # sync personal extensions, which may open unrelated onboarding tabs.
            if not retained:
                default_profile = Path(session.profile) / "Default"
                default_profile.mkdir(mode=0o700)
                (default_profile / "Preferences").write_text(
                    json.dumps({"signin": {"allowed": False}}), encoding="utf-8",
                )
            async with session.lock:
                # Retain the process owner before startup so cancellation/timeout
                # can close even a partially launched Chromium.
                session.browser = uc.Browser(uc.Config(
                    user_data_dir=session.profile,
                    browser_executable_path=executable,
                    headless=False,
                    sandbox=not (hasattr(os, "geteuid") and os.geteuid() == 0),
                    host="127.0.0.1",
                    browser_args=["--window-size=1280,900", "--window-position=40,40",
                                  "--no-first-run", "--disable-dev-shm-usage",
                                  "--disable-sync", "--disable-extensions",
                                  "--ozone-platform=x11", "--disable-gpu", "--lang=zh-CN"],
                ))
                await asyncio.wait_for(session.browser.start(), 40)
                _patch_nodriver_runtime(session.browser)
                session.tab = await asyncio.wait_for(session.browser.get(self.SIGNIN_URL), 35)
                await self._viewport(session.tab)
                session.state = "ready"
                session.message = "请在画面中完成 Google 登录，然后点击“保存登录态”"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            session.state = "error"
            session.message = str(exc) if isinstance(exc, BrowserLoginError) else "登录浏览器启动失败，请关闭后重试"
            print(f"[BrowserLogin] launch failed: {type(exc).__name__}")
            await self._dispose(session, preserve_error=True)

    async def _viewport(self, tab):
        from nodriver import cdp
        await asyncio.wait_for(tab.send(cdp.emulation.set_device_metrics_override(
            self.WIDTH, self.HEIGHT, 1, False,
        )), 5)

    async def _active_tab(self, session):
        if session.state != "ready":
            raise BrowserLoginError(session.message, 409)
        await asyncio.wait_for(session.browser.update_targets(), 5)
        tabs = [t for t in session.browser.tabs if t.target.type_ == "page"]
        if not tabs:
            raise BrowserLoginError("登录页面已关闭，请重新打开", 409)
        # Google can open a popup; operate on the newest page in this isolated browser.
        tab = tabs[-1]
        if session.tab is not tab:
            session.tab = tab
            await self._viewport(tab)
        return tab

    async def status(self, session_id, owner):
        return self._public(self._owned(session_id, owner))

    async def screenshot(self, session_id, owner):
        from nodriver import cdp
        session = self._owned(session_id, owner)
        async with session.lock:
            tab = await self._active_tab(session)
            origin = await asyncio.wait_for(tab.evaluate("location.origin", return_by_value=True), 5)
            if isinstance(origin, str):
                parsed = urlparse(origin)
                safe_origin = f"{parsed.scheme}://{parsed.hostname}" if parsed.scheme in {"http", "https"} and parsed.hostname else ""
                if safe_origin and safe_origin != session.page_origin:
                    session.page_origin = safe_origin
                    print(f"[BrowserLogin] page origin: {safe_origin}")
            await asyncio.wait_for(tab.send(cdp.page.bring_to_front()), 5)
            data = await asyncio.wait_for(tab.send(cdp.page.capture_screenshot(
                format_="jpeg", quality=65, capture_beyond_viewport=False,
            )), 8)
            return base64.b64decode(data)

    async def input(self, session_id, owner, event):
        from nodriver import cdp
        session = self._owned(session_id, owner)
        async with session.lock:
            tab = await self._active_tab(session)
            kind = event.kind
            if kind == "click":
                for action in ("mousePressed", "mouseReleased"):
                    await asyncio.wait_for(tab.send(cdp.input_.dispatch_mouse_event(
                        action, event.x, event.y,
                        button=cdp.input_.MouseButton.LEFT, click_count=1,
                    )), 5)
            elif kind == "wheel":
                await asyncio.wait_for(tab.send(cdp.input_.dispatch_mouse_event(
                    "mouseWheel", event.x, event.y, delta_x=0, delta_y=event.delta,
                )), 5)
            elif kind == "text":
                await asyncio.wait_for(tab.send(cdp.input_.insert_text(event.text)), 5)
            elif kind == "key":
                codes = {"Enter": 13, "Tab": 9, "Backspace": 8, "Delete": 46,
                         "Escape": 27, "ArrowLeft": 37, "ArrowUp": 38,
                         "ArrowRight": 39, "ArrowDown": 40, "Home": 36, "End": 35, "a": 65}
                if event.key not in codes or (event.key == "a" and event.modifiers not in {2, 4}):
                    raise BrowserLoginError("不支持的按键")
                for action in ("keyDown", "keyUp"):
                    await asyncio.wait_for(tab.send(cdp.input_.dispatch_key_event(
                        action, key=event.key, windows_virtual_key_code=codes[event.key],
                        modifiers=event.modifiers,
                    )), 5)
            elif kind == "flow":
                await asyncio.wait_for(tab.get("https://flow.google.com/projects"), 30)

    async def complete(self, session_id, owner, manager, concurrency_manager=None):
        session = self._owned(session_id, owner)
        async with session.lock:
            tab = await self._active_tab(session)
            identity = await asyncio.wait_for(tab.evaluate(
                "JSON.stringify({host:location.hostname,email:(window.WIZ_global_data||{}).oPEP7c||''})",
                return_by_value=True,
            ), 5)
            if isinstance(identity, str):
                identity = json.loads(identity)
            if not isinstance(identity, dict) or identity.get("host") != "flow.google.com" or not identity.get("email"):
                raise BrowserLoginError("尚未登录 Flow。完成 Google 登录后，点击“打开 Flow”，再保存登录态")
            email = str(identity["email"]).strip().lower()
            if session.expected_email and email != session.expected_email:
                raise BrowserLoginError("登录邮箱与当前账号不一致，请切换到提示的账号后再保存")
            from .browser_captcha_personal import BrowserCaptchaService
            cookies = await asyncio.wait_for(session.browser.cookies.get_all(), 8)
            serialized = []
            for cookie in cookies:
                item = BrowserCaptchaService._serialize_browser_cookie_for_storage(cookie)
                if not item:
                    continue
                domain = (item.get("domain") or urlparse(item.get("url", "")).hostname or "").lstrip(".")
                if domain == "google.com" or domain.endswith(".google.com"):
                    serialized.append(item)
            raw = json.dumps(serialized, ensure_ascii=False, separators=(",", ":"))
            validate_flow_cookie_storage(raw)
            token = await manager.db.get_token(session.token_id) if session.token_id else await manager.db.get_token_by_email(email)
            if session.token_id and (not token or (token.login_account or token.email).strip().lower() != email):
                raise BrowserLoginError("账号已删除或修改，请关闭窗口后重新登录", 409)
            if token:
                # Validate the candidate with Flow RPC before touching the saved login state.
                candidate = token.model_copy(update={"google_cookies": raw})
                new_st = "frontend-" + hashlib.sha256(raw.encode()).hexdigest()[:32]
                result = await asyncio.wait_for(manager._st_to_at_for_token(candidate, new_st), 40)
                if not result.get("access_token"):
                    raise BrowserLoginError("Flow 登录验证失败，请确认账号可以正常打开 Flow")
                saved = await manager.db.update_token_if_cookie_matches(
                    token.id, token.google_cookies,
                    google_cookies=raw, st=new_st, at=result["access_token"],
                    at_expires=manager._parse_at_expires(result.get("expires")),
                    credits=result.get("credits", token.credits),
                    user_paygate_tier=result.get("userPaygateTier", token.user_paygate_tier),
                    last_st_refresh_at=datetime.now(timezone.utc), last_st_refresh_result="success",
                )
                if not saved:
                    raise BrowserLoginError("账号登录态刚被更新，请再次点击保存", 409)
                await manager.enable_token(token.id)
                manager._mark_at_valid(token.id)
                token_id = token.id
            else:
                token = await asyncio.wait_for(manager.add_token(
                    st="", google_cookies=raw, login_account=email, protocol_mode="protocol",
                ), 120)
                token_id = token.id
            if concurrency_manager:
                await concurrency_manager.reset_token(token_id, token.image_concurrency, token.video_concurrency)
            handed_off = False
            if config.captcha_method == "personal" and session.profile:
                saved_token = await manager.db.get_token(token_id)
                if not saved_token or saved_token.google_cookies != raw:
                    raise BrowserLoginError("账号登录态刚被更新，请再次点击保存", 409)
                service = await BrowserCaptchaService.get_instance(manager.db)
                try:
                    await service.adopt_login_browser(
                        session.browser, tab, session.profile, saved_token,
                    )
                except Exception:
                    raise BrowserLoginError(
                        "登录态已保存，但原浏览器尚未接入打码服务。请等待当前请求结束，再次点击保存登录态",
                        409,
                    ) from None
                # The captcha service now owns the process and profile. Closing
                # this admin dialog must not destroy the authenticated browser.
                session.browser = None
                session.profile = None
                session.tab = None
                handed_off = True
            session.state = "closed"
            session.message = (
                "登录态已保存，原登录浏览器已接入打码服务"
                if handed_off else "登录态已验证并保存，账号已启用"
            )
        await self._dispose(session)
        return {"success": True, "token_id": token_id, "email": email, "message": session.message}

    async def cancel(self, session_id, owner):
        session = self._owned(session_id, owner)
        await self._dispose(session)

    async def recover_saved_login(self, token, manager=None):
        """Reuse only an idle profile belonging to this account, without an admin dialog."""
        email = (token.login_account or token.email or "").strip().lower()
        async with self._lock:
            current = self._session
            if current and current.state not in {"closed", "error"} and time.monotonic() < current.deadline:
                return None
            if not find_retained_login_profile(token.id, email):
                return None
            session = LoginSession(
                secrets.token_urlsafe(24), "automatic-recovery", token.id, email,
                time.monotonic() + 120,
            )
            try:
                await self._launch(session)
                if session.state != "ready":
                    raise RuntimeError("保存的 Google 登录浏览器暂不可用，稍后重试")
                tab = session.tab

                async def navigate(url):
                    await asyncio.wait_for(tab.get(url), 20)

                async def read_identity():
                    value = await asyncio.wait_for(tab.evaluate(
                        f"JSON.stringify({IDENTITY_SCRIPT})", return_by_value=True,
                    ), 5)
                    return json.loads(value) if isinstance(value, str) else value

                identity = await recover_flow_identity(navigate, read_identity, email)
                from .browser_captcha_personal import BrowserCaptchaService
                cookies = await asyncio.wait_for(session.browser.cookies.get_all(), 8)
                serialized = []
                for cookie in cookies:
                    item = BrowserCaptchaService._serialize_browser_cookie_for_storage(cookie)
                    if item:
                        host = (item.get("domain") or urlparse(item.get("url", "")).hostname or "").lstrip(".")
                        if host == "google.com" or host.endswith(".google.com"):
                            serialized.append(item)
                result = {
                    "email": identity["email"],
                    "google_cookies": json.dumps(serialized, ensure_ascii=False, separators=(",", ":")),
                }
                if manager is not None:
                    # The authenticated profile can carry browser state that a Cookie
                    # export cannot reproduce. Keep that exact browser for CAPTCHA.
                    new_st = await manager._save_refreshed_cookies(token.id, token, result)
                    saved_token = await manager.db.get_token(token.id)
                    if not saved_token or saved_token.google_cookies != result["google_cookies"]:
                        raise RuntimeError("登录态在恢复期间已更新，请稍后重试")
                    service = await BrowserCaptchaService.get_instance(manager.db)
                    await service.adopt_login_browser(session.browser, tab, session.profile, saved_token)
                    session.browser = None
                    session.tab = None
                    session.profile = None
                    result["_saved_st"] = new_st
                return result
            finally:
                await self._dispose(session)

    async def _expire(self, session):
        await asyncio.sleep(max(0, session.deadline - time.monotonic()))
        await self._dispose(session)

    async def _dispose(self, session, preserve_error=False):
        for task in (session.boot_task, session.expiry_task):
            if task and task is not asyncio.current_task() and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass
        async with session.lock:
            if session.browser:
                browser, session.browser = session.browser, None
                process = getattr(browser, "_process", None)
                try:
                    browser.stop()
                    await asyncio.wait_for(browser.aclose(), 5)
                except Exception:
                    pass
                if process:
                    try:
                        if isinstance(process, asyncio.subprocess.Process):
                            await asyncio.wait_for(process.wait(), 5)
                        else:
                            await asyncio.to_thread(process.wait, 5)
                    except Exception:
                        try:
                            process.kill()
                        except Exception:
                            pass
            if session.profile:
                if not is_retained_login_profile(session.profile):
                    shutil.rmtree(session.profile, ignore_errors=True)
                session.profile = None
            session.tab = None
            if not preserve_error:
                session.state = "closed"

    async def close(self):
        if self._session:
            await self._dispose(self._session)

    async def close_owner(self, owner):
        if self._session and self._session.owner == owner:
            await self._dispose(self._session)


browser_login_service = BrowserLoginService()
