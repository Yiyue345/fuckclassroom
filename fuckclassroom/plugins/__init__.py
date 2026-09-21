from __future__ import annotations

import os
from collections.abc import Iterable

from ..core.plugins import PluginRegistry, PluginSpec
from .ai_summary import build_plugin as build_ai_summary_plugin
from .attendance import build_plugin as build_attendance_plugin
from .classroom import build_plugin as build_classroom_plugin
from .core_ui import build_plugin as build_core_ui_plugin
from .course_selection import build_plugin as build_course_selection_plugin
from .ocr import build_plugin as build_ocr_plugin
from .processing import build_plugin as build_processing_plugin
from .transcription import build_plugin as build_transcription_plugin


def _enabled_from_env() -> tuple[str, ...] | None:
    raw = os.environ.get("FUCKCLASSROOM_PLUGINS")
    if raw is None:
        return None
    return tuple(
        value.strip()
        for value in raw.replace(";", ",").split(",")
        if value.strip()
    )


def _expand_dependencies(
    specs: tuple[PluginSpec, ...],
    enabled: Iterable[str],
) -> set[str]:
    by_id = {plugin.id: plugin for plugin in specs}
    enabled_ids = set(enabled)
    enabled_ids.add("core_ui")
    unknown = enabled_ids.difference(by_id)
    if unknown:
        raise ValueError(f"unknown plugins: {', '.join(sorted(unknown))}")

    pending = list(enabled_ids)
    while pending:
        plugin_id = pending.pop()
        for dependency in by_id[plugin_id].requires:
            if dependency not in enabled_ids:
                enabled_ids.add(dependency)
                pending.append(dependency)
    return enabled_ids


def build_plugin_registry(enabled: Iterable[str] | None = None) -> PluginRegistry:
    specs = (
        build_core_ui_plugin(),
        build_classroom_plugin(),
        build_processing_plugin(),
        build_transcription_plugin(),
        build_ocr_plugin(),
        build_ai_summary_plugin(),
        build_attendance_plugin(),
        build_course_selection_plugin(),
    )
    if enabled is None:
        enabled = _enabled_from_env()
    if enabled is None:
        return PluginRegistry(specs)

    enabled_ids = _expand_dependencies(specs, enabled)
    return PluginRegistry(plugin for plugin in specs if plugin.id in enabled_ids)


__all__ = ["build_plugin_registry"]
