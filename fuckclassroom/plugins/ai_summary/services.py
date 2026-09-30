from __future__ import annotations

import base64
from pathlib import Path

from .errors import AIClientError
from ...core.plugins import PluginContext
from ..process_runtime import ProcessPluginError, ProcessPluginHost
from ..rpc import PLUGIN_RPC_API_VERSION


class AiSummaryProcessProxy:
    def __init__(self, host: ProcessPluginHost) -> None:
        self.host = host

    def summarize_course_text(self, text: str) -> str:
        try:
            result = self.host.call_sync(
                "ai_summary.summarize",
                {"text": text},
                timeout=30 * 60,
            )
        except ProcessPluginError as exc:
            raise AIClientError(str(exc)) from exc
        return str(result or "")

    def ocr_image(self, image_bytes: bytes, media_type: str) -> str:
        try:
            result = self.host.call_sync(
                "ai_summary.ocr",
                {
                    "image_b64": base64.b64encode(image_bytes).decode("ascii"),
                    "media_type": media_type,
                },
                timeout=10 * 60,
            )
        except ProcessPluginError as exc:
            raise AIClientError(str(exc)) from exc
        return str(result or "")


def setup_services(context: PluginContext) -> None:
    host = ProcessPluginHost(
        plugin_id="ai_summary",
        root=Path(__file__).resolve().parent,
        entry="worker.py",
        data_dir=Path(context.config.data_dir),
        rpc_registry=context.services.get("plugin_rpc"),
        rpc_api_version=PLUGIN_RPC_API_VERSION,
        rpc_permissions=(),
    )
    proxy = AiSummaryProcessProxy(host)
    context.services.add("ai_summary_process_host", host)
    context.services.add("ai_summary_service", proxy)

    rpc_registry = context.services.get("plugin_rpc")

    def rpc_ocr_image(_rpc_context, params):
        encoded = str(params.get("image_b64") or "")
        media_type = str(params.get("media_type") or "image/png")
        return proxy.ocr_image(
            base64.b64decode(encoded, validate=False),
            media_type,
        )

    rpc_registry.register("ai_summary.ocr.image", rpc_ocr_image)


async def startup(context: PluginContext) -> None:
    await context.services.get("ai_summary_process_host").start()


async def shutdown(context: PluginContext) -> None:
    await context.services.get("ai_summary_process_host").stop()


__all__ = [
    "AiSummaryProcessProxy",
    "setup_services",
    "shutdown",
    "startup",
]
