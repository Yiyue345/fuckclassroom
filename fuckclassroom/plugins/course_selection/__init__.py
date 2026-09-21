from __future__ import annotations

import asyncio
from pathlib import Path

from ...core.plugins import (
    NavigationGroup,
    NavigationItem,
    PluginContext,
    PluginSpec,
    SettingsPanel,
)
from ...course_selection import CourseSelectionAssistant
from ...course_selection.automation import AutoSelectionService
from ...course_selection.proxy import Hy2ProxyManager


PLUGIN_DIR = Path(__file__).resolve().parent


def setup_services(context: PluginContext) -> None:
    config = context.config
    services = context.services
    hy2_proxy = Hy2ProxyManager(config)
    course_selection = CourseSelectionAssistant(
        config,
        hy2_proxy=hy2_proxy,
        credential_store=services.get("credential_store"),
        verification_broker=services.get("verification_broker"),
        login_lock=services.get("login_lock"),
    )
    runtime_settings = services.get("settings_store").load()
    auto_selection = AutoSelectionService(
        course_selection,
        config.data_dir / "webvpn" / "auto_selection_jobs.json",
        retry_seconds=runtime_settings.auto_selection_retry_seconds,
        session_retry_seconds=runtime_settings.auto_selection_session_retry_seconds,
        capacity_retry_seconds=runtime_settings.auto_selection_capacity_retry_seconds,
    )

    services.add("hy2_proxy", hy2_proxy)
    services.add("course_selection", course_selection)
    services.add("auto_selection", auto_selection)


async def startup(context: PluginContext) -> None:
    services = context.services
    course_selection = services.get("course_selection")
    auto_selection = services.get("auto_selection")

    def verify_selection_session_without_hy2():
        with course_selection.defer_hy2_start():
            return course_selection.verify_saved_session(
                timeout_ms=60_000,
                auto_relogin=True,
            )

    def refresh_selection_info_without_hy2():
        with course_selection.defer_hy2_start():
            return course_selection.refresh_selection_info_for_web(timeout_ms=60_000)

    async def verify_saved_session() -> None:
        try:
            result = await asyncio.to_thread(verify_selection_session_without_hy2)
        except Exception as exc:  # noqa: BLE001 - startup work must not stop the app.
            context.logger.warning("启动时WebVPN/教务会话检测失败：%s", exc)
            return

        context.logger.info("启动时WebVPN/教务会话状态：%s", result.message)
        if result.is_valid is True:
            try:
                await asyncio.to_thread(refresh_selection_info_without_hy2)
            except Exception as exc:  # noqa: BLE001 - startup refresh must not stop the app.
                context.logger.warning("启动时选课信息刷新失败：%s", exc)

    auto_selection.start()
    context.create_task(verify_saved_session())


def shutdown(context: PluginContext) -> None:
    context.services.get("auto_selection").stop()
    context.services.get("hy2_proxy").stop()


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
                    NavigationItem(
                        key="selection_info",
                        label="选课中心",
                        href="/selection/info",
                        icon="check-square",
                        active_keys=("selection_info",),
                    ),
                    NavigationItem(
                        key="selection_catalog",
                        label="全校开课",
                        href="/selection/catalog",
                        icon="book-open",
                        active_keys=("selection_catalog",),
                    ),
                    NavigationItem(
                        key="selection_selected",
                        label="已选课程",
                        href="/selection/info/selected",
                        icon="list-checks",
                        active_keys=("selection_selected",),
                        badge_template="selection_badge_selected.html",
                    ),
                    NavigationItem(
                        key="selection_auto",
                        label="定时选课",
                        href="/selection/auto",
                        icon="clock-3",
                        active_keys=("selection_auto",),
                    ),
                    NavigationItem(
                        key="selection_captures",
                        label="接口记录",
                        href="/selection/captures",
                        icon="activity",
                        active_keys=("selection_captures",),
                    ),
                ),
            ),
        ),
        settings_panels=(
            SettingsPanel(
                key="auto-selection",
                label="定时选课",
                template="selection_settings_auto.html",
                order=30,
            ),
        ),
        sidebar_templates=("selection_sidebar_session.html",),
        topbar_status_templates=("selection_topbar_status.html",),
        system_labels=(("selection", "本科选课"),),
    )


__all__ = ["build_plugin", "build_routes", "setup_services", "shutdown", "startup"]
