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


_LEGACY_REQUIRES = {
    "classroom": (),
    "processing": ("classroom",),
    "transcription": ("processing",),
    "ocr": ("processing",),
    "ai_summary": ("processing",),
    "attendance": ("classroom",),
    "course_selection": (),
}


def install_legacy_external_plugins(
    data_dir,
    enabled: Iterable[str],
) -> set[str]:
    """Install wrapper packages around dormant host implementations for tests."""
    from pathlib import Path
    import json

    data_dir = Path(data_dir)
    selected = set(enabled)
    pending = list(selected)
    while pending:
        plugin_id = pending.pop()
        for dependency in _LEGACY_REQUIRES.get(plugin_id, ()):
            if dependency not in selected:
                selected.add(dependency)
                pending.append(dependency)

    for plugin_id in sorted(selected):
        if plugin_id == "core_ui":
            continue
        if plugin_id not in _LEGACY_REQUIRES:
            raise KeyError(plugin_id)
        root = data_dir / "plugins" / plugin_id
        root.mkdir(parents=True, exist_ok=True)
        requires = _LEGACY_REQUIRES[plugin_id]
        (root / "plugin.json").write_text(
            json.dumps(
                {
                    "id": plugin_id,
                    "name": plugin_id,
                    "version": "test",
                    "api_version": 1,
                    "app_requires": ">=0.1,<0.3",
                    "requires": list(requires),
                    "entry": "plugin.py",
                }
            ),
            encoding="utf-8",
        )
        (root / "plugin.py").write_text(
            f"from fuckclassroom.plugins.{plugin_id}.plugin import build_plugin\n",
            encoding="utf-8",
        )
    return selected
