from __future__ import annotations

import base64

from fuckclassroom.core.config import AppConfig

from .client import AIClient


_client: AIClient | None = None


def _get_client(context) -> AIClient:
    global _client
    if _client is None:
        _client = AIClient(AppConfig(data_dir=context.data_dir))
    return _client


def handle_call(method, params, context, progress):
    client = _get_client(context)
    if method == "ai_summary.summarize":
        return client.summarize_course_text(str(params.get("text") or ""))
    if method == "ai_summary.ocr":
        return client.ocr_image(
            base64.b64decode(str(params.get("image_b64") or ""), validate=False),
            str(params.get("media_type") or "image/png"),
        )
    raise ValueError(f"未知 AI Summary Worker 方法：{method}")
