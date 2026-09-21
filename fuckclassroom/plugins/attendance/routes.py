from __future__ import annotations

import secrets
import time
from urllib.parse import parse_qs, quote

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import RedirectResponse, Response

from ...auth.credentials import CredentialStoreError
from .ketangpai import KetangpaiError
from .qr import register_qr_assistant_routes


def build_router(context) -> APIRouter:
    services = context.services
    router = APIRouter()
    ketangpai_client = services.get("ketangpai_client")
    classroom_client = services.get("classroom_client")
    ketangpai_sms_sessions = services.get("ketangpai_sms_sessions")
    ketangpai_sms_lock = services.get("ketangpai_sms_lock")

    @router.post("/accounts/ketangpai/login")
    async def ketangpai_password_login(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        try:
            status = ketangpai_client.login_password(
                (parsed.get("account") or [""])[-1],
                (parsed.get("password") or [""])[-1],
            )
        except (KetangpaiError, CredentialStoreError) as exc:
            return RedirectResponse(
                f"/accounts?ketangpai_error={quote(str(exc))}#ketangpai-session",
                status_code=303,
            )
        return RedirectResponse(
            f"/accounts?ketangpai_notice={quote(status.message)}#ketangpai-session",
            status_code=303,
        )

    @router.post("/accounts/ketangpai/verify")
    def verify_ketangpai_login() -> RedirectResponse:
        try:
            status = ketangpai_client.verify_session(auto_relogin=True)
        except (KetangpaiError, CredentialStoreError) as exc:
            return RedirectResponse(
                f"/accounts?ketangpai_error={quote(str(exc))}#ketangpai-session",
                status_code=303,
            )
        key = "ketangpai_notice" if status.is_valid else "ketangpai_error"
        return RedirectResponse(
            f"/accounts?{key}={quote(status.message)}#ketangpai-session",
            status_code=303,
        )

    @router.post("/accounts/ketangpai/logout")
    def clear_ketangpai_login() -> RedirectResponse:
        try:
            ketangpai_client.clear_session()
        except CredentialStoreError as exc:
            return RedirectResponse(
                f"/accounts?ketangpai_error={quote(str(exc))}#ketangpai-session",
                status_code=303,
            )
        return RedirectResponse(
            "/accounts?ketangpai_notice=课堂派会话已清除#ketangpai-session",
            status_code=303,
        )

    @router.post("/accounts/ketangpai/sms/start")
    async def start_ketangpai_sms(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        mobile = (parsed.get("mobile") or [""])[-1].strip()
        if not mobile.isdigit() or len(mobile) != 11:
            return RedirectResponse(
                "/accounts?ketangpai_error=请输入%2011%20位课堂派登录手机号#ketangpai-session",
                status_code=303,
            )
        session_id = secrets.token_urlsafe(24)
        with ketangpai_sms_lock:
            ketangpai_sms_sessions[session_id] = {
                "mobile": mobile,
                "created_at": time.time(),
                "sent": False,
            }
        return RedirectResponse(
            f"/accounts?ketangpai_sms_session={quote(session_id)}#ketangpai-session",
            status_code=303,
        )

    def get_ketangpai_sms_session(session_id: str) -> dict[str, object]:
        with ketangpai_sms_lock:
            session = ketangpai_sms_sessions.get(session_id)
            if session and time.time() - float(session.get("created_at") or 0) <= 600:
                return dict(session)
        raise HTTPException(status_code=404, detail="课堂派短信登录会话已过期")

    @router.get("/accounts/ketangpai/sms/captcha")
    def ketangpai_sms_captcha(session_id: str) -> Response:
        get_ketangpai_sms_session(session_id)
        try:
            content, content_type = ketangpai_client.request_sms_captcha(session_id)
        except KetangpaiError as exc:
            raise HTTPException(status_code=502, detail=str(exc)) from exc
        return Response(
            content=content,
            media_type=content_type,
            headers={"Cache-Control": "no-store"},
        )

    @router.post("/accounts/ketangpai/sms/send")
    async def send_ketangpai_sms(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        session_id = (parsed.get("session_id") or [""])[-1]
        captcha = (parsed.get("captcha") or [""])[-1]
        try:
            session = get_ketangpai_sms_session(session_id)
            notice = ketangpai_client.send_sms_code(
                str(session["mobile"]),
                captcha,
                session_id,
            )
        except (KetangpaiError, HTTPException) as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return RedirectResponse(
                f"/accounts?ketangpai_sms_session={quote(session_id)}&ketangpai_error={quote(str(message))}#ketangpai-session",
                status_code=303,
            )
        with ketangpai_sms_lock:
            if session_id in ketangpai_sms_sessions:
                ketangpai_sms_sessions[session_id]["sent"] = True
        return RedirectResponse(
            f"/accounts?ketangpai_sms_session={quote(session_id)}&ketangpai_sms_sent=1&ketangpai_notice={quote(notice)}#ketangpai-session",
            status_code=303,
        )

    @router.post("/accounts/ketangpai/sms/login")
    async def login_ketangpai_sms(request: Request) -> RedirectResponse:
        body = (await request.body()).decode("utf-8", errors="replace")
        parsed = parse_qs(body, keep_blank_values=True)
        session_id = (parsed.get("session_id") or [""])[-1]
        code = (parsed.get("code") or [""])[-1]
        try:
            session = get_ketangpai_sms_session(session_id)
            status = ketangpai_client.login_sms(str(session["mobile"]), code)
        except (KetangpaiError, CredentialStoreError, HTTPException) as exc:
            message = exc.detail if isinstance(exc, HTTPException) else str(exc)
            return RedirectResponse(
                f"/accounts?ketangpai_sms_session={quote(session_id)}&ketangpai_error={quote(str(message))}#ketangpai-session",
                status_code=303,
            )
        with ketangpai_sms_lock:
            ketangpai_sms_sessions.pop(session_id, None)
        return RedirectResponse(
            f"/accounts?ketangpai_notice={quote(status.message)}#ketangpai-session",
            status_code=303,
        )

    register_qr_assistant_routes(
        router,
        context.config,
        classroom_client=classroom_client,
        ketangpai_client=ketangpai_client,
    )
    return router


__all__ = ["build_router"]
