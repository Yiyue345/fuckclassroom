from __future__ import annotations

import json
import threading
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

from fastapi.testclient import TestClient

from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.tasks import TaskManager
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
                    page = client.get("/plugins/registry?include_beta=1")
                    self.assertEqual(page.status_code, 200)
                    self.assertIn('aria-checked="false"', page.text)
                    # Historical GET parameters MUST NOT mutate or override the switch.
                    self.assertFalse(RegistryPreferences(root).include_beta())
                    self.assertFalse(RegistryPreferences(root).path.exists())
                    invalid = client.post(
                        "/api/plugins/registry/preferences",
                        json={"include_beta": "true"},
                    )
                    self.assertEqual(invalid.status_code, 422)
                    saved = client.post(
                        "/api/plugins/registry/preferences",
                        json={"include_beta": True},
                    )
                    self.assertEqual(saved.status_code, 200)
                    self.assertTrue(saved.json()["include_beta"])
                    kept = client.get("/plugins/registry")
                    self.assertIn('aria-checked="true"', kept.text)
                    fragment = client.get("/plugins/registry/results?include_beta=1")
                    self.assertIn("data-market-install-form", fragment.text)
                    self.assertEqual(catalog.call_args.kwargs["channels"], ("stable", "beta"))
                    # Visiting a historical URL does not overwrite saved state.
                    old = client.get("/plugins/registry?include_beta=0")
                    self.assertIn('aria-checked="true"', old.text)
                    self.assertTrue(RegistryPreferences(root).include_beta())

                restarted = create_app(AppConfig(data_dir=root))
                with TestClient(restarted) as client:
                    self.assertTrue(client.get("/api/plugins/registry").json()["include_beta"])
                    disabled = client.post(
                        "/api/plugins/registry/preferences",
                        json={"include_beta": False},
                    )
                    self.assertEqual(disabled.status_code, 200)
                self.assertFalse(RegistryPreferences(root).include_beta())

    def test_failed_preference_write_keeps_previous_value(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            pref = RegistryPreferences(Path(folder))
            pref.set_include_beta(True)
            app = create_app(AppConfig(data_dir=Path(folder)))
            with (
                patch.object(RegistryPreferences, "set_include_beta", side_effect=OSError("read-only")),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/api/plugins/registry/preferences",
                    json={"include_beta": False},
                )
            self.assertEqual(response.status_code, 500)
            self.assertTrue(pref.include_beta())

    def test_parallel_plugin_downloads_and_duplicate_rejection(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(AppConfig(data_dir=Path(folder)))
            both_started = threading.Event()
            release = threading.Event()
            lock = threading.Lock()
            active: set[str] = set()
            peak = 0

            def blocked_install(_self, plugin_id, *, current_enabled, channels, progress):
                nonlocal peak
                with lock:
                    active.add(plugin_id)
                    peak = max(peak, len(active))
                    if len(active) == 2:
                        both_started.set()
                progress(12, "正在下载")
                if not release.wait(timeout=5):
                    raise RuntimeError("test downloads did not overlap")
                with lock:
                    active.remove(plugin_id)
                return (
                    SimpleNamespace(id=plugin_id, name=plugin_id, version="1.0.0"),
                    "install",
                    False,
                )

            with (
                patch.object(
                    PluginRegistryClient, "install",
                    autospec=True, side_effect=blocked_install,
                ),
                TestClient(app) as http,
            ):
                try:
                    with ThreadPoolExecutor(max_workers=2) as pool:
                        first = pool.submit(
                            http.post, "/plugins/registry/first_plugin/install",
                            headers={"Accept": "application/json"},
                        ).result(timeout=3)
                        second = pool.submit(
                            http.post, "/plugins/registry/second_plugin/install",
                            headers={"Accept": "application/json"},
                        ).result(timeout=3)

                    self.assertEqual(first.status_code, 200)
                    self.assertEqual(second.status_code, 200)
                    self.assertNotEqual(first.json()["task_id"], second.json()["task_id"])
                    self.assertTrue(both_started.wait(timeout=2))
                    self.assertEqual(peak, 2)

                    duplicate = http.post(
                        "/plugins/registry/first_plugin/install",
                        headers={"Accept": "application/json"},
                    )
                    self.assertEqual(duplicate.status_code, 409)
                    self.assertIn("已有下载或安装任务", duplicate.json()["detail"])
                finally:
                    release.set()

                for response in (first, second):
                    for _ in range(100):
                        task = http.get(response.json()["status_url"]).json()
                        if task["status"] != "running":
                            break
                        time.sleep(0.01)
                    self.assertEqual(task["status"], "succeeded")

    def test_download_task_survives_router_generation_and_resumes_progress(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            shared = TaskManager()
            first_app = create_app(AppConfig(data_dir=root), shared_task_manager=shared)
            second_app = create_app(AppConfig(data_dir=root), shared_task_manager=shared)
            begun = threading.Event()
            finish = threading.Event()

            def install(_client, plugin_id, *, current_enabled, channels, progress):
                begun.set()
                progress(42, "正在下载大文件")
                if not finish.wait(timeout=5):
                    raise RuntimeError("test worker timeout")
                return SimpleNamespace(id=plugin_id, name=plugin_id, version="1.0"), "install", False

            with (
                patch.object(PluginRegistryClient, "install", autospec=True, side_effect=install) as call,
                TestClient(first_app) as client_a,
                TestClient(second_app) as client_b,
            ):
                try:
                    response = client_a.post(
                        "/plugins/registry/demo/install",
                        headers={"Accept": "application/json"},
                    )
                    self.assertEqual(response.status_code, 200)
                    task_id = response.json()["task_id"]
                    self.assertTrue(begun.wait(timeout=2))
                    resumed = client_b.get("/api/plugins/registry/install-tasks")
                    self.assertEqual(resumed.status_code, 200)
                    item = resumed.json()["tasks"]["demo"]
                    self.assertEqual(item["task_id"], task_id)
                    self.assertEqual(item["status"], "running")
                    self.assertEqual(item["status_url"], f"/api/tasks/{task_id}")

                    blocked = client_b.post(
                        "/plugins/registry/demo/install",
                        headers={"Accept": "application/json"},
                    )
                    self.assertEqual(blocked.status_code, 409)
                    self.assertEqual(blocked.json()["task_id"], task_id)
                    self.assertEqual(call.call_count, 1)
                finally:
                    finish.set()

                for _ in range(150):
                    task = client_b.get(f"/api/tasks/{task_id}").json()
                    if task["status"] != "running":
                        break
                    time.sleep(0.01)
                self.assertEqual(task["status"], "succeeded")
                self.assertEqual(
                    client_b.get("/api/plugins/registry/install-tasks").json()
                    ["tasks"]["demo"]["status"], "succeeded"
                )
                retry = client_b.post(
                    "/plugins/registry/demo/install",
                    headers={"Accept": "application/json"},
                )
                self.assertEqual(retry.status_code, 200)
                self.assertNotEqual(retry.json()["task_id"], task_id)

    def test_update_refreshes_runtime_and_reports_verified_outcome(self) -> None:
        for should_fail in (False, True):
            with self.subTest(should_fail=should_fail):
                with tempfile.TemporaryDirectory() as folder:
                    reloads = []

                    def reload_runtime() -> bool:
                        reloads.append(True)
                        if should_fail:
                            raise RuntimeError("Runtime startup failed")
                        return True

                    app = create_app(
                        AppConfig(data_dir=Path(folder)),
                        reload_wait_callback=reload_runtime,
                    )

                    def fake_update(_client, plugin_id, *, current_enabled, channels, progress):
                        progress(93, "插件文件已写入")
                        return (
                            SimpleNamespace(id=plugin_id, name="Demo", version="2.0.0"),
                            "update",
                            False,
                        )

                    with (
                        patch.object(
                            PluginRegistryClient, "install",
                            autospec=True, side_effect=fake_update,
                        ),
                        TestClient(app) as client,
                    ):
                        start = client.post(
                            "/plugins/registry/demo/install",
                            headers={"Accept": "application/json"},
                        )
                        self.assertEqual(start.status_code, 200)
                        task = None
                        for _ in range(200):
                            task = client.get(start.json()["status_url"]).json()
                            if task["status"] != "running":
                                break
                            time.sleep(0.01)

                    self.assertEqual(len(reloads), 1)
                    self.assertEqual(task["status"], "succeeded")
                    self.assertEqual(task["progress"], 100)
                    notice = parse_qs(urlsplit(task["result_url"]).query)["notice"][0]
                    if should_fail:
                        self.assertIn("请重启应用后生效", notice)
                        self.assertIn("Runtime 热重载失败", notice)
                    else:
                        self.assertIn("Runtime 已自动热重载", notice)

    def test_plugin_market_exposes_changelog_for_update(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            app = create_app(AppConfig(data_dir=Path(folder)))
            row = _market_row()
            row.update({
                "action": "update",
                "installed_version": "0.1.0",
                "target_version": "0.2.0",
                "changelog_url": "https://github.com/example/demo/releases/tag/v0.2.0",
            })
            with (
                patch.object(PluginRegistryClient, "catalog", return_value=[row]),
                TestClient(app) as client,
            ):
                response = client.get("/plugins/registry/results")
            self.assertEqual(response.status_code, 200)
            self.assertIn("本次更新说明", response.text)
            self.assertIn('target="_blank" rel="noopener noreferrer"', response.text)
            self.assertIn(row["changelog_url"], response.text)
            self.assertIn("查看 v0.2.0 更新日志", response.text)

    def test_inline_install_uses_task_json_and_saved_channel(self) -> None:
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            RegistryPreferences(root).set_include_beta(True)
            app = create_app(AppConfig(data_dir=root))
            def fake_install(_self, plugin_id, *, current_enabled, channels, progress):
                self.assertEqual(plugin_id, "demo")
                self.assertEqual(channels, ("stable", "beta"))
                progress(10, "下载中")
                progress.detail(45, "下载完成 45%")
                progress.clear_detail()
                progress(100, "安装完成")
                return SimpleNamespace(id="demo", name="Demo", version="0.1.0-beta.1"), "install", False

            with (
                patch.object(PluginRegistryClient, "resolve_install_plan", side_effect=AssertionError("POST should not preflight")),
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
