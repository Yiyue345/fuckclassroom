from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import main

from fuckclassroom.web import ReloadableApplication


class ServerStartupTests(unittest.TestCase):
    def test_reserved_default_uses_available_fallback(self) -> None:
        with patch.object(main, "_probe_port", side_effect=["blocked", "available"]):
            port = main._select_server_port("127.0.0.1", main.DEFAULT_PORT)

        self.assertEqual(port, main.FALLBACK_PORTS[0])

    def test_occupied_default_is_not_silently_replaced(self) -> None:
        with patch.object(main, "_probe_port", return_value="occupied") as probe:
            port = main._select_server_port("127.0.0.1", main.DEFAULT_PORT)

        self.assertEqual(port, main.DEFAULT_PORT)
        probe.assert_called_once_with("127.0.0.1", main.DEFAULT_PORT)

    def test_main_uses_reloadable_application_shell(self) -> None:
        self.assertIsInstance(main.app, ReloadableApplication)

    def test_run_reuses_the_existing_application_instance(self) -> None:
        environment = {
            "FUCKCLASSROOM_HOST": "127.0.0.1",
            "FUCKCLASSROOM_PORT": "9010",
        }
        log_path = Path("data/logs/startup-test.log")
        log_config = {"version": 1}
        with (
            patch.dict(main.os.environ, environment),
            patch.object(main, "_startup_log_path", None),
            patch.object(main, "configure_startup_logging", return_value=log_path) as configure_logging,
            patch.object(
                main,
                "build_uvicorn_log_config",
                return_value=log_config,
            ) as build_log_config,
            patch.object(main.uvicorn, "run") as uvicorn_run,
        ):
            main.run()

        configure_logging.assert_called_once_with(main._config.logs_dir)
        build_log_config.assert_called_once_with(log_path)
        uvicorn_run.assert_called_once_with(
            main.app,
            host="127.0.0.1",
            port=9010,
            log_config=log_config,
        )


if __name__ == "__main__":
    unittest.main()
