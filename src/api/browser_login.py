"""Authenticated control plane for interactive browser login."""

import asyncio
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from pydantic import BaseModel, Field

from . import admin
from ..services.browser_login import browser_login_service, BrowserLoginError


async def login_owner(request: Request, owner: str = Depends(admin.verify_admin_token)):
    # Require a non-simple request and same origin, including on screenshots.
    # The application's general CORS policy must not expose login browser contents.
    origin = request.headers.get("origin")
    # TLS terminates at Caddy. Keep the request Host as the authority; only use
    # its forwarded scheme, never a client-supplied forwarded host.
    scheme = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    expected = f"{scheme}://{request.url.netloc}"
    if request.headers.get("x-browser-login") != "1" or (origin and origin != expected):
        raise HTTPException(403, "请从本项目管理后台操作登录窗口")
    return owner


router = APIRouter(prefix="/api/browser-login", tags=["browser-login"])
PRIVATE_HEADERS = {"Cache-Control": "no-store", "Pragma": "no-cache", "X-Content-Type-Options": "nosniff"}


class StartRequest(BaseModel):
    token_id: Optional[int] = Field(default=None, gt=0)


class InputRequest(BaseModel):
    kind: Literal["click", "wheel", "text", "key", "flow"]
    x: float = Field(default=0, ge=0, le=1279)
    y: float = Field(default=0, ge=0, le=799)
    delta: float = Field(default=0, ge=-2000, le=2000)
    text: str = Field(default="", max_length=4096)
    key: str = Field(default="", max_length=20)
    modifiers: int = Field(default=0, ge=0, le=15)


async def run(operation):
    try:
        return await operation
    except BrowserLoginError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except (TimeoutError, asyncio.TimeoutError):
        raise HTTPException(504, "浏览器或 Flow 验证响应超时，请稍后重试") from None
    except Exception as exc:
        print(f"[BrowserLogin] operation failed: {type(exc).__name__}")
        raise HTTPException(502, "浏览器操作或 Flow 验证失败，请确认已登录正确账号后重试") from None


@router.post("/start")
async def start(body: StartRequest, owner: str = Depends(login_owner)):
    return await run(browser_login_service.start(owner, body.token_id, admin.db))


@router.get("/{session_id}/status")
async def status(session_id: str, response: Response, owner: str = Depends(login_owner)):
    response.headers.update(PRIVATE_HEADERS)
    return await run(browser_login_service.status(session_id, owner))


@router.get("/{session_id}/screenshot")
async def screenshot(session_id: str, owner: str = Depends(login_owner)):
    data = await run(browser_login_service.screenshot(session_id, owner))
    return Response(data, media_type="image/jpeg", headers=PRIVATE_HEADERS)


@router.post("/{session_id}/input")
async def input_event(session_id: str, body: InputRequest, owner: str = Depends(login_owner)):
    await run(browser_login_service.input(session_id, owner, body))
    return {"success": True}


@router.post("/{session_id}/complete")
async def complete(session_id: str, owner: str = Depends(login_owner)):
    return await run(browser_login_service.complete(
        session_id, owner, admin.token_manager, admin.concurrency_manager,
    ))


@router.post("/{session_id}/cancel")
async def cancel(session_id: str, owner: str = Depends(login_owner)):
    await run(browser_login_service.cancel(session_id, owner))
    return {"success": True}
