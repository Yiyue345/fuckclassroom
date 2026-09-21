from __future__ import annotations

import threading

from ...classroom.ketangpai import KetangpaiClient, KetangpaiCredentialStore
from ...core.plugins import PluginContext


def setup_services(context: PluginContext) -> None:
    config = context.config
    services = context.services
    services.add(
        "ketangpai_client",
        KetangpaiClient(
            KetangpaiCredentialStore(config.data_dir / "account" / "ketangpai.json")
        ),
    )
    services.add("ketangpai_sms_sessions", {})
    services.add("ketangpai_sms_lock", threading.Lock())


__all__ = ["setup_services"]
