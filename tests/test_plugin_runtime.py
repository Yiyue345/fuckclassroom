from __future__ import annotations

import ast
import tempfile
import unittest
from pathlib import Path

from fastapi import APIRouter, FastAPI

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

    def test_business_plugins_are_not_registered_as_builtins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            from fuckclassroom.plugins.manager import PluginManager

            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            self.assertEqual(
                [item.id for item in manager.builtin_descriptors],
                ["core_ui"],
            )
            self.assertEqual(
                set(manager.descriptor_map()),
                {"core_ui"},
            )

    def test_explicit_uninstalled_business_plugin_is_rejected(self) -> None:
        from fuckclassroom.plugins.manager import PluginManagerError

        with self.assertRaisesRegex(PluginManagerError, "未知插件"):
            build_plugin_registry({"classroom"})

    def test_default_app_starts_with_core_ui_only(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            paths = {getattr(route, "path", "") for route in app.routes}

            self.assertEqual(app.state.plugin_registry.plugin_ids, ("core_ui",))
            self.assertIn("/health", paths)
            self.assertIn("/settings", paths)
            self.assertIn("/plugins", paths)
            self.assertNotIn("/classroom", paths)
            self.assertNotIn("/selection/info", paths)

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
    async def test_registry_rejects_dependency_cycles(self) -> None:
        with self.assertRaisesRegex(ValueError, "dependency cycle"):
            PluginRegistry(
                (
                    PluginSpec(id="plugin_a", name="A", requires=("plugin_b",)),
                    PluginSpec(id="plugin_b", name="B", requires=("plugin_a",)),
                )
            )

    async def test_runtime_uses_dependency_topology_before_order_and_reverses_shutdown(self) -> None:
        events: list[str] = []

        def service(name: str):
            def setup(context) -> None:
                events.append(f"service:{name}")
                context.services.add(name, object())
            return setup

        async def startup(name: str, dependency: str | None = None) -> None:
            if dependency:
                self.assertTrue(runtime.services.has(dependency))
            events.append(f"start:{name}")

        def shutdown(name: str) -> None:
            events.append(f"stop:{name}")

        dependency = PluginSpec(
            id="dependency",
            name="Dependency",
            order=200,
            service_factory=service("dependency"),
            startup=lambda context: startup("dependency"),
            shutdown=lambda context: shutdown("dependency"),
        )
        consumer = PluginSpec(
            id="consumer",
            name="Consumer",
            order=10,
            requires=("dependency",),
            service_factory=service("consumer"),
            startup=lambda context: startup("consumer", "dependency"),
            shutdown=lambda context: shutdown("consumer"),
        )
        independent = PluginSpec(
            id="independent",
            name="Independent",
            order=20,
            service_factory=service("independent"),
            startup=lambda context: startup("independent"),
            shutdown=lambda context: shutdown("independent"),
        )
        registry = PluginRegistry((consumer, dependency, independent))
        runtime = PluginRuntime(registry, object(), self)

        self.assertEqual(
            registry.plugin_ids,
            ("independent", "dependency", "consumer"),
        )

        runtime.initialize_services()
        await runtime.start()
        await runtime.stop()

        self.assertEqual(
            events,
            [
                "service:independent",
                "service:dependency",
                "service:consumer",
                "start:independent",
                "start:dependency",
                "start:consumer",
                "stop:consumer",
                "stop:dependency",
                "stop:independent",
            ],
        )

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
