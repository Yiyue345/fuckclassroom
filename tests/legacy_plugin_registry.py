from __future__ import annotations

import importlib
from collections.abc import Iterable

from fuckclassroom.core.plugins import PluginRegistry


_LEGACY_MODULES = {
    "core_ui": "fuckclassroom.plugins.core_ui.plugin",
    "classroom": "fuckclassroom.plugins.classroom.plugin",
    "processing": "fuckclassroom.plugins.processing.plugin",
    "transcription": "fuckclassroom.plugins.transcription.plugin",
    "ocr": "fuckclassroom.plugins.ocr.plugin",
    "ai_summary": "fuckclassroom.plugins.ai_summary.plugin",
    "attendance": "fuckclassroom.plugins.attendance.plugin",
    "course_selection": "fuckclassroom.plugins.course_selection.plugin",
}


def build_legacy_business_registry(enabled: Iterable[str]) -> PluginRegistry:
    """Build dormant host plugin specs for implementation/UI regression tests only.

    Production discovery intentionally does not register these modules as builtins.
    The helper keeps host-side implementation tests useful until equivalent coverage
    has fully moved to the standalone plugin repositories.
    """
    specs = {}
    for plugin_id, module_name in _LEGACY_MODULES.items():
        module = importlib.import_module(module_name)
        specs[plugin_id] = module.build_plugin()

    selected = set(enabled)
    selected.add("core_ui")
    pending = list(selected)
    while pending:
        plugin_id = pending.pop()
        spec = specs[plugin_id]
        for dependency in spec.requires:
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)
    return PluginRegistry(specs[plugin_id] for plugin_id in selected)
