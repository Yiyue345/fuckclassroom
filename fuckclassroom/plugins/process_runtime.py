from __future__ import annotations

import asyncio
import base64
import json
import os
import secrets
import subprocess
import sys
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse, Response

from ..core.config import PROJECT_ROOT
from ..core.plugins import PluginContext, PluginSpec


_HOP_BY_HOP_HEADERS = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailer",
    "transfer-encoding",
    "upgrade",
    "content-length",
}


class ProcessPluginError(RuntimeError):
    pass


class ProcessPluginHost:
    def __init__(
        self,
        *,
        plugin_id: str,
        root: Path,
        entry: str,
        data_dir: Path,
    ) -> None:
        self.plugin_id = plugin_id
        self.root = root
        self.entry = entry
        self.data_dir = data_dir
        self._token = secrets.token_urlsafe(32)
        self._process: asyncio.subprocess.Process | None = None
        self._port = 0
        self._pid = 0
        self._stderr_task: asyncio.Task[None] | None = None
        self._stderr_lines: list[str] = []

    @property
    def pid(self) -> int:
        return self._pid

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.returncode is None and self._port > 0

    async def _drain_stderr(self, stream: asyncio.StreamReader) -> None:
        while True:
            line = await stream.readline()
            if not line:
                return
            text = line.decode("utf-8", errors="replace").rstrip()
            if text:
                self._stderr_lines.append(text)
                del self._stderr_lines[:-40]

    def _error_detail(self) -> str:
        return "\n".join(self._stderr_lines[-8:])

    async def start(self) -> None:
        if self.running:
            return
        self.data_dir.mkdir(parents=True, exist_ok=True)
        creationflags = 0
        if os.name == "nt":
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        self._process = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "fuckclassroom.plugins.process_worker",
            "--plugin-id",
            self.plugin_id,
            "--plugin-root",
            str(self.root),
            "--entry",
            self.entry,
            "--data-dir",
            str(self.data_dir),
            "--token",
            self._token,
            cwd=str(PROJECT_ROOT),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        assert self._process.stdout is not None
        assert self._process.stderr is not None
        self._stderr_task = asyncio.create_task(self._drain_stderr(self._process.stderr))
        try:
            raw = await asyncio.wait_for(self._process.stdout.readline(), timeout=12)
        except asyncio.TimeoutError as exc:
            await self.stop()
            raise ProcessPluginError(f"隔离插件 {self.plugin_id} 启动超时") from exc
        if not raw:
            await self._process.wait()
            if self._stderr_task is not None:
                await asyncio.gather(self._stderr_task, return_exceptions=True)
            raise ProcessPluginError(
                f"隔离插件 {self.plugin_id} 启动失败"
                + (f"：{self._error_detail()}" if self._error_detail() else "")
            )
        try:
            ready = json.loads(raw.decode("utf-8"))
            self._port = int(ready["port"])
            self._pid = int(ready["pid"])
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            await self.stop()
            raise ProcessPluginError(f"隔离插件 {self.plugin_id} 返回了无效启动握手") from exc

    async def _rpc(self, payload: dict[str, object], *, timeout: float = 60.0) -> dict[str, Any]:
        if not self.running:
            raise ProcessPluginError(
                f"隔离插件 {self.plugin_id} 进程未运行"
                + (f"：{self._error_detail()}" if self._error_detail() else "")
            )
        message = {"token": self._token, **payload}
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection("127.0.0.1", self._port),
                timeout=3,
            )
            writer.write(
                json.dumps(message, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                + b"\n"
            )
            await writer.drain()
            raw = await asyncio.wait_for(reader.readline(), timeout=timeout)
            writer.close()
            await writer.wait_closed()
        except (OSError, asyncio.TimeoutError, ConnectionError) as exc:
            raise ProcessPluginError(
                f"隔离插件 {self.plugin_id} IPC 失败"
                + (f"：{self._error_detail()}" if self._error_detail() else "")
            ) from exc
        if not raw:
            raise ProcessPluginError(f"隔离插件 {self.plugin_id} 已异常退出")
        try:
            response = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ProcessPluginError(f"隔离插件 {self.plugin_id} 返回了无效 IPC 响应") from exc
        if not response.get("ok"):
            raise ProcessPluginError(str(response.get("error") or "隔离插件 IPC 调用失败"))
        return response

    async def request(
        self,
        *,
        method: str,
        path: str,
        query_string: str,
        headers: dict[str, str],
        body: bytes,
    ) -> dict[str, Any]:
        response = await self._rpc(
            {
                "op": "request",
                "method": method,
                "path": path,
                "query_string": query_string,
                "headers": headers,
                "body_b64": base64.b64encode(body).decode("ascii"),
            }
        )
        payload = response.get("response")
        if not isinstance(payload, dict):
            raise ProcessPluginError(f"隔离插件 {self.plugin_id} 响应缺少 response")
        return payload

    async def stop(self) -> None:
        process = self._process
        if process is None:
            return
        if process.returncode is None and self._port:
            try:
                await self._rpc({"op": "shutdown"}, timeout=4)
            except ProcessPluginError:
                pass
        if process.returncode is None:
            try:
                await asyncio.wait_for(process.wait(), timeout=5)
            except asyncio.TimeoutError:
                process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=3)
                except asyncio.TimeoutError:
                    process.kill()
                    await process.wait()
        if self._stderr_task is not None:
            await asyncio.gather(self._stderr_task, return_exceptions=True)
        self._process = None
        self._stderr_task = None
        self._port = 0
        self._pid = 0


def build_process_plugin_spec(descriptor: Any) -> PluginSpec:
    if descriptor.root is None:
        raise ProcessPluginError(f"隔离插件 {descriptor.id} 缺少安装目录")
    service_key = f"process-plugin:{descriptor.id}"

    def setup_services(context: PluginContext) -> None:
        context.services.add(
            service_key,
            ProcessPluginHost(
                plugin_id=descriptor.id,
                root=descriptor.root,
                entry=descriptor.entry,
                data_dir=Path(context.config.data_dir) / "plugin-process-data" / descriptor.id,
            ),
        )

    async def startup(context: PluginContext) -> None:
        await context.services.get(service_key).start()

    async def shutdown(context: PluginContext) -> None:
        await context.services.get(service_key).stop()

    def build_routes(context: PluginContext) -> APIRouter:
        host: ProcessPluginHost = context.services.get(service_key)
        router = APIRouter()
        methods = ["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "HEAD"]

        async def proxy(request: Request, path: str = "") -> Response:
            request_headers = {
                key.lower(): value
                for key, value in request.headers.items()
                if key.lower() not in _HOP_BY_HOP_HEADERS
            }
            try:
                payload = await host.request(
                    method=request.method,
                    path="/" + path if path else "/",
                    query_string=request.url.query,
                    headers=request_headers,
                    body=await request.body(),
                )
                body = base64.b64decode(str(payload.get("body_b64") or ""), validate=False)
                headers = {
                    str(key).lower(): str(value)
                    for key, value in dict(payload.get("headers") or {}).items()
                    if str(key).lower() not in _HOP_BY_HOP_HEADERS
                }
                return Response(
                    content=body,
                    status_code=int(payload.get("status_code") or 200),
                    headers=headers,
                )
            except ProcessPluginError as exc:
                return JSONResponse(
                    {
                        "detail": "隔离插件进程不可用",
                        "plugin_id": descriptor.id,
                        "error": str(exc),
                    },
                    status_code=502,
                )

        router.add_api_route(
            descriptor.route_prefix,
            proxy,
            methods=methods,
            include_in_schema=False,
        )
        router.add_api_route(
            descriptor.route_prefix + "/{path:path}",
            proxy,
            methods=methods,
            include_in_schema=False,
        )
        return router

    return PluginSpec(
        id=descriptor.id,
        name=descriptor.name,
        order=descriptor.order,
        api_version=descriptor.api_version,
        requires=tuple(descriptor.requires),
        service_factory=setup_services,
        route_factory=build_routes,
        startup=startup,
        shutdown=shutdown,
        system_labels=((descriptor.id, descriptor.name),),
    )


__all__ = [
    "ProcessPluginError",
    "ProcessPluginHost",
    "build_process_plugin_spec",
]
