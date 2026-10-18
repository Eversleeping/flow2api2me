"""Keep validated login profiles available for the same account after shutdown."""

import hashlib
import json
import os
from pathlib import Path


PROFILE_ROOT = Path("data/browser-login")
MARKER = ".flow2api-account.json"


def _identity(token_id, email):
    return {"token_id": int(token_id), "email_hash": hashlib.sha256(
        str(email or "").strip().lower().encode()
    ).hexdigest()}


def _managed_profile(profile):
    path = Path(profile)
    return not path.is_symlink() and path.resolve().parent == PROFILE_ROOT.resolve()


def retain_login_profile(profile, token):
    email = getattr(token, "login_account", None) or getattr(token, "email", "")
    if not email or not _managed_profile(profile):
        return
    path = Path(profile) / MARKER
    path.write_text(json.dumps(_identity(token.id, email)), encoding="utf-8")
    path.chmod(0o600)


def is_retained_login_profile(profile):
    try:
        marker = Path(profile) / MARKER
        return _managed_profile(profile) and not marker.is_symlink() and int(
            json.loads(marker.read_text())["token_id"]
        ) > 0
    except (OSError, ValueError, TypeError, KeyError):
        return False


def _profile_in_use(profile):
    expected = Path(profile).resolve()
    # Chromium's SingletonLock can outlive a container. Inspect actual process
    # arguments instead of treating a stale lock as a running browser.
    for command in Path("/proc").glob("[0-9]*/cmdline"):
        try:
            args = command.read_bytes().decode(errors="replace").split("\0")
            for arg in args:
                if arg.startswith("--user-data-dir="):
                    value = Path(arg.split("=", 1)[1])
                    if not value.is_absolute():
                        value = Path(os.readlink(command.parent / "cwd")) / value
                    if value.resolve() == expected:
                        return True
        except OSError:
            continue
    return False


def find_retained_login_profile(token_id, email):
    if not token_id or not email:
        return None
    identity = _identity(token_id, email)
    candidates = []
    for profile in PROFILE_ROOT.glob("session-*"):
        marker = profile / MARKER
        try:
            if (_managed_profile(profile) and not marker.is_symlink()
                    and json.loads(marker.read_text()) == identity
                    and not _profile_in_use(profile)):
                candidates.append((marker.stat().st_mtime_ns, str(profile)))
        except (OSError, ValueError, TypeError):
            continue
    return max(candidates)[1] if candidates else None
