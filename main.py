from __future__ import annotations

import os
import socket
import sys

sys.dont_write_bytecode = True

import uvicorn

from fuckclassroom.core.config import AppConfig
from fuckclassroom.course_selection.hy2_prebuilt import Hy2PrebuiltError, install_prebuilt_runner
from fuckclassroom.web import create_app


_config = AppConfig()
if _config.hy2_enabled:
    try:
        install_prebuilt_runner(_config.hy2_source_dir, _config.hy2_runner_path)
    except Hy2PrebuiltError as exc:
        # Keep startup resilient: the existing Hy2 manager will fall back to the
        # pinned local Cargo build when a packaged runner is unavailable/invalid.
        print(f"Hy2 预编译组件不可用，将回退本地构建：{exc}", flush=True)

app = create_app(_config)


DEFAULT_PORT = 8765
FALLBACK_PORTS = (8899, 8976, 9123)


def _probe_port(host: str, port: int) -> str:
    family = socket.AF_INET6 if ":" in host else socket.AF_INET
    try:
        with socket.socket(family, socket.SOCK_STREAM) as server:
            server.bind((host, port))
    except PermissionError:
        return "blocked"
    except OSError:
        return "occupied"
    return "available"


def _select_server_port(host: str, preferred_port: int) -> int:
    preferred_status = _probe_port(host, preferred_port)
    if preferred_status != "blocked":
        return preferred_port
    for candidate in FALLBACK_PORTS:
        if candidate != preferred_port and _probe_port(host, candidate) == "available":
            return candidate
    return preferred_port


def run() -> None:
    host = os.getenv("FUCKCLASSROOM_HOST", "127.0.0.1")
    configured_port = os.getenv("FUCKCLASSROOM_PORT")
    preferred_port = int(configured_port) if configured_port else DEFAULT_PORT
    port = preferred_port if configured_port else _select_server_port(host, preferred_port)
    if port != preferred_port:
        print(
            f"端口 {preferred_port} 被 Windows 保留，自动改用 http://{host}:{port}",
            flush=True,
        )
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    run()
