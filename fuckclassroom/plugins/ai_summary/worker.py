from __future__ import annotations

from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.ai_summary.client import AIClient


_client: AIClient | None = None


def _get_client(context) -> AIClient:
    global _client
    if _client is None:
        _client = AIClient(AppConfig(data_dir=context.data_dir))
    return _client


def handle_call(method, params, context, progress):
    if method != "ai_summary.summarize":
        raise ValueError(f"未知 AI Summary Worker 方法：{method}")
    return _get_client(context).summarize_course_text(str(params.get("text") or ""))
