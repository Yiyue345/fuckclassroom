from __future__ import annotations

import unittest
from unittest.mock import patch

import main


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

    def test_run_reuses_the_existing_application_instance(self) -> None:
        environment = {
            "FUCKCLASSROOM_HOST": "127.0.0.1",
            "FUCKCLASSROOM_PORT": "9010",
        }
        with patch.dict(main.os.environ, environment), patch.object(main.uvicorn, "run") as uvicorn_run:
            main.run()

        uvicorn_run.assert_called_once_with(main.app, host="127.0.0.1", port=9010)


if __name__ == "__main__":
    unittest.main()
