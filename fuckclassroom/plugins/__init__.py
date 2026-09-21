from __future__ import annotations

from collections.abc import Iterable

from ..core.plugins import PluginRegistry
from .classroom import build_plugin as build_classroom_plugin
from .core_ui import build_plugin as build_core_ui_plugin
from .course_selection import build_plugin as build_course_selection_plugin


def build_plugin_registry(enabled: Iterable[str] | None = None) -> PluginRegistry:
    specs = (
        build_core_ui_plugin(),
        build_classroom_plugin(),
        build_course_selection_plugin(),
    )
    if enabled is None:
        return PluginRegistry(specs)

    enabled_ids = set(enabled)
    enabled_ids.add("core_ui")
    return PluginRegistry(plugin for plugin in specs if plugin.id in enabled_ids)


__all__ = ["build_plugin_registry"]
