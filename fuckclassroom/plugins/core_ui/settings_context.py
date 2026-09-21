from __future__ import annotations

from typing import Any

from ...core.plugins import ServiceContainer


def build_network_settings_context(
    services: ServiceContainer,
    _request: Any,
) -> dict[str, object]:
    hy2_proxy = services.maybe("hy2_proxy")
    return {
        "credentials": services.get("credential_store").public_status(),
        "hy2_status": (
            hy2_proxy.get_status().to_dict()
            if hy2_proxy is not None
            else {"message": "本科选课插件未启用"}
        ),
    }


def build_storage_settings_context(
    services: ServiceContainer,
    _request: Any,
) -> dict[str, object]:
    return {
        "cache_info": services.get("cache_store").info(),
    }


__all__ = [
    "build_network_settings_context",
    "build_storage_settings_context",
]
