from __future__ import annotations

import base64

from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.attendance.scanner import (
    QRScannerError,
    QRScannerUnavailable,
    WxScanService,
)


_scanner: WxScanService | None = None


def _get_scanner(context) -> WxScanService:
    global _scanner
    if _scanner is None:
        _scanner = WxScanService(AppConfig(data_dir=context.data_dir))
    return _scanner


def handle_call(method, params, context, progress):
    if method != "attendance.scan_qr":
        raise ValueError(f"未知 Attendance Worker 方法：{method}")
    image_bytes = base64.b64decode(str(params.get("image_b64") or ""), validate=False)
    try:
        decoded = _get_scanner(context).scan_image(image_bytes)
    except QRScannerUnavailable as exc:
        return {
            "decoded": [],
            "error_type": "unavailable",
            "message": str(exc),
        }
    except QRScannerError as exc:
        return {
            "decoded": [],
            "error_type": "scan_error",
            "message": str(exc),
        }
    return {"decoded": decoded}
