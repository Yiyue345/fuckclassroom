from __future__ import annotations

import ast
import os
import tempfile
import unittest
from pathlib import Path

from fastapi import APIRouter, FastAPI
from fastapi.testclient import TestClient

from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.plugins import PluginRegistry, PluginRuntime, PluginSpec
from fuckclassroom.plugins import build_plugin_registry
from fuckclassroom.web import create_app


class PluginCompositionTests(unittest.TestCase):
    def test_web_app_is_only_the_plugin_composition_root(self) -> None:
        source = Path("fuckclassroom/web/app.py").read_text(encoding="utf-8")

        self.assertNotIn("APIRouter", source)
        self.assertNotIn("@app.get(", source)
        self.assertNotIn("@app.post(", source)
        self.assertNotIn("plugins.classroom.routes", source)
        self.assertNotIn("plugins.course_selection.routes", source)

        for plugin_id in (
            "core_ui",
            "classroom",
            "processing",
            "transcription",
            "ocr",
            "ai_summary",
            "attendance",
            "course_selection",
        ):
            root = Path("fuckclassroom/plugins") / plugin_id
            self.assertTrue((root / "plugin.py").exists())

        for plugin_id in ("core_ui", "classroom", "processing", "ai_summary", "attendance", "course_selection"):
            self.assertTrue((Path("fuckclassroom/plugins") / plugin_id / "routes.py").exists())

        for plugin_id in ("core_ui", "classroom", "processing", "transcription", "ocr", "ai_summary", "attendance", "course_selection"):
            self.assertTrue((Path("fuckclassroom/plugins") / plugin_id / "services.py").exists())

        for plugin_id in ("classroom", "attendance", "course_selection"):
            self.assertTrue((Path("fuckclassroom/plugins") / plugin_id / "lifecycle.py").exists())

        implementations = (
            "fuckclassroom/plugins/processing/extraction.py",
            "fuckclassroom/plugins/transcription/engine.py",
            "fuckclassroom/plugins/ocr/engine.py",
            "fuckclassroom/plugins/ocr/compat.py",
            "fuckclassroom/plugins/ai_summary/client.py",
            "fuckclassroom/plugins/attendance/scanner.py",
            "fuckclassroom/plugins/attendance/ketangpai.py",
        )
        for implementation in implementations:
            self.assertTrue(Path(implementation).exists())

        classroom_init = Path("fuckclassroom/classroom/__init__.py").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("install_rapidocr_compat", classroom_init)
        self.assertNotIn("install_ocr_failure_guard", classroom_init)

    def test_plugin_manifests_lazy_load_services_and_lifecycle(self) -> None:
        for plugin_id in (
            "core_ui",
            "classroom",
            "processing",
            "transcription",
            "ocr",
            "ai_summary",
            "attendance",
            "course_selection",
        ):
            source = (Path("fuckclassroom/plugins") / plugin_id / "plugin.py").read_text(
                encoding="utf-8"
            )
            tree = ast.parse(source)
            top_level_modules = {
                node.module
                for node in tree.body
                if isinstance(node, ast.ImportFrom)
            }
            self.assertNotIn("services", top_level_modules, plugin_id)
            self.assertNotIn("lifecycle", top_level_modules, plugin_id)

    def test_processing_does_not_eagerly_import_optional_engines(self) -> None:
        processing_source = Path(
            "fuckclassroom/plugins/processing/extraction.py"
        ).read_text(encoding="utf-8")
        processing_tree = ast.parse(processing_source)
        processing_imports = {
            node.module
            for node in processing_tree.body
            if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("ai_summary.client", processing_imports)
        self.assertNotIn("transcription.engine", processing_imports)

        ocr_source = Path("fuckclassroom/plugins/ocr/services.py").read_text(
            encoding="utf-8"
        )
        ocr_tree = ast.parse(ocr_source)
        ocr_imports = {
            node.module
            for node in ocr_tree.body
            if isinstance(node, ast.ImportFrom)
        }
        self.assertNotIn("ai_summary.client", ocr_imports)

    def test_registry_can_enable_feature_plugins_independently(self) -> None:
        classroom = build_plugin_registry({"classroom"})
        selection = build_plugin_registry({"course_selection"})

        self.assertEqual(classroom.plugin_ids, ("core_ui", "classroom"))
        self.assertEqual(selection.plugin_ids, ("core_ui", "course_selection"))

        transcription = build_plugin_registry({"transcription"})
        self.assertEqual(
            transcription.plugin_ids,
            ("core_ui", "classroom", "processing", "transcription"),
        )

    def test_classroom_only_app_has_no_selection_routes_or_services(self) -> None:
        app = create_app(enabled_plugins={"classroom"})
        paths = {getattr(route, "path", "") for route in app.routes}
        services = app.state.plugin_runtime.services

        self.assertIn("/classroom", paths)
        self.assertIn("/courses", paths)
        self.assertNotIn("/selection/info", paths)
        self.assertTrue(services.has("classroom_client"))
        self.assertTrue(services.has("auth_service"))
        self.assertFalse(services.has("course_selection"))
        self.assertFalse(services.has("auto_selection"))
        self.assertFalse(services.has("extraction_service"))
        self.assertFalse(services.has("transcription_service"))
        self.assertFalse(services.has("ocr_service"))
        self.assertFalse(services.has("ai_summary_service"))
        self.assertFalse(services.has("ketangpai_client"))

    def test_transcription_plugin_loads_only_its_processing_capability(self) -> None:
        app = create_app(enabled_plugins={"transcription"})
        paths = {getattr(route, "path", "") for route in app.routes}
        services = app.state.plugin_runtime.services

        self.assertIn("/classroom", paths)
        self.assertIn("/courses/{course_id}/lessons/{lesson_id}/extract", paths)
        self.assertNotIn("/courses/{course_id}/lessons/{lesson_id}/summarize", paths)
        self.assertTrue(services.has("extraction_service"))
        self.assertTrue(services.has("transcription_service"))
        self.assertFalse(services.has("ocr_service"))
        self.assertFalse(services.has("ai_summary_service"))
        self.assertFalse(services.has("ketangpai_client"))

    def test_heavy_builtin_services_run_in_isolated_worker_processes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                AppConfig(data_dir=Path(directory)),
                enabled_plugins={"ocr", "transcription", "ai_summary"},
            )
            services = app.state.plugin_runtime.services
            hosts = [
                services.get("classroom_process_host"),
                services.get("ocr_process_host"),
                services.get("transcription_process_host"),
                services.get("ai_summary_process_host"),
            ]

            with TestClient(app):
                for host in hosts:
                    self.assertTrue(host.running)
                    self.assertGreater(host.pid, 0)
                    self.assertNotEqual(host.pid, os.getpid())

                self.assertEqual(
                    type(services.get("classroom_client")).__name__,
                    "ClassroomClientProcessProxy",
                )
                self.assertEqual(
                    type(services.get("ocr_service")).__name__,
                    "OcrProcessProxy",
                )
                self.assertEqual(
                    type(services.get("transcription_service")).__name__,
                    "TranscriptionProcessProxy",
                )
                self.assertEqual(
                    type(services.get("ai_summary_service")).__name__,
                    "AiSummaryProcessProxy",
                )

            for host in hosts:
                self.assertFalse(host.running)

    def test_stateful_selection_keeps_session_state_main_and_runs_business_loop_in_worker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(
                AppConfig(data_dir=Path(directory)),
                enabled_plugins={"course_selection"},
            )
            services = app.state.plugin_runtime.services
            host = services.get("course_selection_process_host")

            self.assertEqual(
                type(services.get("course_selection")).__name__,
                "CourseSelectionProcessFacade",
            )
            self.assertEqual(
                type(services.get("course_selection_local")).__name__,
                "CourseSelectionAssistant",
            )
            self.assertEqual(
                type(services.get("auto_selection")).__name__,
                "AutoSelectionProcessProxy",
            )

            with TestClient(app):
                self.assertTrue(host.running)
                self.assertGreater(host.pid, 0)
                self.assertNotEqual(host.pid, os.getpid())
                self.assertEqual(services.get("auto_selection").list_jobs(), [])

            self.assertFalse(host.running)

    def test_attendance_plugin_owns_qr_and_ketangpai_routes(self) -> None:
        app = create_app(enabled_plugins={"attendance"})
        paths = {getattr(route, "path", "") for route in app.routes}
        services = app.state.plugin_runtime.services

        self.assertIn("/accounts/ketangpai/login", paths)
        self.assertIn(
            "/api/courses/{course_id}/lessons/{lesson_id}/live/ppt/{slide_id}/qr",
            paths,
        )
        self.assertTrue(services.has("ketangpai_client"))
        self.assertTrue(services.has("qr_scanner"))
        self.assertTrue(services.has("attendance_scanner_process_host"))
        self.assertEqual(
            type(services.get("qr_scanner")).__name__,
            "AttendanceScannerProcessProxy",
        )
        self.assertFalse(services.has("extraction_service"))

    def test_selection_only_app_has_no_classroom_routes_or_services(self) -> None:
        app = create_app(enabled_plugins={"course_selection"})
        paths = {getattr(route, "path", "") for route in app.routes}
        services = app.state.plugin_runtime.services

        self.assertIn("/selection/info", paths)
        self.assertIn("/api/selection/catalog", paths)
        self.assertNotIn("/classroom", paths)
        self.assertNotIn("/courses", paths)
        self.assertTrue(services.has("course_selection"))
        self.assertTrue(services.has("auto_selection"))
        self.assertFalse(services.has("classroom_client"))
        self.assertFalse(services.has("extraction_service"))

    def test_core_only_app_keeps_health_and_settings(self) -> None:
        app = create_app(enabled_plugins=set())
        paths = {getattr(route, "path", "") for route in app.routes}
        services = app.state.plugin_runtime.services

        self.assertIn("/health", paths)
        self.assertIn("/settings", paths)
        self.assertIn("/plugins", paths)
        self.assertNotIn("/classroom", paths)
        self.assertNotIn("/selection/info", paths)
        self.assertEqual(
            set(services.keys),
            {
                "settings_store",
                "cache_store",
                "task_manager",
                "credential_store",
                "verification_broker",
                "login_lock",
                "plugin_manager",
                "plugin_rpc",
                "templates",
            },
        )


    def test_registry_rejects_incompatible_plugin_api(self) -> None:
        with self.assertRaisesRegex(ValueError, "uses API"):
            PluginRegistry(
                (
                    PluginSpec(
                        id="future",
                        name="Future",
                        api_version=999,
                    ),
                )
            )


class PluginRuntimeTests(unittest.IsolatedAsyncioTestCase):
    async def test_service_route_and_lifecycle_hooks_are_owned_by_runtime(self) -> None:
        events: list[str] = []

        def setup(context) -> None:
            events.append("service")
            context.services.add("example", object())

        async def startup(context) -> None:
            self.assertTrue(context.services.has("example"))
            events.append("start")

        def shutdown(context) -> None:
            self.assertTrue(context.services.has("example"))
            events.append("stop")

        spec = PluginSpec(
            id="example",
            name="Example",
            service_factory=setup,
            startup=startup,
            shutdown=shutdown,
        )
        runtime = PluginRuntime(PluginRegistry((spec,)), object(), self)
        runtime.initialize_services()

        router = APIRouter()

        @router.get("/example")
        def example() -> dict[str, bool]:
            return {"ok": True}

        runtime.attach_router("example", router)
        app = FastAPI()
        runtime.include_routes(app)

        await runtime.start()
        await runtime.stop()

        self.assertEqual(events, ["service", "start", "stop"])
        self.assertIn("/example", {getattr(route, "path", "") for route in app.routes})


if __name__ == "__main__":
    unittest.main()
