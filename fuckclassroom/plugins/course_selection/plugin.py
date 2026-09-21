from __future__ import annotations

from pathlib import Path

from ...core.plugins import NavigationGroup, NavigationItem, PluginContext, PluginSpec, SettingsPanel


PLUGIN_DIR = Path(__file__).resolve().parent

def setup_services(context: PluginContext):
    from .services import setup_services as setup
    return setup(context)

def startup(context: PluginContext):
    from .lifecycle import startup as hook
    return hook(context)

def shutdown(context: PluginContext):
    from .lifecycle import shutdown as hook
    return hook(context)


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="course_selection",
        name="本科选课",
        order=20,
        service_factory=setup_services,
        route_factory=build_routes,
        startup=startup,
        shutdown=shutdown,
        template_dir=PLUGIN_DIR / "templates",
        static_dir=PLUGIN_DIR / "static",
        navigation_groups=(
            NavigationGroup(
                key="selection",
                label="本科选课",
                aria_label="本科选课导航",
                system="selection",
                order=20,
                status_template="selection_nav_status.html",
                items=(
                    NavigationItem(key="selection_info", label="选课中心", href="/selection/info", icon="check-square", active_keys=("selection_info",)),
                    NavigationItem(key="selection_catalog", label="全校开课", href="/selection/catalog", icon="book-open", active_keys=("selection_catalog",)),
                    NavigationItem(key="selection_selected", label="已选课程", href="/selection/info/selected", icon="list-checks", active_keys=("selection_selected",), badge_template="selection_badge_selected.html"),
                    NavigationItem(key="selection_auto", label="定时选课", href="/selection/auto", icon="clock-3", active_keys=("selection_auto",)),
                    NavigationItem(key="selection_captures", label="接口记录", href="/selection/captures", icon="activity", active_keys=("selection_captures",)),
                ),
            ),
        ),
        settings_panels=(
            SettingsPanel(key="auto-selection", label="定时选课", template="selection_settings_auto.html", order=30),
        ),
        sidebar_templates=("selection_sidebar_session.html",),
        topbar_status_templates=("selection_topbar_status.html",),
        system_labels=(("selection", "本科选课"),),
    )


__all__ = ["build_plugin"]
