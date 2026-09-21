from __future__ import annotations

from collections.abc import Iterable

from ..core.config import AppConfig
from .manager import PluginManager, build_registry


def build_plugin_registry(
    enabled: Iterable[str] | None = None,
    *,
    config: AppConfig | None = None,
    manager: PluginManager | None = None,
):
    app_config = config or AppConfig()
    plugin_manager = manager or PluginManager(app_config)
    return build_registry(plugin_manager, enabled)


__all__ = ["PluginManager", "build_plugin_registry"]
