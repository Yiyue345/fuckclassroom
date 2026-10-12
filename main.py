from __future__ import annotations

import logging
import os
import socket
import sys

sys.dont_write_bytecode = True

import uvicorn

from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.logging_setup import build_uvicorn_log_config, configure_startup_logging
from fuckclassroom.web import create_reloadable_app


logger = logging.getLogger(__name__)

_config = AppConfig()
_startup_log_path = None


def _ensure_startup_logging():
    global _startup_log_path
    if _startup_log_path is not None:
        return _startup_log_path
    try:
        _startup_log_path = configure_startup_logging(_config.logs_dir)
    except OSError as exc:
        print(f"无法创建启动日志，将仅输出到控制台：{exc}", flush=True)
    return _startup_log_path


if __name__ == "__main__":
    # Configure logging before plugin/application construction so startup
    # diagnostics are included in the same launch log.
    _ensure_startup_logging()



app = create_reloadable_app(_config)


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
    log_path = _ensure_startup_logging()
    host = os.getenv("FUCKCLASSROOM_HOST", "127.0.0.1")
    configured_port = os.getenv("FUCKCLASSROOM_PORT")
    preferred_port = int(configured_port) if configured_port else DEFAULT_PORT
    port = preferred_port if configured_port else _select_server_port(host, preferred_port)
    if port != preferred_port:
        logger.warning(
            "端口 %s 被 Windows 保留，自动改用 http://%s:%s",
            preferred_port,
            host,
            port,
        )

    logger.info("启动服务：http://%s:%s (pid=%s, log=%s)", host, port, os.getpid(), log_path)
    uvicorn_options = {}
    if log_path is not None:
        uvicorn_options["log_config"] = build_uvicorn_log_config(log_path)
    uvicorn.run(app, host=host, port=port, **uvicorn_options)


if __name__ == "__main__":
    run()
