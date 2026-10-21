from __future__ import annotations

import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuckclassroom.web.reloadable import ReloadableApplication


class ReloadableApplicationTests(unittest.TestCase):
    def test_reload_swaps_generation_without_restarting_process(self) -> None:
        builds = {"count": 0}

        def factory(reload_callback, _reload_status):
            builds["count"] += 1
            generation = builds["count"]
            app = FastAPI()

            @app.get("/generation")
            def get_generation():
                return {"generation": generation}

            @app.post("/reload")
            async def reload_generation():
                scheduled = await reload_callback()
                return {"scheduled": scheduled}

            return app

        app = ReloadableApplication(factory)
        with TestClient(app) as client:
            self.assertEqual(client.get("/generation").json()["generation"], 1)
            self.assertTrue(client.post("/reload").json()["scheduled"])
            self.assertEqual(client.get("/generation").json()["generation"], 2)

    def test_reload_can_be_scheduled_from_background_thread(self) -> None:
        builds = {"count": 0}

        def factory(reload_callback, _reload_status):
            builds["count"] += 1
            generation = builds["count"]
            app = FastAPI()

            @app.get("/generation")
            def get_generation():
                return {"generation": generation}

            return app

        shell = ReloadableApplication(factory)
        with TestClient(shell) as client:
            self.assertEqual(client.get("/generation").json()["generation"], 1)
            result: dict[str, bool] = {}

            thread = threading.Thread(
                target=lambda: result.setdefault(
                    "scheduled",
                    shell.request_reload_threadsafe(),
                )
            )
            thread.start()
            thread.join(timeout=1)

            self.assertFalse(thread.is_alive())
            self.assertTrue(result["scheduled"])
            self.assertEqual(client.get("/generation").json()["generation"], 2)

    def test_update_worker_waits_for_runtime_generation_swap(self) -> None:
        builds = {"count": 0}

        def factory(_reload_callback, _reload_status):
            builds["count"] += 1
            generation = builds["count"]
            app = FastAPI()

            @app.get("/generation")
            def get_generation():
                return {"generation": generation}

            return app

        shell = ReloadableApplication(factory)
        with TestClient(shell) as client:
            self.assertEqual(client.get("/generation").json()["generation"], 1)
            outcome = {}

            def worker():
                outcome["success"] = shell.reload_updated_plugin_from_worker()

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertTrue(outcome["success"])
            self.assertEqual(client.get("/generation").json()["generation"], 2)

    def test_update_worker_reports_reload_failure_without_claiming_success(self) -> None:
        builds = {"count": 0}

        def factory(_reload_callback, _reload_status):
            builds["count"] += 1
            if builds["count"] > 1:
                raise RuntimeError("new runtime refused to start")
            return FastAPI()

        shell = ReloadableApplication(factory)
        with TestClient(shell):
            outcome = {}

            def worker():
                try:
                    shell.reload_updated_plugin_from_worker()
                except RuntimeError as exc:
                    outcome["error"] = str(exc)

            thread = threading.Thread(target=worker)
            thread.start()
            thread.join(timeout=5)
            self.assertFalse(thread.is_alive())
            self.assertIn("Runtime 热重载失败", outcome.get("error", ""))
            self.assertIn("new runtime refused", outcome["error"])

    def test_running_plugin_updates_without_changing_enabled_selection(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_dir = root / "plugins"
            target = plugin_dir / "demo"
            target.mkdir(parents=True)
            version_path = target / "version.txt"
            version_path.write_text("v1", encoding="utf-8")
            selection = root / "plugin_state.json"
            selection.write_text(
                json.dumps({"selected": ["core_ui", "demo", "dependent"]}),
                encoding="utf-8",
            )
            original_selection = selection.read_bytes()
            events: list[str] = []

            def factory(_reload_callback, _reload_status):
                version = version_path.read_text(encoding="utf-8")
                app = FastAPI()
                app.state.plugin_registry = SimpleNamespace(plugin_ids=("demo",))
                app.state.plugin_manager = SimpleNamespace(desired_enabled=lambda: {"demo"})
                @app.get("/version")
                def get_version():
                    return {"version": version}
                @app.on_event("shutdown")
                def shutdown():
                    events.append("stop-" + version)
                return app

            shell = ReloadableApplication(factory, plugin_dir=plugin_dir)
            with TestClient(shell) as client:
                self.assertEqual(client.get("/version").json()["version"], "v1")
                result: dict[str, object] = {}

                def installer():
                    self.assertIn("stop-v1", events)
                    version_path.write_text("v2", encoding="utf-8")
                    return "updated"

                def worker():
                    result["value"] = shell.update_plugin_from_worker("demo", installer)

                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
                self.assertEqual(result["value"], "updated")
                self.assertEqual(client.get("/version").json()["version"], "v2")
                self.assertEqual(selection.read_bytes(), original_selection)
                self.assertFalse(any(plugin_dir.glob(".demo.rollback-*")))

    def test_failed_plugin_startup_rolls_back_old_version_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin_dir = Path(directory) / "plugins"
            target = plugin_dir / "demo"
            target.mkdir(parents=True)
            version_path = target / "version.txt"
            version_path.write_text("v1", encoding="utf-8")
            events = []

            def factory(_reload_callback, _reload_status):
                version = version_path.read_text(encoding="utf-8")
                if version == "broken":
                    raise RuntimeError("new plugin startup failed")
                app = FastAPI()
                app.state.plugin_registry = SimpleNamespace(plugin_ids=("demo",))
                app.state.plugin_manager = SimpleNamespace(desired_enabled=lambda: {"demo"})
                @app.get("/version")
                def get_version():
                    return {"version": version}
                @app.on_event("shutdown")
                def shutdown():
                    events.append("stop-" + version)
                return app

            shell = ReloadableApplication(factory, plugin_dir=plugin_dir)
            with TestClient(shell) as client:
                outcome: dict[str, object] = {}

                def worker():
                    def installer():
                        self.assertIn("stop-v1", events)
                        version_path.write_text("broken", encoding="utf-8")
                        return "updated"
                    try:
                        shell.update_plugin_from_worker("demo", installer)
                    except RuntimeError as exc:
                        outcome["error"] = str(exc)

                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
                self.assertIn("旧版本已恢复", outcome.get("error", ""))
                self.assertEqual(version_path.read_text(encoding="utf-8"), "v1")
                self.assertEqual(client.get("/version").json()["version"], "v1")
                self.assertFalse(any(plugin_dir.glob(".demo.rollback-*")))

    def test_failed_plugin_install_restores_runtime_without_changing_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin_dir = Path(directory) / "plugins"
            plugin_dir.mkdir()
            target = plugin_dir / "demo"
            target.mkdir()
            original = target / "version.txt"
            original.write_text("v1", encoding="utf-8")

            def factory(_reload_callback, _reload_status):
                version = original.read_text(encoding="utf-8")
                app = FastAPI()
                app.state.plugin_registry = SimpleNamespace(plugin_ids=("demo",))
                app.state.plugin_manager = SimpleNamespace(desired_enabled=lambda: {"demo"})
                @app.get("/version")
                def version_route():
                    return {"version": version}
                return app

            shell = ReloadableApplication(factory, plugin_dir=plugin_dir)
            with TestClient(shell) as client:
                outcome = []

                def worker():
                    def installer():
                        original.write_text("partial", encoding="utf-8")
                        raise OSError("disk full")
                    try:
                        shell.update_plugin_from_worker("demo", installer)
                    except RuntimeError as exc:
                        outcome.append(str(exc))

                thread = threading.Thread(target=worker)
                thread.start()
                thread.join(timeout=8)
                self.assertFalse(thread.is_alive())
                self.assertIn("旧版本已恢复", outcome[0])
                self.assertEqual(client.get("/version").json()["version"], "v1")

    def test_failed_reload_keeps_previous_generation(self) -> None:
        builds = {"count": 0}
        shell = None

        def factory(reload_callback, _reload_status):
            builds["count"] += 1
            generation = builds["count"]
            if generation == 2:
                raise RuntimeError("boom")
            app = FastAPI()

            @app.get("/generation")
            def get_generation():
                return {"generation": generation}

            @app.post("/reload")
            async def reload_generation():
                return {"scheduled": await reload_callback()}

            return app

        shell = ReloadableApplication(factory)
        with TestClient(shell) as client:
            self.assertEqual(client.get("/generation").json()["generation"], 1)
            self.assertTrue(client.post("/reload").json()["scheduled"])
            self.assertEqual(client.get("/generation").json()["generation"], 1)
            self.assertIn("boom", str(shell.runtime_status()["last_error"]))


if __name__ == "__main__":
    unittest.main()
