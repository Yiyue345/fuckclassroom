from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.plugins import PLUGIN_API_VERSION
from fuckclassroom.web import create_app


class ProcessPluginTests(unittest.TestCase):
    def test_process_plugin_runs_out_of_process_and_crash_is_contained(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            plugin_root = data_dir / "plugins" / "isolated_demo"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "isolated_demo",
                        "name": "Isolated Demo",
                        "api_version": PLUGIN_API_VERSION,
                        "execution": "process",
                        "route_prefix": "/plugins-runtime/isolated_demo",
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                """
import os

from fuckclassroom.plugins.process_api import json_response


def handle_request(request, context):
    if request.path == "/crash":
        os._exit(23)
    return json_response(
        {
            "pid": os.getpid(),
            "plugin_id": context.plugin_id,
            "path": request.path,
            "query": request.query_string,
        }
    )
""".lstrip(),
                encoding="utf-8",
            )
            (data_dir / "plugin_state.json").write_text(
                json.dumps({"selected": ["core_ui", "isolated_demo"]}),
                encoding="utf-8",
            )

            app = create_app(AppConfig(data_dir=data_dir))
            with TestClient(app) as client:
                response = client.get("/plugins-runtime/isolated_demo/hello?x=1")
                self.assertEqual(response.status_code, 200)
                payload = response.json()
                self.assertNotEqual(payload["pid"], os.getpid())
                self.assertEqual(payload["plugin_id"], "isolated_demo")
                self.assertEqual(payload["path"], "/hello")
                self.assertEqual(payload["query"], "x=1")

                crashed = client.get("/plugins-runtime/isolated_demo/crash")
                self.assertEqual(crashed.status_code, 502)
                self.assertEqual(crashed.json()["plugin_id"], "isolated_demo")

                self.assertEqual(client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
