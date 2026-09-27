from __future__ import annotations

import importlib.metadata
import json
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path

from fuckclassroom import __version__ as APP_VERSION
from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.plugins import PLUGIN_API_VERSION
from fuckclassroom.plugins import build_plugin_registry
from fuckclassroom.plugins.manager import (
    DependencySpec,
    PluginDescriptor,
    PluginManager,
    PluginManagerError,
)
from fuckclassroom.plugins.rpc import PLUGIN_RPC_API_VERSION
from fuckclassroom.web import create_app


class PluginManagerTests(unittest.TestCase):
    def test_default_state_keeps_all_builtin_plugins_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))

            self.assertEqual(
                manager.desired_enabled(),
                {
                    "core_ui",
                    "classroom",
                    "processing",
                    "transcription",
                    "ocr",
                    "ai_summary",
                    "attendance",
                    "course_selection",
                },
            )
            self.assertEqual(manager.selection_source, "default")
            self.assertFalse(manager.state_path.exists())

    def test_disabling_dependency_also_disables_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))

            desired = manager.disable("classroom")

            self.assertEqual(desired, {"core_ui", "course_selection"})
            payload = json.loads(manager.state_path.read_text(encoding="utf-8"))
            self.assertEqual(
                set(payload["selected"]),
                {"core_ui", "course_selection"},
            )

    def test_enabling_plugin_automatically_enables_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            manager.disable("classroom")

            desired = manager.enable("transcription")

            self.assertTrue(
                {"core_ui", "classroom", "processing", "transcription"}.issubset(desired)
            )
            self.assertNotIn("ocr", desired)
            self.assertNotIn("ai_summary", desired)
            self.assertNotIn("attendance", desired)

    def test_enabling_independent_plugin_ignores_unrelated_missing_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            plugin_root = data_dir / "plugins" / "independent"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "independent",
                        "name": "Independent",
                        "api_version": PLUGIN_API_VERSION,
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='independent', name='Independent')\n",
                encoding="utf-8",
            )
            (data_dir / "plugin_state.json").write_text(
                json.dumps({"selected": ["core_ui", "classroom"]}),
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=data_dir))
            missing_playwright = [
                {
                    "label": "Playwright + Chromium",
                    "installed": False,
                    "optional": False,
                }
            ]

            original_dependency_status = manager.dependency_status

            def dependency_status(plugin_id: str):
                if plugin_id == "classroom":
                    return missing_playwright
                return original_dependency_status(plugin_id)

            with patch.object(manager, "dependency_status", side_effect=dependency_status):
                desired = manager.enable("independent")

            self.assertIn("independent", desired)
            self.assertIn("classroom", desired)
            payload = json.loads(manager.state_path.read_text(encoding="utf-8"))
            self.assertIn("independent", payload["selected"])

    def test_saved_state_controls_next_application_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            manager = PluginManager(config)
            manager.disable("classroom")

            app = create_app(config)
            paths = {getattr(route, "path", "") for route in app.routes}

            self.assertNotIn("/classroom", paths)
            self.assertNotIn("/courses", paths)
            self.assertIn("/selection/info", paths)
            self.assertEqual(
                set(app.state.plugin_registry.plugin_ids),
                {"core_ui", "course_selection"},
            )

    def test_core_ui_cannot_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))

            with self.assertRaisesRegex(PluginManagerError, "不能禁用"):
                manager.disable("core_ui")

    def test_external_plugin_can_be_installed_enabled_and_loaded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source_plugin"
            source.mkdir()
            (source / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "demo_plugin",
                        "name": "Demo Plugin",
                        "version": "0.1.0",
                        "api_version": PLUGIN_API_VERSION,
                        "app_requires": ">=0.1,<0.3",
                        "description": "test plugin",
                        "requires": ["classroom"],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (source / "plugin.py").write_text(
                """
from fuckclassroom.core.plugins import PluginSpec


def build_plugin():
    return PluginSpec(
        id="demo_plugin",
        name="Demo Plugin",
        order=70,
        requires=("classroom",),
    )
""".lstrip(),
                encoding="utf-8",
            )

            config = AppConfig(data_dir=root / "data")
            manager = PluginManager(config)
            installed = manager.install_from_path(str(source))

            self.assertEqual(installed.id, "demo_plugin")
            self.assertTrue((config.data_dir / "plugins" / "demo_plugin" / "plugin.json").is_file())
            self.assertNotIn("demo_plugin", manager.desired_enabled())

            manager.enable("demo_plugin")
            registry = build_plugin_registry(config=config, manager=manager)

            self.assertIn("demo_plugin", registry.plugin_ids)
            self.assertIn("classroom", registry.plugin_ids)

    def test_externalized_lab_selection_can_use_canonical_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "lab_selection"
            source.mkdir()
            (source / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "lab_selection",
                        "name": "实验课选课",
                        "version": "0.1.0",
                        "api_version": PLUGIN_API_VERSION,
                        "app_requires": ">=0.1,<0.3",
                        "requires": ["core_ui"],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (source / "plugin.py").write_text(
                """
from fuckclassroom.core.plugins import PluginSpec


def build_plugin():
    return PluginSpec(
        id="lab_selection",
        name="实验课选课",
        order=21,
        api_version=1,
        requires=("core_ui",),
    )
""".lstrip(),
                encoding="utf-8",
            )

            config = AppConfig(data_dir=root / "data")
            manager = PluginManager(config)
            installed = manager.install_from_path(str(source))

            self.assertEqual(installed.id, "lab_selection")
            self.assertFalse(installed.builtin)
            self.assertNotIn("lab_selection", {item.id for item in manager.builtin_descriptors})

            desired = manager.enable("lab_selection")
            registry = build_plugin_registry(config=config, manager=manager)

            self.assertIn("lab_selection", desired)
            self.assertIn("lab_selection", registry.plugin_ids)

    def test_plugin_api_and_app_version_compatibility_are_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            rows = {row["id"]: row for row in manager.status_rows(())}

            self.assertEqual(manager.api_version, PLUGIN_API_VERSION)
            self.assertEqual(manager.app_version, APP_VERSION)
            self.assertTrue(rows["classroom"]["compatible"])
            self.assertEqual(rows["classroom"]["api_version"], PLUGIN_API_VERSION)

    def test_incompatible_plugin_api_cannot_be_enabled_and_is_skipped_on_boot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            plugin_root = data_dir / "plugins" / "future_plugin"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "future_plugin",
                        "name": "Future Plugin",
                        "api_version": PLUGIN_API_VERSION + 1,
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "raise RuntimeError('incompatible plugin must not execute')\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=data_dir))

            with self.assertRaisesRegex(PluginManagerError, "不兼容"):
                manager.enable("future_plugin")

            (data_dir / "plugin_state.json").write_text(
                json.dumps({"selected": ["core_ui", "future_plugin"]}),
                encoding="utf-8",
            )
            registry = build_plugin_registry(
                config=manager.config,
                manager=manager,
            )
            self.assertEqual(registry.plugin_ids, ("core_ui",))

            rows = {row["id"]: row for row in manager.status_rows(registry.plugin_ids)}
            self.assertFalse(rows["future_plugin"]["compatible"])
            self.assertIn("插件 API", rows["future_plugin"]["compatibility_message"])

    def test_incompatible_app_version_cannot_be_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "future_app"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "future_app",
                        "name": "Future App",
                        "api_version": PLUGIN_API_VERSION,
                        "app_requires": ">=99",
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='future_app', name='Future App')\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))

            with self.assertRaisesRegex(PluginManagerError, "不兼容"):
                manager.enable("future_app")

            descriptor = manager.descriptor_map()["future_app"]
            compatibility = manager.compatibility_status(descriptor)
            self.assertFalse(compatibility["compatible"])
            self.assertIn("需要 FuckClassroom", compatibility["message"])

    def test_manifest_and_plugin_spec_api_versions_must_match(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "api_mismatch"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "api_mismatch",
                        "name": "API Mismatch",
                        "api_version": PLUGIN_API_VERSION,
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='api_mismatch', name='API Mismatch', api_version=2)\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))
            descriptor = manager.descriptor_map()["api_mismatch"]

            with self.assertRaisesRegex(PluginManagerError, "api_version"):
                manager.load_spec(descriptor)

    def test_builtin_dependency_status_is_exposed(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))

            transcription = manager.dependency_status("transcription")
            labels = {str(item["label"]) for item in transcription}

            self.assertEqual(
                labels,
                {"faster-whisper", "Hugging Face Hub", "OpenCC"},
            )
            self.assertTrue(all(bool(item["installed"]) for item in transcription))

            attendance = manager.dependency_status("attendance")
            wxscan = next(
                item for item in attendance if item["label"] == "wxscan 原生扩展"
            )
            self.assertTrue(wxscan["optional"])

    def test_external_plugin_with_missing_python_dependency_cannot_be_enabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "needs_dep"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "needs_dep",
                        "name": "Needs Dep",
                        "requires": [],
                        "entry": "plugin.py",
                        "python_requires": [
                            {
                                "requirement": "definitely-missing-fuckclassroom-package>=1",
                                "import": "definitely_missing_fuckclassroom_package",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='needs_dep', name='Needs Dep')\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))

            with self.assertRaisesRegex(PluginManagerError, "请先安装依赖"):
                manager.enable("needs_dep")

    def test_missing_external_dependency_is_skipped_during_boot(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            data_dir = root / "data"
            plugin_root = data_dir / "plugins" / "needs_dep"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "needs_dep",
                        "name": "Needs Dep",
                        "requires": [],
                        "entry": "plugin.py",
                        "python_requires": [
                            {
                                "requirement": "definitely-missing-fuckclassroom-package>=1",
                                "import": "definitely_missing_fuckclassroom_package",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "import definitely_missing_fuckclassroom_package\n"
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='needs_dep', name='Needs Dep')\n",
                encoding="utf-8",
            )
            (data_dir / "plugin_state.json").write_text(
                json.dumps({"selected": ["core_ui", "needs_dep"]}),
                encoding="utf-8",
            )

            manager = PluginManager(AppConfig(data_dir=data_dir))
            registry = build_plugin_registry(
                config=manager.config,
                manager=manager,
            )

            self.assertEqual(registry.plugin_ids, ("core_ui",))
            rows = {row["id"]: row for row in manager.status_rows(registry.plugin_ids)}
            self.assertTrue(rows["needs_dep"]["desired_enabled"])
            self.assertFalse(rows["needs_dep"]["current_enabled"])
            self.assertTrue(rows["needs_dep"]["dependency_issue"])

    def test_external_dependency_manifest_rejects_direct_url(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source_plugin"
            source.mkdir()
            (source / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "unsafe_dep",
                        "name": "Unsafe Dep",
                        "requires": [],
                        "entry": "plugin.py",
                        "python_requires": [
                            "demo-package @ https://example.com/demo.whl"
                        ],
                    }
                ),
                encoding="utf-8",
            )
            (source / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='unsafe_dep', name='Unsafe Dep')\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))

            with self.assertRaisesRegex(PluginManagerError, "不允许 URL"):
                manager.install_from_path(str(source))

    def test_requirements_are_split_between_core_and_plugins(self) -> None:
        root = Path("requirements.txt").read_text(encoding="utf-8")
        all_requirements = Path("requirements/all.txt").read_text(encoding="utf-8")

        self.assertEqual(root.strip(), "-r requirements/core.txt")
        self.assertIn("packaging>=24.0", Path("requirements/core.txt").read_text(encoding="utf-8"))
        self.assertIn("faster-whisper>=1.1", Path("requirements/transcription.txt").read_text(encoding="utf-8"))
        self.assertIn("rapidocr-onnxruntime>=1.2", Path("requirements/ocr.txt").read_text(encoding="utf-8"))
        self.assertIn("opencv-python>=4.8", Path("requirements/attendance.txt").read_text(encoding="utf-8"))
        self.assertIn("-r transcription.txt", all_requirements)
        self.assertIn("-r attendance.txt", all_requirements)

    def test_external_plugin_is_not_removable_while_configured_or_running(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "demo_plugin"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "demo_plugin",
                        "name": "Demo Plugin",
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='demo_plugin', name='Demo Plugin')\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))
            manager.enable("demo_plugin")

            with self.assertRaisesRegex(PluginManagerError, "仍被配置为启用"):
                manager.remove("demo_plugin")

            manager.disable("demo_plugin")
            with self.assertRaisesRegex(PluginManagerError, "当前仍在运行"):
                manager.remove("demo_plugin", current_enabled={"demo_plugin"})

            manager.remove("demo_plugin")
            self.assertFalse(plugin_root.exists())


    def test_process_isolated_plugin_is_not_imported_in_parent(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "isolated_demo"
            plugin_root.mkdir(parents=True)
            marker = plugin_root / "imported.txt"
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
                "from pathlib import Path\n"
                "Path(__file__).with_name('imported.txt').write_text('child', encoding='utf-8')\n"
                "def handle_request(request, context):\n"
                "    return {'ok': True}\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))
            descriptor = manager.descriptor_map()["isolated_demo"]

            spec = manager.load_spec(descriptor)

            self.assertEqual(spec.id, "isolated_demo")
            self.assertEqual(descriptor.execution, "process")
            self.assertEqual(descriptor.source_label, "外部 · 独立进程")
            self.assertFalse(marker.exists())
            self.assertIsNotNone(spec.route_factory)
            self.assertIsNotNone(spec.startup)
            self.assertIsNotNone(spec.shutdown)


    def test_plugin_toggle_supports_json_without_page_redirect(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            reload_calls: list[bool] = []

            async def request_reload() -> bool:
                reload_calls.append(True)
                return True

            app = create_app(config, reload_callback=request_reload)
            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.post(
                    "/plugins/ocr/disable",
                    headers={"Accept": "application/json"},
                )

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["plugin_id"], "ocr")
            self.assertEqual(payload["action"], "disable")
            self.assertNotIn("ocr", payload["desired_enabled"])
            self.assertTrue(payload["reload_scheduled"])
            self.assertEqual(reload_calls, [True])


    def test_plugin_toggle_json_409_preserves_backend_detail(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "plugins" / "future_app"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "future_app",
                        "name": "Future App",
                        "api_version": PLUGIN_API_VERSION,
                        "app_requires": ">=99",
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='future_app', name='Future App')\n",
                encoding="utf-8",
            )
            (root / "plugin_state.json").write_text(
                json.dumps({"selected": ["core_ui"]}),
                encoding="utf-8",
            )
            config = AppConfig(data_dir=root)
            app = create_app(config)
            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.post(
                    "/plugins/future_app/enable",
                    headers={"Accept": "application/json"},
                )

            self.assertEqual(response.status_code, 409)
            detail = response.json()["detail"]
            self.assertIn("插件 Future App 不兼容", detail)
            self.assertIn("需要 FuckClassroom >=99", detail)

    def test_process_plugin_rpc_manifest_is_validated(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "rpc_demo"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "rpc_demo",
                        "name": "RPC Demo",
                        "execution": "process",
                        "rpc_api_version": PLUGIN_RPC_API_VERSION,
                        "rpc_permissions": [
                            "core.settings.get",
                            "core.storage.get",
                        ],
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "def handle_request(request, context):\n"
                "    return {'ok': True}\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))
            descriptor = manager.descriptor_map()["rpc_demo"]

            self.assertEqual(descriptor.rpc_api_version, PLUGIN_RPC_API_VERSION)
            self.assertEqual(
                descriptor.rpc_permissions,
                ("core.settings.get", "core.storage.get"),
            )
            self.assertTrue(manager.compatibility_status(descriptor)["compatible"])

    def test_future_process_plugin_rpc_api_is_incompatible(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "future_rpc"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "future_rpc",
                        "name": "Future RPC",
                        "execution": "process",
                        "rpc_api_version": PLUGIN_RPC_API_VERSION + 1,
                        "rpc_permissions": [],
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "def handle_request(request, context):\n"
                "    return {'ok': True}\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))
            descriptor = manager.descriptor_map()["future_rpc"]
            compatibility = manager.compatibility_status(descriptor)

            self.assertFalse(compatibility["compatible"])
            self.assertIn("Plugin RPC API", compatibility["message"])
            with self.assertRaisesRegex(PluginManagerError, "不兼容"):
                manager.enable("future_rpc")

    def test_rpc_fields_are_rejected_for_in_process_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "bad_rpc"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "bad_rpc",
                        "name": "Bad RPC",
                        "rpc_permissions": ["core.settings.get"],
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='bad_rpc', name='Bad RPC')\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))

            with self.assertRaisesRegex(PluginManagerError, "仅适用于 execution=process"):
                manager.descriptor_map()


    def test_external_plugin_cannot_request_internal_worker_rpc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "internal_rpc"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "internal_rpc",
                        "name": "Internal RPC",
                        "execution": "process",
                        "rpc_permissions": ["internal.classroom.session.ensure"],
                        "requires": [],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "def handle_request(request, context):\n"
                "    return {'ok': True}\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))

            with self.assertRaisesRegex(PluginManagerError, "内部 Worker 保留能力"):
                manager.descriptor_map()


    def test_playwright_probe_uses_short_ttl_cache_for_success_and_failure(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            succeeded = type("Completed", (), {"returncode": 0})()
            failed = type("Completed", (), {"returncode": 1})()

            with patch(
                "fuckclassroom.plugins.manager.subprocess.run",
                side_effect=[succeeded, failed],
            ) as run:
                self.assertTrue(manager._probe_dependency("playwright_chromium"))
                self.assertTrue(manager._probe_dependency("playwright_chromium"))
                self.assertEqual(run.call_count, 1)

                with manager._dependency_probe_cache_lock:
                    _, cached_result = manager._dependency_probe_cache["playwright_chromium"]
                    manager._dependency_probe_cache["playwright_chromium"] = (
                        0.0,
                        cached_result,
                    )

                self.assertFalse(manager._probe_dependency("playwright_chromium"))
                self.assertFalse(manager._probe_dependency("playwright_chromium"))

            self.assertEqual(run.call_count, 2)
            command = run.call_args_list[0].args[0]
            self.assertEqual(command[:2], [__import__("sys").executable, "-c"])
            self.assertIn("chromium.launch(headless=True)", command[2])

    def test_new_manager_generation_does_not_reuse_probe_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            config = AppConfig(data_dir=Path(directory))
            first = PluginManager(config)
            second = PluginManager(config)
            succeeded = type("Completed", (), {"returncode": 0})()
            failed = type("Completed", (), {"returncode": 1})()

            with patch(
                "fuckclassroom.plugins.manager.subprocess.run",
                side_effect=[succeeded, failed],
            ) as run:
                self.assertTrue(first._probe_dependency("playwright_chromium"))
                self.assertFalse(second._probe_dependency("playwright_chromium"))

            self.assertEqual(run.call_count, 2)

    def test_dependency_install_invalidates_probe_cache_before_and_after_repair(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            dependency = DependencySpec(
                "playwright>=1.45",
                "playwright",
                "Playwright + Chromium",
                post_install=(("{python}", "-m", "playwright", "install", "chromium"),),
                probe="playwright_chromium",
            )
            descriptor = PluginDescriptor(
                "demo_probe",
                "Demo Probe",
                "",
                "1.0.0",
                100,
                (),
                True,
                python_requirements=(dependency,),
            )
            statuses = [
                [{"label": "Playwright + Chromium", "installed": False, "optional": False}],
                [{"label": "Playwright + Chromium", "installed": True, "optional": False}],
            ]
            manager._dependency_probe_cache["playwright_chromium"] = (999999999.0, True)

            with (
                patch.object(manager, "descriptor_map", return_value={"demo_probe": descriptor}),
                patch.object(manager, "dependency_status", side_effect=statuses),
                patch.object(manager, "_run_install_command"),
            ):
                manager.install_dependencies("demo_probe")

            self.assertNotIn(
                "playwright_chromium",
                manager._dependency_probe_cache,
            )

    def test_importable_module_is_not_missing_when_distribution_name_differs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            dependency = DependencySpec("opencv-python>=4.8", "cv2", "OpenCV")

            with (
                patch(
                    "fuckclassroom.plugins.manager.importlib.util.find_spec",
                    return_value=object(),
                ),
                patch(
                    "fuckclassroom.plugins.manager.importlib.metadata.version",
                    side_effect=importlib.metadata.PackageNotFoundError,
                ),
            ):
                status = manager._dependency_status(dependency)

            self.assertTrue(status["installed"])
            self.assertEqual(status["version"], "")

    def test_known_distribution_version_still_enforces_requirement(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            dependency = DependencySpec("opencv-python>=4.8", "cv2", "OpenCV")

            with (
                patch(
                    "fuckclassroom.plugins.manager.importlib.util.find_spec",
                    return_value=object(),
                ),
                patch(
                    "fuckclassroom.plugins.manager.importlib.metadata.version",
                    return_value="4.7.0",
                ),
            ):
                status = manager._dependency_status(dependency)

            self.assertFalse(status["installed"])

    def test_install_dependencies_only_processes_currently_missing_items(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            required = DependencySpec(
                "already-present>=1",
                "already_present",
                "Already Present",
                post_install=(("must-not-run",),),
            )
            optional = DependencySpec(
                "optional-addon>=1",
                "optional_addon",
                "Optional Addon",
                optional=True,
                post_install=(("optional-post",),),
            )
            descriptor = PluginDescriptor(
                "demo_dep",
                "Demo Dep",
                "",
                "1.0.0",
                100,
                (),
                True,
                python_requirements=(required, optional),
            )
            initial = [
                {
                    "label": "Already Present",
                    "installed": True,
                    "optional": False,
                },
                {
                    "label": "Optional Addon",
                    "installed": False,
                    "optional": True,
                },
            ]
            final = [
                {
                    "label": "Already Present",
                    "installed": True,
                    "optional": False,
                },
                {
                    "label": "Optional Addon",
                    "installed": True,
                    "optional": True,
                },
            ]
            commands: list[tuple[list[str], str]] = []

            def record(command, progress, start_percent, end_percent, message):
                commands.append((list(command), message))

            with (
                patch.object(manager, "descriptor_map", return_value={"demo_dep": descriptor}),
                patch.object(manager, "dependency_status", side_effect=[initial, final]),
                patch.object(manager, "_run_install_command", side_effect=record),
            ):
                manager.install_dependencies("demo_dep")

            self.assertFalse(any("必需依赖" in message for _, message in commands))
            self.assertTrue(
                any("optional-addon>=1" in command for command, _ in commands)
            )
            self.assertTrue(any(command == ["optional-post"] for command, _ in commands))
            self.assertFalse(any("must-not-run" in command for command, _ in commands))

    def test_builtin_required_dependency_blocks_enable_and_runtime(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            manager = PluginManager(AppConfig(data_dir=data_dir))
            manager.state_path.write_text(
                json.dumps({"selected": ["core_ui"]}),
                encoding="utf-8",
            )
            missing = [
                {
                    "label": "Playwright + Chromium",
                    "installed": False,
                    "optional": False,
                }
            ]

            def dependency_status(plugin_id: str):
                return missing if plugin_id == "classroom" else []

            with (
                patch.object(manager, "dependency_status", side_effect=dependency_status),
                patch.object(manager, "load_spec"),
            ):
                with self.assertRaisesRegex(PluginManagerError, "请先安装依赖"):
                    manager.enable("classroom")

            manager.state_path.write_text(
                json.dumps({"selected": ["core_ui", "classroom"]}),
                encoding="utf-8",
            )
            with patch.object(
                manager,
                "dependency_status",
                side_effect=dependency_status,
            ):
                registry = build_plugin_registry(
                    config=manager.config,
                    manager=manager,
                )
            self.assertEqual(registry.plugin_ids, ("core_ui",))

    def test_stale_plugin_state_is_pruned_before_mutation(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            manager = PluginManager(AppConfig(data_dir=data_dir))
            manager.state_path.write_text(
                json.dumps({"selected": ["core_ui", "removed_plugin"]}),
                encoding="utf-8",
            )

            with (
                patch.object(manager, "dependency_status", return_value=[]),
                patch.object(manager, "load_spec"),
            ):
                desired = manager.enable("ai_summary")

            payload = json.loads(manager.state_path.read_text(encoding="utf-8"))
            self.assertNotIn("removed_plugin", payload["selected"])
            self.assertIn("ai_summary", payload["selected"])
            self.assertIn("ai_summary", desired)

    def test_disable_validation_failure_does_not_partially_write_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            manager = PluginManager(AppConfig(data_dir=data_dir))
            manager.state_path.write_text(
                json.dumps({"selected": ["core_ui", "classroom"]}, indent=2),
                encoding="utf-8",
            )
            before = manager.state_path.read_bytes()

            with patch.object(
                manager,
                "_expand_dependencies",
                side_effect=PluginManagerError("validation failed"),
            ):
                with self.assertRaisesRegex(PluginManagerError, "validation failed"):
                    manager.disable("classroom")

            self.assertEqual(manager.state_path.read_bytes(), before)

    def test_dependency_repair_for_enabled_plugin_requests_runtime_reload(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            reload_calls: list[bool] = []

            def request_reload_from_worker() -> bool:
                reload_calls.append(True)
                return True

            app = create_app(
                AppConfig(data_dir=Path(directory)),
                enabled_plugins={"core_ui"},
                reload_background_callback=request_reload_from_worker,
            )
            from fastapi.testclient import TestClient

            with (
                patch.object(PluginManager, "install_dependencies", return_value=None),
                TestClient(app) as client,
            ):
                response = client.post(
                    "/plugins/core_ui/dependencies/install",
                    headers={"Accept": "application/json"},
                )
                self.assertEqual(response.status_code, 200)
                status_url = response.json()["status_url"]
                task = {}
                for _ in range(100):
                    task = client.get(status_url).json()
                    if task["status"] != "running":
                        break
                    time.sleep(0.01)

            self.assertEqual(task["status"], "succeeded")
            self.assertEqual(reload_calls, [True])
            self.assertIn("已请求自动应用", task["result_url"])

    def test_zip_with_abnormal_compression_ratio_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            archive_path = root / "bomb.zip"
            with zipfile.ZipFile(
                archive_path,
                "w",
                compression=zipfile.ZIP_DEFLATED,
                compresslevel=9,
            ) as archive:
                archive.writestr("plugin/payload.bin", b"0" * (2 * 1024 * 1024))

            manager = PluginManager(AppConfig(data_dir=root / "data"))
            with self.assertRaisesRegex(PluginManagerError, "压缩比"):
                manager.install_from_path(str(archive_path))


if __name__ == "__main__":
    unittest.main()
