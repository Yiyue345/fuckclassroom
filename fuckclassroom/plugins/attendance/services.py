from __future__ import annotations

import base64
import threading
from pathlib import Path

from .ketangpai import KetangpaiClient, KetangpaiCredentialStore
from ...core.plugins import PluginContext
from .scanner import QRScannerError, QRScannerUnavailable
from ..process_runtime import ProcessPluginError, ProcessPluginHost
from ..rpc import PLUGIN_RPC_API_VERSION


class AttendanceScannerProcessProxy:
    def __init__(self, host: ProcessPluginHost) -> None:
        self.host = host

    def scan_image(self, image_bytes: bytes) -> list[str]:
        try:
            payload = self.host.call_sync(
                "attendance.scan_qr",
                {"image_b64": base64.b64encode(image_bytes).decode("ascii")},
                timeout=180,
            )
        except ProcessPluginError as exc:
            raise QRScannerError(f"二维码扫描子进程不可用：{exc}") from exc
        if not isinstance(payload, dict):
            raise QRScannerError("二维码扫描子进程返回格式错误")
        error_type = str(payload.get("error_type") or "")
        message = str(payload.get("message") or "")
        if error_type == "unavailable":
            raise QRScannerUnavailable(message or "二维码扫描后端不可用")
        if error_type:
            raise QRScannerError(message or "二维码识别失败")
        decoded = payload.get("decoded")
        return [str(item) for item in decoded] if isinstance(decoded, list) else []


def setup_services(context: PluginContext) -> None:
    config = context.config
    services = context.services
    services.add(
        "ketangpai_client",
        KetangpaiClient(
            KetangpaiCredentialStore(config.data_dir / "account" / "ketangpai.json")
        ),
    )
    services.add("ketangpai_sms_sessions", {})
    services.add("ketangpai_sms_lock", threading.Lock())

    scanner_host = ProcessPluginHost(
        plugin_id="attendance-scanner",
        root=Path(__file__).resolve().parent,
        entry="worker.py",
        data_dir=Path(config.data_dir),
        rpc_registry=services.get("plugin_rpc"),
        rpc_api_version=PLUGIN_RPC_API_VERSION,
        rpc_permissions=(),
    )
    services.add("attendance_scanner_process_host", scanner_host)
    services.add("qr_scanner", AttendanceScannerProcessProxy(scanner_host))


async def startup(context: PluginContext) -> None:
    await context.services.get("attendance_scanner_process_host").start()


async def shutdown(context: PluginContext) -> None:
    await context.services.get("attendance_scanner_process_host").stop()


__all__ = [
    "AttendanceScannerProcessProxy",
    "setup_services",
    "shutdown",
    "startup",
]
