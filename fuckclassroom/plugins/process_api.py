from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


@dataclass(frozen=True, slots=True)
class ProcessPluginContext:
    plugin_id: str
    plugin_root: Path
    data_dir: Path


@dataclass(frozen=True, slots=True)
class ProcessPluginRequest:
    method: str
    path: str
    query_string: str
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True, slots=True)
class ProcessPluginResponse:
    body: bytes = b""
    status_code: int = 200
    headers: Mapping[str, str] | None = None


def json_response(
    payload: object,
    *,
    status_code: int = 200,
    headers: Mapping[str, str] | None = None,
) -> ProcessPluginResponse:
    merged = {"content-type": "application/json; charset=utf-8"}
    if headers:
        merged.update({str(key).lower(): str(value) for key, value in headers.items()})
    return ProcessPluginResponse(
        body=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        status_code=status_code,
        headers=merged,
    )


__all__ = [
    "ProcessPluginContext",
    "ProcessPluginRequest",
    "ProcessPluginResponse",
    "json_response",
]
