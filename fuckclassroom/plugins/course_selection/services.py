from __future__ import annotations

from ...core.plugins import PluginContext
from ...course_selection import CourseSelectionAssistant
from ...course_selection.automation import AutoSelectionService
from ...course_selection.proxy import Hy2ProxyManager


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


__all__ = ["setup_services"]
