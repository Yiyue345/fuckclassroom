from __future__ import annotations

import threading
from urllib.parse import urljoin

from fastapi import APIRouter, HTTPException

from ...classroom import ClassroomClient, ClassroomClientError
from .ketangpai import (
    KetangpaiAuthenticationError,
    KetangpaiClient,
    KetangpaiCredentialStore,
    KetangpaiError,
)
from .scanner import (
    QRScannerError,
    QRScannerUnavailable,
    WxScanService,
    analyze_qr_payload,
)
from ...core.config import AppConfig


GUET_CHANGKE_BASE_URL = "https://courses.guet.edu.cn/"

_SIGN_MESSAGES = {
    "rollcall_closed": "二维码签到已结束",
    "device_used": "该设备已用于签到，请联系老师确认",
    "QR_code_expired": "签到二维码已过期，正在等待课件中的新二维码",
    "unknown_student": "当前账号不是本课程学生",
    "failed": "签到失败，请稍后重试",
}


def submit_changke_qr(
    classroom_client: ClassroomClient,
    fields: dict[str, object],
) -> dict[str, object]:
    rollcall_id = str(fields.get("rollcallId") or "").strip()
    data = str(fields.get("data") or "").strip()
    if not rollcall_id or not data:
        return {
            "attempted": False,
            "success": False,
            "state": "official_required",
            "retryable": False,
            "message": "二维码未包含可直接签到的数据，请在官方页面继续操作",
        }
    try:
        response = classroom_client.answer_qr_rollcall(
            rollcall_id,
            data,
            classroom_client.get_qr_device_id(),
        )
    except ClassroomClientError as exc:
        return {
            "attempted": True,
            "success": False,
            "state": "error",
            "retryable": True,
            "message": str(exc),
        }

    status = str(response.get("status") or "")
    http_status = response.get("_http_status")
    success = http_status == 200 and status in {"on_call", "on_call_fine"}
    if success:
        return {
            "attempted": True,
            "success": True,
            "state": "success",
            "retryable": False,
            "message": "签到成功",
        }

    message_key = str(response.get("message") or "failed")
    state = {
        "rollcall_closed": "closed",
        "QR_code_expired": "expired",
        "device_used": "device_conflict",
        "unknown_student": "not_enrolled",
    }.get(message_key, "failed")
    return {
        "attempted": True,
        "success": False,
        "state": state,
        "retryable": state == "failed",
        "message": _SIGN_MESSAGES.get(message_key, message_key or _SIGN_MESSAGES["failed"]),
    }


def submit_ketangpai_qr(
    ketangpai_client: KetangpaiClient,
    raw_url: str,
) -> dict[str, object]:
    try:
        response = ketangpai_client.submit_scan(raw_url)
    except KetangpaiAuthenticationError as exc:
        return {
            "attempted": False,
            "success": False,
            "state": "login_required",
            "retryable": False,
            "message": str(exc),
            "account_url": "/accounts#ketangpai-session",
        }
    except KetangpaiError as exc:
        return {
            "attempted": True,
            "success": False,
            "state": "error",
            "retryable": True,
            "message": str(exc),
        }

    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    success = int(data.get("state") or 0) == 8
    message = str(data.get("info") or response.get("message") or "")
    return {
        "attempted": True,
        "success": success,
        "state": "success" if success else "failed",
        "retryable": False,
        "message": message or ("签到成功" if success else "课堂派签到失败"),
    }


def register_qr_assistant_routes(
    app: APIRouter,
    config: AppConfig | None = None,
    *,
    classroom_client: ClassroomClient | None = None,
    ketangpai_client: KetangpaiClient | None = None,
    scanner: WxScanService | None = None,
) -> None:
    app_config = config or AppConfig()
    classroom_client = classroom_client or ClassroomClient(app_config)
    ketangpai_client = ketangpai_client or KetangpaiClient(
        KetangpaiCredentialStore(app_config.data_dir / "account" / "ketangpai.json")
    )
    scanner = scanner or WxScanService(app_config)
    successful_rollcalls: dict[str, dict[str, object]] = {}
    successful_rollcalls_lock = threading.Lock()

    @app.post("/api/courses/{course_id}/lessons/{lesson_id}/live/ppt/{slide_id}/qr")
    def scan_live_ppt_qr(course_id: str, lesson_id: str, slide_id: int) -> dict[str, object]:
        if slide_id <= 0:
            raise HTTPException(status_code=400, detail="直播 PPT 页编号无效")
        try:
            slide = next(
                (
                    item
                    for item in classroom_client.list_live_ppt_slides(course_id, lesson_id)
                    if item.id == slide_id
                ),
                None,
            )
            if slide is None:
                raise HTTPException(status_code=404, detail="未找到这页直播 PPT")
            image_bytes, _ = classroom_client.download_live_ppt_image(slide.image_url)
            decoded = scanner.scan_image(image_bytes)
        except HTTPException:
            raise
        except ClassroomClientError as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc
        except QRScannerUnavailable as exc:
            raise HTTPException(status_code=503, detail=str(exc)) from exc
        except QRScannerError as exc:
            raise HTTPException(status_code=422, detail=str(exc)) from exc

        unique_payloads = list(dict.fromkeys(decoded))
        matches: list[dict[str, object]] = []
        for raw in unique_payloads:
            match = analyze_qr_payload(raw).to_dict()
            if match["platform"] == "changke" and not match["action_url"] and raw.startswith("/"):
                match["action_url"] = urljoin(GUET_CHANGKE_BASE_URL, raw)
            if match["platform"] == "changke":
                fields = match["fields"] if isinstance(match["fields"], dict) else {}
                rollcall_id = str(fields.get("rollcallId") or "")
                with successful_rollcalls_lock:
                    cached_sign = successful_rollcalls.get(rollcall_id)
                sign = dict(cached_sign) if cached_sign else submit_changke_qr(
                    classroom_client,
                    fields,
                )
                match["sign"] = sign
                if rollcall_id and sign["success"]:
                    with successful_rollcalls_lock:
                        successful_rollcalls[rollcall_id] = dict(sign)
            elif match["platform"] == "ketangpai":
                raw_key = f"ketangpai:{raw}"
                with successful_rollcalls_lock:
                    cached_sign = successful_rollcalls.get(raw_key)
                sign = dict(cached_sign) if cached_sign else submit_ketangpai_qr(
                    ketangpai_client,
                    raw,
                )
                match["sign"] = sign
                if sign["success"]:
                    with successful_rollcalls_lock:
                        successful_rollcalls[raw_key] = dict(sign)
            matches.append(match)
        return {
            "slide_id": slide.id,
            "created_at": slide.created_at,
            "matches": matches,
            "count": len(matches),
        }
