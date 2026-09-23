from __future__ import annotations

import time
from typing import Any

from ...core.plugins import ServiceContainer


def build_account_context(
    services: ServiceContainer,
    request: Any,
) -> dict[str, object]:
    ketangpai_client = services.get("ketangpai_client")
    sms_sessions = services.get("ketangpai_sms_sessions")
    sms_lock = services.get("ketangpai_sms_lock")
    sms_session_id = request.query_params.get("ketangpai_sms_session", "")
    sms_session = None

    with sms_lock:
        now = time.time()
        for key in list(sms_sessions):
            if now - float(sms_sessions[key].get("created_at") or 0) > 600:
                sms_sessions.pop(key, None)
        sms_session = sms_sessions.get(sms_session_id)

    notice = request.query_params.get("ketangpai_notice")
    error = request.query_params.get("ketangpai_error")
    notices = []
    if error:
        notices.append({"kind": "error", "icon": "circle-alert", "message": error})
    if notice:
        notices.append({"kind": "success", "icon": "shield-check", "message": notice})

    return {
        "session": ketangpai_client.get_session_status(),
        "notices": tuple(notices),
        "sms_sent": request.query_params.get("ketangpai_sms_sent") == "1",
        "sms": (
            {"session_id": sms_session_id, **sms_session}
            if sms_session is not None
            else None
        ),
    }


__all__ = ["build_account_context"]
