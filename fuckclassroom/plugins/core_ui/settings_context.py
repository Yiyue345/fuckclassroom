from __future__ import annotations

from typing import Any

from ...core.plugins import ServiceContainer


def build_network_settings_context(
    services: ServiceContainer,
    _request: Any,
) -> dict[str, object]:
    return {
        "credentials": services.get("credential_store").public_status(),
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
