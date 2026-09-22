from __future__ import annotations

from pathlib import Path

from ...core.plugins import NavigationGroup, NavigationItem, PluginContext, PluginSpec, SettingsPanel


PLUGIN_DIR = Path(__file__).resolve().parent

def setup_services(context: PluginContext):
    from .services import setup_services as setup
    return setup(context)


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def build_network_settings_context(services, request):
    from .settings_context import build_network_settings_context as build
    return build(services, request)


def build_storage_settings_context(services, request):
    from .settings_context import build_storage_settings_context as build
    return build(services, request)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="core_ui",
        name="通用",
        order=0,
        service_factory=setup_services,
        route_factory=build_routes,
        template_dir=PLUGIN_DIR / "templates",
        static_dir=PLUGIN_DIR / "static",
        stylesheets=("/plugins/core_ui/static/core_ui.css?v=20260922-1",),
        navigation_groups=(
            NavigationGroup(
                key="common",
                label="通用",
                aria_label="通用导航",
                system="common",
                order=90,
                items=(
                    NavigationItem(key="tasks", label="任务中心", href="/tasks", icon="list-todo", active_keys=("tasks",)),
                    NavigationItem(key="accounts", label="账户与认证", href="/accounts", icon="shield-check", active_keys=("accounts",)),
                    NavigationItem(key="plugins", label="插件管理", href="/plugins", icon="settings", active_keys=("plugins",)),
                    NavigationItem(key="settings", label="设置", href="/settings", icon="settings", active_keys=("settings",)),
                ),
            ),
        ),
        settings_panels=(
            SettingsPanel(key="appearance", label="外观", template="core_settings_appearance.html", order=10),
            SettingsPanel(key="network", label="统一登录", template="core_settings_network.html", order=40, context_factory=build_network_settings_context),
            SettingsPanel(key="settings-actions", label="设置操作", template="core_settings_actions.html", order=80, in_form=False, show_in_nav=False),
            SettingsPanel(key="storage", label="缓存与存储", template="core_settings_storage.html", order=90, in_form=False, context_factory=build_storage_settings_context),
        ),
        system_labels=(("common", "通用"), ("accounts", "账户")),
    )


__all__ = ["build_plugin"]
