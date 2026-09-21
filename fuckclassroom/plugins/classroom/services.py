from __future__ import annotations

from ...auth import LoginSessionService
from ...classroom import ClassroomClient, SemesterSyncService
from ...classroom.downloads import DownloadLibrary
from ...core.plugins import PluginContext


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

    services.add("classroom_client", classroom_client)
    services.add("semester_sync", semester_sync)
    services.add("auth_service", auth_service)
    services.add("download_library", DownloadLibrary(config.downloads_dir))


__all__ = ["setup_services"]
