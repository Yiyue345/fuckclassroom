from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path

from fastapi.testclient import TestClient

from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.plugins import PLUGIN_API_VERSION
from fuckclassroom.plugins.process_api import PLUGIN_RPC_API_VERSION
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
                        "rpc_api_version": PLUGIN_RPC_API_VERSION,
                        "rpc_permissions": [
                            "core.settings.get",
                            "core.plugins.enabled",
                            "core.storage.get",
                            "core.storage.set",
                            "core.tasks.create",
                            "core.tasks.update",
                            "core.tasks.log",
                            "core.tasks.complete"
                        ],
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                """
import os

from fuckclassroom.plugins.process_api import ProcessPluginRpcError, json_response


def handle_request(request, context):
    if request.path == "/crash":
        os._exit(23)
    if request.path == "/rpc":
        workers = context.rpc.call("core.settings.get", {"key": "ocr_workers"})
        context.rpc.call("core.storage.set", {"key": "demo", "value": {"count": 2}})
        stored = context.rpc.call("core.storage.get", {"key": "demo"})
        enabled = context.rpc.call("core.plugins.enabled")
        task = context.rpc.call("core.tasks.create", {"title": "Isolated RPC Task"})
        task_id = task["task_id"]
        context.rpc.call(
            "core.tasks.update",
            {"task_id": task_id, "progress": 50, "message": "RPC halfway"},
        )
        context.rpc.call(
            "core.tasks.log",
            {"task_id": task_id, "level": "info", "message": "child log"},
        )
        context.rpc.call(
            "core.tasks.complete",
            {"task_id": task_id, "result_url": "/plugins"},
        )
        return json_response(
            {
                "workers": workers,
                "stored": stored,
                "enabled": enabled,
                "task_id": task_id,
            }
        )
    if request.path == "/denied":
        try:
            context.rpc.call("classroom.courses.list", {})
        except ProcessPluginRpcError as exc:
            return json_response({"code": exc.code, "message": str(exc)})
        return json_response({"code": "unexpected"})
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

                rpc = client.get("/plugins-runtime/isolated_demo/rpc")
                self.assertEqual(rpc.status_code, 200)
                rpc_payload = rpc.json()
                self.assertEqual(rpc_payload["workers"], 4)
                self.assertEqual(rpc_payload["stored"], {"count": 2})
                self.assertIn("isolated_demo", rpc_payload["enabled"])
                task = client.get(f"/api/tasks/{rpc_payload['task_id']}")
                self.assertEqual(task.status_code, 200)
                self.assertEqual(task.json()["status"], "succeeded")
                self.assertEqual(task.json()["progress"], 100)
                self.assertEqual(task.json()["result_url"], "/plugins")

                denied = client.get("/plugins-runtime/isolated_demo/denied")
                self.assertEqual(denied.status_code, 200)
                self.assertEqual(denied.json()["code"], "permission_denied")

                crashed = client.get("/plugins-runtime/isolated_demo/crash")
                self.assertEqual(crashed.status_code, 502)
                self.assertEqual(crashed.json()["plugin_id"], "isolated_demo")

                self.assertEqual(client.get("/health").status_code, 200)


if __name__ == "__main__":
    unittest.main()
