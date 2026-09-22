from __future__ import annotations

import asyncio
import json
import socket
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping


_RPC_MAX_MESSAGE_BYTES = 4 * 1024 * 1024


class ProcessPluginRpcError(RuntimeError):
    def __init__(self, message: str, *, code: str = "rpc_error") -> None:
        super().__init__(message)
        self.code = code


class ProcessPluginRpcClient:
    """Child-side client for the permission-checked Plugin RPC API."""

    def __init__(
        self,
        *,
        host: str,
        port: int,
        token: str,
        api_version: int,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.token = token
        self.api_version = int(api_version)

    def call(self, method: str, params: Mapping[str, object] | None = None) -> object:
        payload = {
            "token": self.token,
            "rpc_api_version": self.api_version,
            "method": str(method),
            "params": dict(params or {}),
        }
        encoded = (
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        if len(encoded) > _RPC_MAX_MESSAGE_BYTES:
            raise ProcessPluginRpcError("RPC 请求超过 4 MiB", code="request_too_large")

        try:
            with socket.create_connection((self.host, self.port), timeout=5) as connection:
                connection.settimeout(65)
                stream = connection.makefile("rwb")
                stream.write(encoded)
                stream.flush()
                raw = stream.readline(_RPC_MAX_MESSAGE_BYTES + 1)
        except OSError as exc:
            raise ProcessPluginRpcError(f"无法连接主进程 RPC：{exc}", code="transport_error") from exc

        if not raw or len(raw) > _RPC_MAX_MESSAGE_BYTES:
            raise ProcessPluginRpcError("主进程 RPC 响应为空或过大", code="transport_error")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProcessPluginRpcError("主进程 RPC 返回无效 JSON", code="transport_error") from exc

        if not response.get("ok"):
            error = response.get("error")
            if isinstance(error, dict):
                message = str(error.get("message") or "RPC 调用失败")
                code = str(error.get("code") or "rpc_error")
            else:
                message = str(error or "RPC 调用失败")
                code = "rpc_error"
            raise ProcessPluginRpcError(message, code=code)
        return response.get("result")

    async def call_async(
        self,
        method: str,
        params: Mapping[str, object] | None = None,
    ) -> object:
        return await asyncio.to_thread(self.call, method, params)


@dataclass(frozen=True, slots=True)
class ProcessPluginContext:
    plugin_id: str
    plugin_root: Path
    data_dir: Path
    rpc: ProcessPluginRpcClient


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
    "ProcessPluginRpcClient",
    "ProcessPluginRpcError",
    "json_response",
]
