from __future__ import annotations

import unittest
from pathlib import Path

from fastapi import APIRouter, FastAPI

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

        for plugin_id in ("core_ui", "classroom", "course_selection"):
            root = Path("fuckclassroom/plugins") / plugin_id
            self.assertTrue((root / "plugin.py").exists())
            self.assertTrue((root / "routes.py").exists())
            self.assertTrue((root / "services.py").exists())

        self.assertTrue(
            (Path("fuckclassroom/plugins/classroom") / "lifecycle.py").exists()
        )
        self.assertTrue(
            (Path("fuckclassroom/plugins/course_selection") / "lifecycle.py").exists()
        )

    def test_registry_can_enable_feature_plugins_independently(self) -> None:
        classroom = build_plugin_registry({"classroom"})
        selection = build_plugin_registry({"course_selection"})

        self.assertEqual(classroom.plugin_ids, ("core_ui", "classroom"))
        self.assertEqual(selection.plugin_ids, ("core_ui", "course_selection"))

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
                "templates",
            },
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
