from __future__ import annotations

from ...classroom.ai import AIClient
from ...core.plugins import PluginContext


def setup_services(context: PluginContext) -> None:
    context.services.add("ai_summary_service", AIClient(context.config))


__all__ = ["setup_services"]
