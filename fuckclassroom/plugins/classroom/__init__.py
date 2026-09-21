from __future__ import annotations

import asyncio
import threading
from pathlib import Path

from ...auth import LoginSessionService
from ...classroom import ClassroomClient, ClassroomClientError, SemesterSyncService
from ...classroom.downloads import DownloadLibrary
from ...classroom.extraction import LessonExtractionService
from ...classroom.ketangpai import KetangpaiClient, KetangpaiCredentialStore
from ...core.plugins import (
    NavigationGroup,
    NavigationItem,
    PluginContext,
    PluginSpec,
    SettingsPanel,
)


PLUGIN_DIR = Path(__file__).resolve().parent


def setup_services(context: PluginContext) -> None:
    config = context.config
    services = context.services
    classroom_client = ClassroomClient(config)
    semester_sync = SemesterSyncService(
        classroom_client,
        config.cache_dir / "classroom" / "semesters.json",
    )
    auth_service = LoginSessionService(
        config,
        credential_store=services.get("credential_store"),
        verification_broker=services.get("verification_broker"),
        login_lock=services.get("login_lock"),
    )
    classroom_client.set_session_recoverer(
        lambda: auth_service.verify_saved_session(auto_relogin=True)
    )
    ketangpai_client = KetangpaiClient(
        KetangpaiCredentialStore(config.data_dir / "account" / "ketangpai.json")
    )

    services.add("classroom_client", classroom_client)
    services.add("semester_sync", semester_sync)
    services.add("auth_service", auth_service)
    services.add("ketangpai_client", ketangpai_client)
    services.add("extraction_service", LessonExtractionService(config))
    services.add("download_library", DownloadLibrary(config.downloads_dir))
    services.add("ketangpai_sms_sessions", {})
    services.add("ketangpai_sms_lock", threading.Lock())


async def startup(context: PluginContext) -> None:
    services = context.services
    semester_sync = services.get("semester_sync")
    auth_service = services.get("auth_service")
    ketangpai_client = services.get("ketangpai_client")

    async def sync_semesters() -> None:
        try:
            snapshot = await asyncio.to_thread(semester_sync.sync, force=True)
        except Exception as exc:  # noqa: BLE001 - startup work must not stop the app.
            context.logger.warning("启动时学期同步失败：%s", exc)
            return
        if snapshot.error:
            context.logger.warning("启动时学期同步失败，已使用缓存：%s", snapshot.error)
        else:
            context.logger.info("启动时已同步 %s 个学期", len(snapshot.terms))

    async def verify_sessions() -> None:
        results = await asyncio.gather(
            asyncio.to_thread(auth_service.verify_saved_session, auto_relogin=True),
            asyncio.to_thread(ketangpai_client.verify_session, auto_relogin=True),
            return_exceptions=True,
        )
        for label, result in zip(("课程录播", "课堂派"), results):
            if isinstance(result, Exception):
                context.logger.warning("启动时%s会话检测失败：%s", label, result)
            else:
                context.logger.info("启动时%s会话状态：%s", label, result.message)

    context.create_task(sync_semesters())
    context.create_task(verify_sessions())


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
                    NavigationItem(
                        key="overview",
                        label="概览",
                        href="/classroom",
                        icon="layout-dashboard",
                        active_keys=("overview",),
                    ),
                    NavigationItem(
                        key="courses",
                        label="我的课程",
                        href="/courses",
                        icon="book-open",
                        active_keys=("courses",),
                        badge_template="classroom_badge_courses.html",
                    ),
                    NavigationItem(
                        key="tasks",
                        label="提取与总结",
                        href="/tasks",
                        icon="list-todo",
                        active_keys=("tasks",),
                        badge_template="classroom_badge_tasks.html",
                    ),
                    NavigationItem(
                        key="downloads",
                        label="文件管理",
                        href="/downloads",
                        icon="folder",
                        active_keys=("downloads",),
                    ),
                ),
            ),
        ),
        settings_panels=(
            SettingsPanel(
                key="courses",
                label="课程与资源",
                template="classroom_settings_courses.html",
                order=20,
                checkbox_fields=("auto_open_outputs_after_extract",),
            ),
            SettingsPanel(
                key="extraction",
                label="文字提取",
                template="classroom_settings_extraction.html",
                order=50,
                checkbox_fields=(
                    "ocr_cache_enabled",
                    "ocr_use_angle_cls",
                    "asr_use_batched",
                    "asr_simplified_chinese",
                ),
            ),
            SettingsPanel(
                key="ai",
                label="AI 服务",
                template="classroom_settings_ai.html",
                order=60,
                checkbox_fields=("auto_summarize_after_extract",),
            ),
        ),
        sidebar_templates=("classroom_sidebar_session.html",),
        topbar_status_templates=("classroom_topbar_status.html",),
        system_labels=(("classroom", "课程录播"),),
    )


__all__ = ["build_plugin", "build_routes", "setup_services", "startup"]
