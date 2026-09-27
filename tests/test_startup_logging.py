from __future__ import annotations

from contextlib import redirect_stderr
from datetime import datetime, timedelta
import io
import logging
from pathlib import Path
import tempfile
import unittest

from fuckclassroom.core.logging_setup import (
    build_uvicorn_log_config,
    configure_startup_logging,
    create_startup_log,
)


class StartupLoggingTests(unittest.TestCase):
    def test_only_five_most_recent_launch_logs_are_retained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            log_dir = Path(directory)
            start = datetime(2026, 9, 27, 17, 0, 0)
            created = [
                create_startup_log(log_dir, now=start + timedelta(seconds=index))
                for index in range(6)
            ]

            remaining = sorted(log_dir.glob("startup-*.log"))
            self.assertEqual(len(remaining), 5)
            self.assertFalse(created[0].exists())
            self.assertEqual(set(remaining), set(created[1:]))

    def test_configured_root_logger_writes_file_and_console(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root_logger = logging.getLogger()
            original_level = root_logger.level
            original_handlers = list(root_logger.handlers)
            stderr = io.StringIO()
            with redirect_stderr(stderr):
                log_path = configure_startup_logging(Path(directory))
                try:
                    logging.getLogger("fuckclassroom.test").warning("日志探针")
                    for handler in root_logger.handlers:
                        handler.flush()
                    content = log_path.read_text(encoding="utf-8")
                    self.assertIn("fuckclassroom.test", content)
                    self.assertIn("日志探针", content)
                    self.assertIn("fuckclassroom.test", stderr.getvalue())
                    self.assertIn("日志探针", stderr.getvalue())
                finally:
                    for handler in list(root_logger.handlers):
                        if handler not in original_handlers:
                            root_logger.removeHandler(handler)
                            handler.close()
                    root_logger.setLevel(original_level)

    def test_uvicorn_logs_use_the_same_launch_log_without_console_propagation(self) -> None:
        log_path = Path("data/logs/startup-test.log")
        config = build_uvicorn_log_config(log_path)

        self.assertEqual(config["handlers"]["startup_file"]["filename"], str(log_path))
        self.assertIn("startup_file", config["loggers"]["uvicorn"]["handlers"])
        self.assertIn("startup_file", config["loggers"]["uvicorn.access"]["handlers"])
        self.assertFalse(config["loggers"]["uvicorn"]["propagate"])
        self.assertFalse(config["loggers"]["uvicorn.access"]["propagate"])


if __name__ == "__main__":
    unittest.main()
