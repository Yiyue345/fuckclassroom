from __future__ import annotations

from copy import deepcopy
from datetime import datetime
import logging
import os
from pathlib import Path

from uvicorn.config import LOGGING_CONFIG


DEFAULT_STARTUP_LOG_RETENTION = 5
STARTUP_LOG_PREFIX = "startup-"
STARTUP_LOG_SUFFIX = ".log"
_STARTUP_HANDLER_MARKER = "_fuckclassroom_startup_file"


def create_startup_log(
    log_dir: Path,
    *,
    keep: int = DEFAULT_STARTUP_LOG_RETENTION,
    now: datetime | None = None,
) -> Path:
    """Create one log file for this process launch and prune older launch logs."""
    if keep < 1:
        raise ValueError("keep must be at least 1")

    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S-%f")
    base_name = f"{STARTUP_LOG_PREFIX}{timestamp}-{os.getpid()}"

    counter = 0
    while True:
        suffix = "" if counter == 0 else f"-{counter}"
        path = log_dir / f"{base_name}{suffix}{STARTUP_LOG_SUFFIX}"
        try:
            path.touch(exist_ok=False)
            break
        except FileExistsError:
            counter += 1

    _prune_startup_logs(log_dir, keep=keep, preserve=path)
    return path


def configure_startup_logging(
    log_dir: Path,
    *,
    keep: int = DEFAULT_STARTUP_LOG_RETENTION,
    level: int = logging.INFO,
) -> Path:
    """Attach a UTF-8 file handler for the current launch to the root logger."""
    log_path = create_startup_log(log_dir, keep=keep)
    root_logger = logging.getLogger()

    for handler in list(root_logger.handlers):
        if getattr(handler, _STARTUP_HANDLER_MARKER, False):
            root_logger.removeHandler(handler)
            handler.close()

    handler = logging.FileHandler(log_path, encoding="utf-8")
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s | %(levelname)s | %(name)s | %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )
    setattr(handler, _STARTUP_HANDLER_MARKER, True)
    root_logger.addHandler(handler)

    if root_logger.level == logging.NOTSET or root_logger.level > level:
        root_logger.setLevel(level)

    logging.getLogger("fuckclassroom.startup").info("启动日志已创建：%s", log_path)
    return log_path


def build_uvicorn_log_config() -> dict:
    """Keep Uvicorn's console formatting while also propagating into our file handler."""
    config = deepcopy(LOGGING_CONFIG)
    for logger_name in ("uvicorn", "uvicorn.access"):
        logger_config = config.get("loggers", {}).get(logger_name)
        if logger_config is not None:
            logger_config["propagate"] = True
    return config


def _prune_startup_logs(log_dir: Path, *, keep: int, preserve: Path) -> None:
    candidates = sorted(
        (
            path
            for path in log_dir.glob(f"{STARTUP_LOG_PREFIX}*{STARTUP_LOG_SUFFIX}")
            if path.is_file()
        ),
        key=lambda path: path.name,
        reverse=True,
    )

    if preserve in candidates:
        candidates.remove(preserve)
        candidates.insert(0, preserve)

    for stale_path in candidates[keep:]:
        try:
            stale_path.unlink()
        except OSError:
            # A stale log being temporarily locked must not prevent startup.
            continue
