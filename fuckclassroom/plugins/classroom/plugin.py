from __future__ import annotations

from pathlib import Path

from ...core.plugins import NavigationGroup, NavigationItem, PluginContext, PluginSpec, SettingsPanel
from .lifecycle import startup
from .services import setup_services


PLUGIN_DIR = Path(__file__).resolve().parent


def build_routes(context: PluginContext):
    from .routes import build_router
    return build_router(context)


def build_plugin() -> PluginSpec:
    return PluginSpec(
        id="classroom",
        name="课程录播",
        order=10,
        service_factory=setup_services,
        route_factory=build_routes,
        startup=startup,
        template_dir=PLUGIN_DIR / "templates",
        static_dir=PLUGIN_DIR / "static",
        navigation_groups=(
            NavigationGroup(
                key="classroom",
                label="课程录播",
                aria_label="课程录播导航",
                system="classroom",
                order=10,
                status_template="classroom_nav_status.html",
                items=(
                    NavigationItem(key="overview", label="概览", href="/classroom", icon="layout-dashboard", active_keys=("overview",)),
                    NavigationItem(key="courses", label="我的课程", href="/courses", icon="book-open", active_keys=("courses",), badge_template="classroom_badge_courses.html"),
                    NavigationItem(key="tasks", label="提取与总结", href="/tasks", icon="list-todo", active_keys=("tasks",), badge_template="classroom_badge_tasks.html"),
                    NavigationItem(key="downloads", label="文件管理", href="/downloads", icon="folder", active_keys=("downloads",)),
                ),
            ),
        ),
        settings_panels=(
            SettingsPanel(key="courses", label="课程与资源", template="classroom_settings_courses.html", order=20, checkbox_fields=("auto_open_outputs_after_extract",)),
            SettingsPanel(
                key="extraction",
                label="文字提取",
                template="classroom_settings_extraction.html",
                order=50,
                checkbox_fields=("ocr_cache_enabled", "ocr_use_angle_cls", "asr_use_batched", "asr_simplified_chinese"),
            ),
            SettingsPanel(key="ai", label="AI 服务", template="classroom_settings_ai.html", order=60, checkbox_fields=("auto_summarize_after_extract",)),
        ),
        sidebar_templates=("classroom_sidebar_session.html",),
        topbar_status_templates=("classroom_topbar_status.html",),
        system_labels=(("classroom", "课程录播"),),
    )


__all__ = ["build_plugin"]
