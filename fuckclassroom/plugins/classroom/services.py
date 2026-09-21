from __future__ import annotations

import threading

from ...auth import LoginSessionService
from ...classroom import ClassroomClient, SemesterSyncService
from ...classroom.downloads import DownloadLibrary
from ...classroom.extraction import LessonExtractionService
from ...classroom.ketangpai import KetangpaiClient, KetangpaiCredentialStore
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


__all__ = ["setup_services"]
