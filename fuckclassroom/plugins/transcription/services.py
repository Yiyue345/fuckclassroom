from __future__ import annotations

from .engine import LocalTranscriptionService
from ...core.plugins import PluginContext


def setup_services(context: PluginContext) -> None:
    context.services.add(
        "transcription_service",
        LocalTranscriptionService(context.config),
    )


__all__ = ["setup_services"]
