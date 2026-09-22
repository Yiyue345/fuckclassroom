from __future__ import annotations

import argparse
import asyncio
import base64
import importlib.util
import inspect
import json
import os
import socketserver
import sys
import threading
import traceback
from pathlib import Path
from types import ModuleType
from typing import Any

from .process_api import (
    ProcessPluginContext,
    ProcessPluginRequest,
    ProcessPluginResponse,
    json_response,
)


_MAX_MESSAGE_BYTES = 64 * 1024 * 1024


def _load_module(plugin_id: str, root: Path, entry: str) -> ModuleType:
    target = root / entry
    safe_id = plugin_id.replace("-", "_")
    module_name = f"fuckclassroom_isolated_{safe_id}_{os.getpid()}"
    spec = importlib.util.spec_from_file_location(
        module_name,
        target,
        submodule_search_locations=[str(root)],
    )
    if spec is None or spec.loader is None:
        raise RuntimeError(f"无法加载隔离插件入口：{target}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _call(callable_object, *args):
    result = callable_object(*args)
    if inspect.isawaitable(result):
        return asyncio.run(result)
    return result


def _normalize_response(value: object) -> ProcessPluginResponse:
    if isinstance(value, ProcessPluginResponse):
        return value
    if value is None:
        return ProcessPluginResponse(status_code=204)
    if isinstance(value, bytes):
        return ProcessPluginResponse(
            body=value,
            headers={"content-type": "application/octet-stream"},
        )
    if isinstance(value, str):
        return ProcessPluginResponse(
            body=value.encode("utf-8"),
            headers={"content-type": "text/plain; charset=utf-8"},
        )
    if isinstance(value, (dict, list, tuple, int, float, bool)):
        return json_response(value)
    raise TypeError(
        "handle_request() 必须返回 ProcessPluginResponse、JSON 可序列化值、str、bytes 或 None"
    )


class _PluginServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True

    def __init__(
        self,
        address,
        handler,
        *,
        token: str,
        module: ModuleType,
        context: ProcessPluginContext,
    ):
        super().__init__(address, handler)
        self.token = token
        self.module = module
        self.context = context


class _Handler(socketserver.StreamRequestHandler):
    server: _PluginServer

    def _write(self, payload: dict[str, object]) -> None:
        self.wfile.write(
            json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n"
        )
        self.wfile.flush()

    def handle(self) -> None:
        raw = self.rfile.readline(_MAX_MESSAGE_BYTES + 1)
        if not raw or len(raw) > _MAX_MESSAGE_BYTES:
            self._write({"ok": False, "error": "IPC 请求过大或为空"})
            return
        try:
            message = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            self._write({"ok": False, "error": "IPC 请求不是有效 JSON"})
            return
        if message.get("token") != self.server.token:
            self._write({"ok": False, "error": "IPC token 无效"})
            return

        operation = str(message.get("op") or "request")
        if operation == "health":
            self._write({"ok": True, "pid": os.getpid()})
            return
        if operation == "shutdown":
            self._write({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return
        if operation != "request":
            self._write({"ok": False, "error": f"不支持的 IPC 操作：{operation}"})
            return

        handler = getattr(self.server.module, "handle_request", None)
        if not callable(handler):
            self._write({"ok": False, "error": "隔离插件未提供 handle_request(request, context)"})
            return

        try:
            body = base64.b64decode(str(message.get("body_b64") or ""), validate=False)
            request = ProcessPluginRequest(
                method=str(message.get("method") or "GET").upper(),
                path=str(message.get("path") or "/"),
                query_string=str(message.get("query_string") or ""),
                headers={
                    str(key).lower(): str(value)
                    for key, value in dict(message.get("headers") or {}).items()
                },
                body=body,
            )
            response = _normalize_response(
                _call(handler, request, self.server.context)
            )
            self._write(
                {
                    "ok": True,
                    "response": {
                        "status_code": int(response.status_code),
                        "headers": {
                            str(key).lower(): str(value)
                            for key, value in dict(response.headers or {}).items()
                        },
                        "body_b64": base64.b64encode(response.body).decode("ascii"),
                    },
                }
            )
        except BaseException as exc:  # noqa: BLE001 - isolate plugin failures from host process.
            traceback.print_exc(file=sys.stderr)
            error = f"{type(exc).__name__}: {exc}"
            response = json_response({"detail": "隔离插件执行失败", "error": error}, status_code=500)
            self._write(
                {
                    "ok": True,
                    "response": {
                        "status_code": 500,
                        "headers": dict(response.headers or {}),
                        "body_b64": base64.b64encode(response.body).decode("ascii"),
                    },
                }
            )


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--plugin-id", required=True)
    parser.add_argument("--plugin-root", required=True)
    parser.add_argument("--entry", required=True)
    parser.add_argument("--data-dir", required=True)
    parser.add_argument("--token", required=True)
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    protocol_out = sys.stdout
    sys.stdout = sys.stderr

    root = Path(args.plugin_root).resolve()
    data_dir = Path(args.data_dir).resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    context = ProcessPluginContext(
        plugin_id=args.plugin_id,
        plugin_root=root,
        data_dir=data_dir,
    )

    try:
        module = _load_module(args.plugin_id, root, args.entry)
        startup = getattr(module, "startup", None)
        if callable(startup):
            _call(startup, context)
        if not callable(getattr(module, "handle_request", None)):
            raise RuntimeError("隔离插件必须提供 handle_request(request, context)")
    except BaseException:  # noqa: BLE001
        traceback.print_exc(file=sys.stderr)
        return 2

    server = _PluginServer(
        ("127.0.0.1", 0),
        _Handler,
        token=args.token,
        module=module,
        context=context,
    )
    port = int(server.server_address[1])
    protocol_out.write(
        json.dumps(
            {"type": "ready", "port": port, "pid": os.getpid()},
            separators=(",", ":"),
        )
        + "\n"
    )
    protocol_out.flush()

    try:
        server.serve_forever(poll_interval=0.2)
    finally:
        server.server_close()
        shutdown = getattr(module, "shutdown", None)
        if callable(shutdown):
            try:
                _call(shutdown, context)
            except BaseException:  # noqa: BLE001
                traceback.print_exc(file=sys.stderr)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
