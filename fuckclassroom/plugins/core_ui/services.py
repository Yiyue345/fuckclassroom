from __future__ import annotations

import threading
from pathlib import Path

from ...auth.cas import LoginVerificationBroker
from ...auth.credentials import SelectionCredentialStore
from ...core.cache import JsonCache
from ...core.plugins import PluginContext
from ...core.settings import SettingsStore
from ...core.tasks import TaskManager


def _credential_path(config) -> Path:
    target = config.data_dir / "account" / "credentials.json"
    legacy = config.data_dir / "webvpn" / "credentials.json"
    if target.exists() or not legacy.exists():
        return target
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        legacy.replace(target)
        return target
    except OSError:
        return legacy


def setup_services(context: PluginContext) -> None:
    config = context.config
    context.services.add("settings_store", SettingsStore(config.data_dir / "settings.json"))
    context.services.add("cache_store", JsonCache(config.cache_dir))
    if not context.services.has("task_manager"):
        context.services.add("task_manager", TaskManager())
    context.services.add(
        "credential_store",
        SelectionCredentialStore(_credential_path(config)),
    )
    context.services.add("verification_broker", LoginVerificationBroker())
    context.services.add("login_lock", threading.RLock())
    from ...auth.webvpn import WebVpnSessionService
    context.services.add(
        "webvpn_session",
        WebVpnSessionService(
            config,
            credential_store=context.services.get("credential_store"),
            verification_broker=context.services.get("verification_broker"),
            login_lock=context.services.get("login_lock"),
        ),
    )


__all__ = ["setup_services"]
