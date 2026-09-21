from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Iterable


@dataclass(frozen=True, slots=True)
class NavigationItem:
    key: str
    label: str
    href: str
    icon: str
    active_keys: tuple[str, ...]
    badge_template: str | None = None


@dataclass(frozen=True, slots=True)
class NavigationGroup:
    key: str
    label: str
    aria_label: str
    system: str
    order: int
    items: tuple[NavigationItem, ...]
    status_template: str | None = None


@dataclass(frozen=True, slots=True)
class SettingsPanel:
    key: str
    label: str
    template: str
    order: int
    in_form: bool = True
    show_in_nav: bool = True
    checkbox_fields: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PluginSpec:
    id: str
    name: str
    order: int = 100
    navigation_groups: tuple[NavigationGroup, ...] = ()
    settings_panels: tuple[SettingsPanel, ...] = ()
    sidebar_templates: tuple[str, ...] = ()
    topbar_status_templates: tuple[str, ...] = ()
    system_labels: tuple[tuple[str, str], ...] = ()
    template_dir: Path | None = None
    static_dir: Path | None = None


@dataclass(frozen=True, slots=True)
class StaticMount:
    plugin_id: str
    path: str
    name: str
    directory: Path


class PluginRegistry:
    """Registry for UI-facing plugin contributions.

    This first layer intentionally owns presentation concerns only: navigation,
    settings panels, template roots and static assets. Business routes/services
    remain unchanged until they can be migrated independently.
    """

    def __init__(self, plugins: Iterable[PluginSpec] = ()) -> None:
        self._plugins: dict[str, PluginSpec] = {}
        for plugin in plugins:
            self.register(plugin)

    def register(self, plugin: PluginSpec) -> None:
        if plugin.id in self._plugins:
            raise ValueError(f"duplicate plugin id: {plugin.id}")
        self._plugins[plugin.id] = plugin

    @property
    def plugins(self) -> tuple[PluginSpec, ...]:
        return tuple(sorted(self._plugins.values(), key=lambda item: (item.order, item.id)))

    @property
    def navigation_groups(self) -> tuple[NavigationGroup, ...]:
        groups = [group for plugin in self.plugins for group in plugin.navigation_groups]
        return tuple(sorted(groups, key=lambda item: (item.order, item.key)))

    @property
    def settings_panels(self) -> tuple[SettingsPanel, ...]:
        panels = [panel for plugin in self.plugins for panel in plugin.settings_panels]
        return tuple(sorted(panels, key=lambda item: (item.order, item.key)))

    @property
    def sidebar_templates(self) -> tuple[str, ...]:
        return tuple(
            template
            for plugin in self.plugins
            for template in plugin.sidebar_templates
        )

    @property
    def topbar_status_templates(self) -> tuple[str, ...]:
        return tuple(
            template
            for plugin in self.plugins
            for template in plugin.topbar_status_templates
        )

    @property
    def active_checkbox_fields(self) -> frozenset[str]:
        return frozenset(
            field
            for panel in self.settings_panels
            if panel.in_form
            for field in panel.checkbox_fields
        )

    def template_directories(self, core_directory: Path) -> list[str]:
        directories = [str(core_directory)]
        directories.extend(
            str(plugin.template_dir)
            for plugin in self.plugins
            if plugin.template_dir is not None and plugin.template_dir.exists()
        )
        return directories

    @property
    def static_mounts(self) -> tuple[StaticMount, ...]:
        mounts: list[StaticMount] = []
        for plugin in self.plugins:
            if plugin.static_dir is None or not plugin.static_dir.exists():
                continue
            mounts.append(
                StaticMount(
                    plugin_id=plugin.id,
                    path=f"/plugins/{plugin.id}/static",
                    name=f"plugin-{plugin.id}-static",
                    directory=plugin.static_dir,
                )
            )
        return tuple(mounts)

    def system_label(self, system: str) -> str:
        labels = {
            key: label
            for plugin in self.plugins
            for key, label in plugin.system_labels
        }
        return labels.get(system, system)


__all__ = [
    "NavigationGroup",
    "NavigationItem",
    "PluginRegistry",
    "PluginSpec",
    "SettingsPanel",
    "StaticMount",
]
