from __future__ import annotations

import json
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fastapi.testclient import TestClient

from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.registry_client import PluginRegistryClient
from fuckclassroom.plugins.registry_preferences import RegistryPreferences
from fuckclassroom.web import create_app


def _market_row() -> dict[str, object]:
    return {
        "id": "demo",
        "name": "Demo",
        "publisher": "test",
        "official": False,
        "summary": "Test plugin",
        "compatibility_severity": "success",
        "compatibility_state": "compatible",
        "compatibility_label": "✓ 兼容",
        "compatibility_message": "",
        "signed": False,
        "missing_plugin_dependencies": [],
        "status": "active",
        "installed_version": "",
        "target_version": "0.1.0-beta.1",
        "channel": "beta",
        "released_at": "2026-10-02",
        "action": "install",
        "update_blocked": False,
    }


class RegistryPreferenceTests(unittest.TestCase):
    def test_registry_preference_is_saved_in_application_data(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            preference = RegistryPreferences(path)
            self.assertFalse(preference.include_beta())
            preference.set_include_beta(True)
            self.assertTrue(RegistryPreferences(path).include_beta())
            self.assertEqual(
                json.loads(preference.path.read_text(encoding="utf-8")),
                {"include_beta": True},
            )
            preference.set_include_beta(False)
            self.assertFalse(RegistryPreferences(path).include_beta())

    def test_corrupted_preference_falls_back_to_stable(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            pref = RegistryPreferences(Path(folder))
            pref.path.write_text("{bad json", encoding="utf-8")
            self.assertFalse(pref.include_beta())

    def test_toggle_survives_navigation_and_restart(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            with patch.object(PluginRegistryClient, "catalog", return_value=[_market_row()]) as catalog:
                app = create_app(AppConfig(data_dir=root))
                with TestClient(app) as client:
                    enabled = client.get("/plugins/registry?include_beta=1")
                    self.assertEqual(enabled.status_code, 200)
                    self.assertIn('role="switch"', enabled.text)
                    self.assertIn('aria-checked="true"', enabled.text)
                    self.assertIn('data-market-install-form', enabled.text)
                    self.assertIn("plugin_registry.js", enabled.text)
                    self.assertIn('data-market-download-state', enabled.text)

                    kept = client.get("/plugins/registry")
                    self.assertEqual(kept.status_code, 200)
                    self.assertIn('aria-checked="true"', kept.text)
                    self.assertEqual(catalog.call_args.kwargs["channels"], ("stable", "beta"))
                self.assertTrue(RegistryPreferences(root).include_beta())

                restarted_app = create_app(AppConfig(data_dir=root))
                with TestClient(restarted_app) as client:
                    data = client.get("/api/plugins/registry").json()
                    self.assertTrue(data["include_beta"])
                    disabled = client.get("/plugins/registry?include_beta=0")
                    self.assertEqual(disabled.status_code, 200)
                    self.assertIn('aria-checked="false"', disabled.text)
                self.assertFalse(RegistryPreferences(root).include_beta())

    def test_inline_install_uses_task_json_and_saved_channel(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            RegistryPreferences(root).set_include_beta(True)
            app = create_app(AppConfig(data_dir=root))
            plan = SimpleNamespace(
                action="install", name="Demo", update_blocked=False,
                compatibility_message="",
            )

            def fake_install(_self, plugin_id, *, current_enabled, channels, progress):
                self.assertEqual(plugin_id, "demo")
                self.assertEqual(channels, ("stable", "beta"))
                progress(10, "下载中")
                progress.detail(45, "下载完成 45%")
                progress.clear_detail()
                progress(100, "安装完成")
                return SimpleNamespace(id="demo", name="Demo", version="0.1.0-beta.1"), "install", False

            with (
                patch.object(PluginRegistryClient, "resolve_install_plan", return_value=plan) as resolve,
                patch.object(PluginRegistryClient, "install", autospec=True, side_effect=fake_install),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/plugins/registry/demo/install",
                    headers={"Accept": "application/json"},
                )
                self.assertEqual(response.status_code, 200)
                data = response.json()
                self.assertIn("status_url", data)
                self.assertIn("task_id", data)
                self.assertEqual(resolve.call_args.kwargs["channels"], ("stable", "beta"))

                task = None
                for _ in range(100):
                    task = client.get(data["status_url"]).json()
                    if task["status"] != "running":
                        break
                    time.sleep(0.01)

                self.assertEqual(task["status"], "succeeded")
                self.assertEqual(task["progress"], 100)
                self.assertIn("include_beta=1", task["result_url"])


if __name__ == "__main__":
    unittest.main()
