from __future__ import annotations

import json
import io
import time
import zipfile
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fuckclassroom.web.reloadable import ReloadableApplication, create_reloadable_app
from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.manager import PluginManager


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

    def test_inactive_plugin_update_does_not_interrupt_other_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "plugins"
            target = root / "demo"
            target.mkdir(parents=True)
            version_file = target / "version.txt"
            version_file.write_text("v1", encoding="utf-8")
            starts: list[str] = []
            stops: list[str] = []

            def factory(_reload_callback, _reload_status):
                starts.append("start")
                app = FastAPI()
                app.state.plugin_registry = SimpleNamespace(plugin_ids=("core_ui", "other"))
                app.state.plugin_manager = SimpleNamespace(desired_enabled=lambda: {"other"})
                @app.get("/other/status")
                def other_status():
                    return {"available": True}
                @app.on_event("shutdown")
                def on_shutdown():
                    stops.append("stop")
                return app

            shell = ReloadableApplication(factory, plugin_dir=root)
            started = threading.Event()
            release = threading.Event()
            outcome = {}

            def installer():
                started.set()
                if not release.wait(timeout=5):
                    raise RuntimeError("timed out in inactive installer")
                version_file.write_text("v2", encoding="utf-8")
                return "v2"

            with TestClient(shell) as client:
                thread = threading.Thread(
                    target=lambda: outcome.setdefault(
                        "value", shell.update_plugin_from_worker("demo", installer)
                    ),
                )
                thread.start()
                try:
                    self.assertTrue(started.wait(timeout=3))
                    # This request reaches an unrelated active plugin while the
                    # inactive package is still being replaced.
                    self.assertEqual(client.get("/other/status").status_code, 200)
                    self.assertEqual(starts, ["start"])
                    self.assertEqual(stops, [])
                finally:
                    release.set()
                    thread.join(timeout=5)
                self.assertFalse(thread.is_alive())
                self.assertEqual(outcome["value"], "v2")
                self.assertEqual(starts, ["start"])
                self.assertEqual(stops, [])
            self.assertEqual(stops, ["stop"])

    def test_shutdown_waits_until_plugin_update_finishes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            plugin_dir = Path(directory) / "plugins"
            target = plugin_dir / "demo"
            target.mkdir(parents=True)
            version_file = target / "version.txt"
            version_file.write_text("v1", encoding="utf-8")
            running = threading.Event()
            closing = threading.Event()
            installer_entered = threading.Event()
            installer_release = threading.Event()
            shutdown_complete = threading.Event()
            events: list[str] = []
            outcome = {}

            def factory(_reload_callback, _reload_status):
                version = version_file.read_text(encoding="utf-8")
                app = FastAPI()
                app.state.plugin_registry = SimpleNamespace(plugin_ids=("demo",))
                app.state.plugin_manager = SimpleNamespace(desired_enabled=lambda: {"demo"})
                @app.on_event("shutdown")
                def on_shutdown():
                    events.append("stop-" + version)
                return app

            shell = ReloadableApplication(factory, plugin_dir=plugin_dir)

            def serve():
                with TestClient(shell):
                    running.set()
                    closing.wait(timeout=5)
                shutdown_complete.set()

            server = threading.Thread(target=serve)
            server.start()
            self.assertTrue(running.wait(timeout=3))

            def install():
                installer_entered.set()
                if not installer_release.wait(timeout=5):
                    raise RuntimeError("installer held too long")
                events.append("install-finished")
                version_file.write_text("v2", encoding="utf-8")
                return "updated"

            def update_worker():
                outcome["value"] = shell.update_plugin_from_worker("demo", install)

            worker = threading.Thread(target=update_worker)
            worker.start()
            try:
                self.assertTrue(installer_entered.wait(timeout=3))
                closing.set()
                # Shutdown must await the update, not tear down the generation
                # while the downloader is still touching plugin files.
                self.assertFalse(shutdown_complete.wait(timeout=0.1))
            finally:
                installer_release.set()
                closing.set()
                worker.join(timeout=5)
                server.join(timeout=5)
            self.assertFalse(worker.is_alive())
            self.assertFalse(server.is_alive())
            self.assertTrue(shutdown_complete.is_set())
            self.assertEqual(outcome["value"], "updated")
            self.assertEqual(events, ["stop-v1", "install-finished", "stop-v2"])
            self.assertFalse(any(plugin_dir.glob(".demo.rollback-*")))

    def test_multifile_plugin_update_and_rollback_loads_matching_source(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = AppConfig(data_dir=root)
            manager = PluginManager(config)
            manager.plugin_dir.mkdir(parents=True, exist_ok=True)
            manifest = {
                "id": "demo", "name": "Demo", "version": "1.0.0",
                "api_version": 1, "app_requires": ">=0",
                "execution": "in_process", "entry": "plugin.py",
                "requires": [], "python_requires": [],
            }
            entry = (
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "from .routes import build_routes\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='demo', name='Demo', route_factory=build_routes)\n"
            )
            routes = (
                "from fastapi import APIRouter\n"
                "from .services import current\n"
                "def build_routes(context):\n"
                "    router = APIRouter()\n"
                "    @router.get('/demo/version')\n"
                "    def version(): return {'value': current()}\n"
                "    return router\n"
            )
            services = "from .nested import marker\ndef current(): return marker()\n"

            def archive(label, *, broken=False):
                path = root / (label + ".zip")
                version = label.removeprefix("v")
                record = {**manifest, "version": version + ".0.0"}
                plugin_entry = entry if not broken else (
                    "def build_plugin(): raise RuntimeError('broken module startup')\n"
                )
                with zipfile.ZipFile(path, "w") as pkg:
                    pkg.writestr("plugin.json", json.dumps(record))
                    pkg.writestr("plugin.py", plugin_entry)
                    pkg.writestr("routes.py", routes)
                    pkg.writestr("services.py", services)
                    pkg.writestr(
                        "nested.py", f"def marker(): return '{label}'\n",
                    )
                return path

            initial = archive("v1")
            manager.install_from_path(str(initial))
            manager.enable("demo")
            shell = create_reloadable_app(config)

            with TestClient(shell) as client:
                self.assertEqual(client.get("/demo/version").json()["value"], "v1")
                for label, broken in (("v2", False), ("v9", True), ("v3", False)):
                    source = archive(label, broken=broken)
                    outcome: dict[str, object] = {}

                    def install_plugin():
                        return PluginManager(config).install_from_path(
                            str(source), replace=True, allow_stopped_runtime_replace=True,
                        )

                    def run_update():
                        try:
                            outcome["value"] = shell.update_plugin_from_worker(
                                "demo", install_plugin,
                            )
                        except RuntimeError as exc:
                            outcome["error"] = str(exc)

                    thread = threading.Thread(target=run_update)
                    thread.start()
                    thread.join(timeout=8)
                    self.assertFalse(thread.is_alive())
                    if broken:
                        self.assertIn("旧版本已恢复", str(outcome.get("error")))
                        self.assertEqual(
                            client.get("/demo/version").json()["value"], "v2",
                        )
                    else:
                        self.assertEqual(outcome["value"].version, label[1:] + ".0.0")
                        self.assertEqual(
                            client.get("/demo/version").json()["value"], label,
                        )

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
