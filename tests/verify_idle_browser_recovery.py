"""Manual Chromium integration check; run against a private COPY of data/.

    python -m tests.verify_idle_browser_recovery

Requires the headed Docker image and an active account with a retained profile.
Checks real process exit, two login restorations with credential refresh, and reCAPTCHA.
Does not submit a generation request or print credentials.
"""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from src.core.database import Database
from src.services.browser_captcha_personal import BrowserCaptchaService
from src.services.browser_login import BrowserLoginService
from src.services.flow_login_recovery import IDENTITY_SCRIPT


async def verify():
    db = Database()
    await db.reload_config_to_memory()
    tokens = await db.get_all_tokens()
    service = BrowserCaptchaService(db)
    try:
        for token in tokens:
            if token.is_active and await BrowserLoginService().recover_saved_login(
                token, captcha_service=service,
            ):
                break
        else:
            raise AssertionError("No retained active login could be restored")
        profile = service._interactive_login_profile
        print("CHECK initial_login_restored PASS", flush=True)

        for cycle in range(2):
            before_refresh = (await db.get_token(token.id)).last_st_refresh_at
            process = service.browser._process
            service._runtime_last_active_at = time.time() - 120
            for info in service._resident_tabs.values():
                info.last_used_at = time.time() - 120
            assert await service.shutdown_idle_runtime_if_needed(idle_ttl_seconds=60)
            assert process.returncode is not None, "Chromium process still running"
            assert service.browser is None and not service._resident_tabs
            assert Path(profile, ".flow2api-account.json").is_file()
            print(f"CHECK cycle_{cycle + 1}_process_exited_profile_kept PASS", flush=True)

            await asyncio.wait_for(service.prepare_generation(token.id, token.current_project_id), 125)
            token = await db.get_token(token.id)
            assert token.last_st_refresh_result == "success"
            assert token.last_st_refresh_at is not None
            assert before_refresh is None or token.last_st_refresh_at > before_refresh
            assert token.st and token.at and token.google_cookies
            print(f"CHECK cycle_{cycle + 1}_credentials_refreshed_before_generation PASS", flush=True)
            assert service.browser._process.pid != process.pid
            assert service._interactive_login_profile == profile
            info = next(iter(service._resident_tabs.values()))
            identity = await info.tab.evaluate(
                f"JSON.stringify({IDENTITY_SCRIPT})", return_by_value=True,
            )
            if isinstance(identity, str):
                identity = json.loads(identity)
            assert identity["host"] == "flow.google.com"
            assert identity["email"].casefold() == (token.login_account or token.email).casefold()
            print(f"CHECK cycle_{cycle + 1}_same_account_restored PASS", flush=True)

        captcha = await asyncio.wait_for(
            service.get_token(token.current_project_id, token_id=token.id), 150,
        )
        assert isinstance(captcha, str) and len(captcha) > 100, "reCAPTCHA failed after resume"
        print("CHECK recaptcha_after_resume PASS", flush=True)
    finally:
        await service.close()
    print("CHECK all PASSED", flush=True)


if __name__ == "__main__":
    display = os.environ.get("DISPLAY", ":99")
    xvfb = subprocess.Popen(
        ["Xvfb", display, "-screen", "0", "1440x900x24", "-ac"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        for _ in range(100):
            if Path(f"/tmp/.X11-unix/X{display.lstrip(':')}").exists():
                break
            time.sleep(0.1)
        asyncio.run(verify())
    finally:
        xvfb.terminate()
        xvfb.wait(timeout=5)
