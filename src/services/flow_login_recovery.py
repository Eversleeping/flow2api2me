"""Recover a Flow session through an existing Google browser login."""

import asyncio
from urllib.parse import urlencode

FLOW_URL = "https://flow.google.com/projects"
SIGNIN_URL = "https://accounts.google.com/ServiceLogin?" + urlencode({
    "osid": "1", "continue": FLOW_URL, "followup": FLOW_URL,
})
IDENTITY_SCRIPT = """({host: location.hostname, ready: document.readyState,
    email: (window.WIZ_global_data || {}).oPEP7c || '',
    needs_login: location.hostname === 'accounts.google.com' &&
        [...document.querySelectorAll('input[type="password"], input[type="email"], input[autocomplete="one-time-code"], input[name="totpPin"], input[name="idvPin"]')]
            .some(element => element.getClientRects().length > 0)})"""


class GoogleLoginRequired(RuntimeError):
    """Google requires user interaction instead of silently restoring Flow."""


async def recover_flow_identity(navigate, read_identity, expected_email=""):
    """Try Flow, then Google's own SSO entry point; never fill credentials."""
    async def wait_for_flow(*, signing_in=False):
        last = None
        for attempt in range(30):
            try:
                last = await read_identity()
            except Exception:
                # SSO redirects can replace the execution context between reads.
                last = None
                await asyncio.sleep(0.3)
                continue
            if isinstance(last, dict) and last.get("ready") == "complete":
                email = str(last.get("email") or "").strip()
                if last.get("host") == "flow.google.com" and email:
                    if expected_email and email.casefold() != expected_email.strip().casefold():
                        raise RuntimeError("Flow 登录账号与当前 Token 不一致，未保存 Cookie")
                    return last
                if signing_in and attempt >= 2 and last.get("needs_login"):
                    raise GoogleLoginRequired("Google 需要密码或二次验证，请点击“重新登录”完成验证")
                if not signing_in and attempt >= 2:
                    return None
            await asyncio.sleep(0.3)
        if signing_in and isinstance(last, dict) and last.get("host") == "accounts.google.com" and last.get("ready") == "complete":
            raise GoogleLoginRequired("Google 需要手动确认登录，请点击“重新登录”继续")
        raise TimeoutError("Flow 自动恢复登录尚未完成，稍后重试")

    await navigate(FLOW_URL)
    identity = await wait_for_flow()
    if identity:
        return identity
    url = SIGNIN_URL
    if expected_email:
        url += "&" + urlencode({"login_hint": expected_email})
    await navigate(url)
    identity = await wait_for_flow(signing_in=True)
    if not identity:
        raise RuntimeError("Flow 自动恢复登录未返回账号")
    return identity
