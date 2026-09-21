from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins import build_plugin_registry
from fuckclassroom.plugins.manager import PluginManager, PluginManagerError
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


if __name__ == "__main__":
    unittest.main()
