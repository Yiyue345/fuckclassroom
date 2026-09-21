from __future__ import annotations

import asyncio
import inspect
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Iterable


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


class ServiceContainer:
    """Small explicit service container shared by enabled plugins."""

    def __init__(self) -> None:
        self._services: dict[str, object] = {}

    def add(self, key: str, service: object) -> object:
        if key in self._services:
            raise ValueError(f"duplicate service: {key}")
        self._services[key] = service
        return service

    def get(self, key: str) -> Any:
        try:
            return self._services[key]
        except KeyError as exc:
            raise KeyError(f"service is not available: {key}") from exc

    def maybe(self, key: str, default: Any = None) -> Any:
        return self._services.get(key, default)

    def has(self, key: str) -> bool:
        return key in self._services

    @property
    def keys(self) -> tuple[str, ...]:
        return tuple(self._services)


@dataclass(slots=True)
class PluginContext:
    plugin_id: str
    config: object
    services: ServiceContainer
    logger: Any
    _runtime: "PluginRuntime"

    @property
    def registry(self) -> "PluginRegistry":
        return self._runtime.registry

    def create_task(self, awaitable: Any) -> asyncio.Task[Any]:
        return self._runtime.create_background_task(self.plugin_id, awaitable)


PluginHook = Callable[[PluginContext], object]


@dataclass(frozen=True, slots=True)
class PluginSpec:
    id: str
    name: str
    order: int = 100
    requires: tuple[str, ...] = ()
    navigation_groups: tuple[NavigationGroup, ...] = ()
    settings_panels: tuple[SettingsPanel, ...] = ()
    sidebar_templates: tuple[str, ...] = ()
    topbar_status_templates: tuple[str, ...] = ()
    system_labels: tuple[tuple[str, str], ...] = ()
    template_dir: Path | None = None
    static_dir: Path | None = None
    service_factory: PluginHook | None = None
    route_factory: PluginHook | None = None
    startup: PluginHook | None = None
    shutdown: PluginHook | None = None


@dataclass(frozen=True, slots=True)
class StaticMount:
    plugin_id: str
    path: str
    name: str
    directory: Path


class PluginRegistry:
    def __init__(self, plugins: Iterable[PluginSpec] = ()) -> None:
        self._plugins: dict[str, PluginSpec] = {}
        for plugin in plugins:
            self.register(plugin)
        self.validate_dependencies()

    def register(self, plugin: PluginSpec) -> None:
        if plugin.id in self._plugins:
            raise ValueError(f"duplicate plugin id: {plugin.id}")
        self._plugins[plugin.id] = plugin

    def validate_dependencies(self) -> None:
        for plugin in self.plugins:
            missing = [item for item in plugin.requires if item not in self._plugins]
            if missing:
                raise ValueError(
                    f"plugin {plugin.id} requires disabled plugins: {', '.join(missing)}"
                )

    def is_enabled(self, plugin_id: str) -> bool:
        return plugin_id in self._plugins

    def get(self, plugin_id: str) -> PluginSpec:
        try:
            return self._plugins[plugin_id]
        except KeyError as exc:
            raise KeyError(f"plugin is not enabled: {plugin_id}") from exc

    @property
    def plugins(self) -> tuple[PluginSpec, ...]:
        return tuple(sorted(self._plugins.values(), key=lambda item: (item.order, item.id)))

    @property
    def plugin_ids(self) -> tuple[str, ...]:
        return tuple(plugin.id for plugin in self.plugins)

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
        return tuple(template for plugin in self.plugins for template in plugin.sidebar_templates)

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


class PluginRuntime:
    """Owns enabled plugin services, routes and lifecycle hooks."""

    def __init__(self, registry: PluginRegistry, config: object, logger: Any) -> None:
        self.registry = registry
        self.config = config
        self.logger = logger
        self.services = ServiceContainer()
        self._routers: dict[str, object] = {}
        self._background_tasks: dict[str, list[asyncio.Task[Any]]] = {}

    def _context(self, plugin_id: str) -> PluginContext:
        return PluginContext(
            plugin_id=plugin_id,
            config=self.config,
            services=self.services,
            logger=self.logger,
            _runtime=self,
        )

    def initialize_services(self) -> None:
        for plugin in self.registry.plugins:
            if plugin.service_factory is not None:
                plugin.service_factory(self._context(plugin.id))

    def initialize_routes(self) -> None:
        for plugin in self.registry.plugins:
            if plugin.route_factory is None:
                continue
            router = plugin.route_factory(self._context(plugin.id))
            if router is not None:
                self.attach_router(plugin.id, router)

    def attach_router(self, plugin_id: str, router: object) -> None:
        if not self.registry.is_enabled(plugin_id):
            return
        if plugin_id in self._routers:
            raise ValueError(f"router already attached for plugin: {plugin_id}")
        self._routers[plugin_id] = router

    def include_routes(self, app: Any) -> None:
        for plugin in self.registry.plugins:
            router = self._routers.get(plugin.id)
            if router is None:
                continue
            for route in tuple(getattr(router, "routes", ())):
                app.router.routes.append(route)

    def create_background_task(self, plugin_id: str, awaitable: Any) -> asyncio.Task[Any]:
        task = asyncio.create_task(awaitable)
        self._background_tasks.setdefault(plugin_id, []).append(task)
        return task

    async def start(self) -> None:
        for plugin in self.registry.plugins:
            if plugin.startup is None:
                continue
            result = plugin.startup(self._context(plugin.id))
            if inspect.isawaitable(result):
                await result

    async def stop(self) -> None:
        tasks = [
            task
            for plugin_tasks in self._background_tasks.values()
            for task in plugin_tasks
            if not task.done()
        ]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception as exc:  # noqa: BLE001 - lifecycle cleanup must continue.
                self.logger.warning("插件后台任务退出异常：%s", exc)

        for plugin in reversed(self.registry.plugins):
            if plugin.shutdown is None:
                continue
            try:
                result = plugin.shutdown(self._context(plugin.id))
                if inspect.isawaitable(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - one plugin must not block shutdown.
                self.logger.warning("插件 %s 停止失败：%s", plugin.id, exc)


__all__ = [
    "NavigationGroup",
    "NavigationItem",
    "PluginContext",
    "PluginRegistry",
    "PluginRuntime",
    "PluginSpec",
    "ServiceContainer",
    "SettingsPanel",
    "StaticMount",
]
