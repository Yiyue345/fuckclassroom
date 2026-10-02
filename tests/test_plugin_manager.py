from __future__ import annotations

import importlib.metadata
import json
import shutil
import tempfile
import time
import unittest
import zipfile
from unittest.mock import patch
from pathlib import Path
from urllib.parse import unquote

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


def _write_external_plugin(
    data_dir: Path,
    plugin_id: str,
    *,
    requires: tuple[str, ...] = (),
    api_version: int = PLUGIN_API_VERSION,
) -> Path:
    root = data_dir / "plugins" / plugin_id
    root.mkdir(parents=True, exist_ok=True)
    (root / "plugin.json").write_text(
        json.dumps(
            {
                "id": plugin_id,
                "name": plugin_id,
                "version": "0.1.0",
                "api_version": api_version,
                "app_requires": ">=0.1,<0.3",
                "requires": list(requires),
                "entry": "plugin.py",
            }
        ),
        encoding="utf-8",
    )
    (root / "plugin.py").write_text(
        "from fuckclassroom.core.plugins import PluginSpec\n"
        "def build_plugin():\n"
        f"    return PluginSpec(id={plugin_id!r}, name={plugin_id!r}, "
        f"api_version={api_version}, requires={requires!r})\n",
        encoding="utf-8",
    )
    return root


class PluginManagerTests(unittest.TestCase):
    def test_default_state_enables_only_core_ui(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))

            self.assertEqual(manager.desired_enabled(), {"core_ui"})
            self.assertEqual(
                {item.id for item in manager.builtin_descriptors},
                {"core_ui"},
            )
            self.assertEqual(manager.selection_source, "default")
            self.assertFalse(manager.state_path.exists())

    def test_disabling_external_dependency_also_disables_dependents(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _write_external_plugin(data_dir, "base_plugin")
            _write_external_plugin(
                data_dir,
                "dependent_plugin",
                requires=("base_plugin",),
            )
            manager = PluginManager(AppConfig(data_dir=data_dir))
            manager.enable("dependent_plugin")

            desired = manager.disable("base_plugin")

            self.assertEqual(desired, {"core_ui"})
            payload = json.loads(manager.state_path.read_text(encoding="utf-8"))
            self.assertEqual(set(payload["selected"]), {"core_ui"})

    def test_enabling_external_plugin_automatically_enables_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _write_external_plugin(data_dir, "classroom", requires=("core_ui",))
            _write_external_plugin(data_dir, "processing", requires=("classroom",))
            _write_external_plugin(data_dir, "transcription", requires=("processing",))
            manager = PluginManager(AppConfig(data_dir=data_dir))

            desired = manager.enable("transcription")

            self.assertEqual(
                desired,
                {"core_ui", "classroom", "processing", "transcription"},
            )

    def test_enabling_independent_plugin_ignores_unrelated_missing_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _write_external_plugin(data_dir, "blocked_plugin")
            _write_external_plugin(data_dir, "independent")
            (data_dir / "plugin_state.json").write_text(
                json.dumps({"selected": ["core_ui", "blocked_plugin"]}),
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=data_dir))
            original_dependency_status = manager.dependency_status

            def dependency_status(plugin_id: str):
                if plugin_id == "blocked_plugin":
                    return [
                        {
                            "label": "Missing runtime",
                            "installed": False,
                            "optional": False,
                        }
                    ]
                return original_dependency_status(plugin_id)

            with patch.object(manager, "dependency_status", side_effect=dependency_status):
                desired = manager.enable("independent")

            self.assertIn("independent", desired)
            self.assertIn("blocked_plugin", desired)
            payload = json.loads(manager.state_path.read_text(encoding="utf-8"))
            self.assertIn("independent", payload["selected"])

    def test_saved_external_state_controls_next_application_start(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            _write_external_plugin(data_dir, "demo_plugin")
            (data_dir / "plugin_state.json").write_text(
                json.dumps({"selected": ["core_ui", "demo_plugin"]}),
                encoding="utf-8",
            )

            app = create_app(AppConfig(data_dir=data_dir))

            self.assertEqual(
                set(app.state.plugin_registry.plugin_ids),
                {"core_ui", "demo_plugin"},
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
                        "requires": [],
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
        requires=(),
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
            self.assertEqual(set(registry.plugin_ids), {"core_ui", "demo_plugin"})

    def test_external_business_plugin_can_use_former_builtin_canonical_id(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "classroom"
            source.mkdir()
            (source / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "classroom",
                        "name": "课程录播",
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
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='classroom', name='课程录播', "
                "requires=('core_ui',))\n",
                encoding="utf-8",
            )

            config = AppConfig(data_dir=root / "data")
            manager = PluginManager(config)
            installed = manager.install_from_path(str(source))

            self.assertEqual(installed.id, "classroom")
            self.assertFalse(installed.builtin)
            self.assertNotIn(
                "classroom",
                {item.id for item in manager.builtin_descriptors},
            )

            manager.enable("classroom")
            registry = build_plugin_registry(config=config, manager=manager)
            self.assertEqual(set(registry.plugin_ids), {"core_ui", "classroom"})

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
            self.assertTrue(rows["core_ui"]["compatible"])
            self.assertEqual(rows["core_ui"]["api_version"], PLUGIN_API_VERSION)

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
            self.assertEqual(
                rows["future_plugin"]["compatibility_state"],
                "plugin_api_incompatible",
            )
            self.assertEqual(
                rows["future_plugin"]["compatibility_label"],
                "✕ Plugin API 不兼容",
            )
            self.assertIn("插件 API", rows["future_plugin"]["compatibility_message"])

    def test_app_version_mismatch_warns_but_does_not_block_enable(self) -> None:
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

            descriptor = manager.descriptor_map()["future_app"]
            compatibility = manager.compatibility_status(descriptor)

            self.assertTrue(compatibility["compatible"])
            self.assertEqual(compatibility["state"], "app_version_mismatch")
            self.assertEqual(compatibility["label"], "⚠ 主程序版本不满足")
            self.assertFalse(compatibility["app_version_satisfied"])

            enabled = manager.enable("future_app")
            self.assertIn("future_app", enabled)

            registry = build_plugin_registry(
                config=manager.config,
                manager=manager,
            )
            self.assertIn("future_app", registry.plugin_ids)

            rows = {row["id"]: row for row in manager.status_rows(registry.plugin_ids)}
            self.assertTrue(rows["future_app"]["compatible"])
            self.assertEqual(
                rows["future_app"]["compatibility_state"],
                "app_version_mismatch",
            )

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

    def test_only_core_ui_is_reserved_as_builtin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))

            self.assertEqual(
                [item.id for item in manager.builtin_descriptors],
                ["core_ui"],
            )
            self.assertEqual(manager.dependency_status("core_ui"), [])

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


    def test_status_rows_report_missing_plugin_dependencies(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "dependent"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "dependent",
                        "name": "Dependent",
                        "requires": ["missing_base"],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='dependent', name='Dependent', requires=('missing_base',))\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))

            rows = {row["id"]: row for row in manager.status_rows({"core_ui"})}

            self.assertEqual(rows["dependent"]["missing_plugin_dependencies"], ["missing_base"])
            self.assertTrue(rows["dependent"]["plugin_dependency_issue"])
            self.assertTrue(rows["dependent"]["dependency_issue"])

    def test_remove_blocks_disabled_plugin_that_is_still_required(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for plugin_id, requires in (("base_plugin", []), ("dependent", ["base_plugin"])):
                plugin_root = root / "data" / "plugins" / plugin_id
                plugin_root.mkdir(parents=True)
                (plugin_root / "plugin.json").write_text(
                    json.dumps(
                        {
                            "id": plugin_id,
                            "name": plugin_id,
                            "requires": requires,
                            "entry": "plugin.py",
                        }
                    ),
                    encoding="utf-8",
                )
                requires_tuple = repr(tuple(requires))
                (plugin_root / "plugin.py").write_text(
                    "from fuckclassroom.core.plugins import PluginSpec\n"
                    f"def build_plugin(): return PluginSpec(id={plugin_id!r}, name={plugin_id!r}, requires={requires_tuple})\n",
                    encoding="utf-8",
                )
            manager = PluginManager(AppConfig(data_dir=root / "data"))

            with self.assertRaisesRegex(PluginManagerError, "dependent"):
                manager.remove("base_plugin")

            self.assertTrue((root / "data" / "plugins" / "base_plugin").exists())

    def test_remove_rename_failure_keeps_original_plugin_intact(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "demo_plugin"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps({"id": "demo_plugin", "name": "Demo", "requires": [], "entry": "plugin.py"}),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text("# demo\n", encoding="utf-8")
            manager = PluginManager(AppConfig(data_dir=root / "data"))

            with patch.object(Path, "replace", side_effect=PermissionError("locked")):
                with self.assertRaisesRegex(PluginManagerError, "仍被占用|无写入权限"):
                    manager.remove("demo_plugin")

            self.assertTrue(plugin_root.exists())
            self.assertIn("demo_plugin", manager.descriptor_map())

    def test_remove_cleanup_failure_hides_plugin_and_returns_warning(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "demo_plugin"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps({"id": "demo_plugin", "name": "Demo", "requires": [], "entry": "plugin.py"}),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text("# demo\n", encoding="utf-8")
            manager = PluginManager(AppConfig(data_dir=root / "data"))

            with patch("fuckclassroom.plugins.manager.shutil.rmtree", side_effect=PermissionError("locked")):
                warning = manager.remove("demo_plugin")
                descriptors = manager.descriptor_map()

            self.assertIn("残留目录暂时无法清理", warning)
            self.assertFalse(plugin_root.exists())
            self.assertNotIn("demo_plugin", descriptors)
            residues = list((root / "data" / "plugins").glob(".demo_plugin.removing-*"))
            self.assertEqual(len(residues), 1)

    def test_configured_plugin_with_missing_requires_does_not_block_app_startup(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "plugins" / "broken_plugin"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "broken_plugin",
                        "name": "Broken Plugin",
                        "requires": ["missing_base"],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='broken_plugin', name='Broken Plugin', requires=('missing_base',))\n",
                encoding="utf-8",
            )
            (root / "plugin_state.json").write_text(
                json.dumps({"selected": ["core_ui", "broken_plugin"]}),
                encoding="utf-8",
            )

            app = create_app(AppConfig(data_dir=root))
            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.get("/plugins")

            self.assertEqual(response.status_code, 200)
            self.assertNotIn("broken_plugin", app.state.plugin_registry.plugin_ids)
            rows = {
                row["id"]: row
                for row in app.state.plugin_manager.status_rows(
                    app.state.plugin_registry.plugin_ids
                )
            }
            self.assertTrue(rows["broken_plugin"]["desired_enabled"])
            self.assertFalse(rows["broken_plugin"]["current_enabled"])
            self.assertEqual(
                rows["broken_plugin"]["missing_plugin_dependencies"],
                ["missing_base"],
            )

    def test_stale_removing_directory_is_retried_and_nonmatching_hidden_dirs_are_kept(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_dir = root / "plugins"
            stale = plugin_dir / ".demo_plugin.removing-0123456789abcdef0123456789abcdef"
            stale.mkdir(parents=True)
            (stale / "leftover.bin").write_bytes(b"x")
            unrelated = plugin_dir / ".notes.removing-manual"
            unrelated.mkdir()
            (unrelated / "keep.txt").write_text("keep", encoding="utf-8")

            manager = PluginManager(AppConfig(data_dir=root))
            descriptors = manager.descriptor_map()

            self.assertIn("core_ui", descriptors)
            self.assertFalse(stale.exists())
            self.assertTrue(unrelated.exists())

    def test_failed_stale_removing_cleanup_does_not_block_plugin_scan(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_dir = root / "plugins"
            stale = plugin_dir / ".demo_plugin.removing-fedcba9876543210fedcba9876543210"
            stale.mkdir(parents=True)

            manager = PluginManager(AppConfig(data_dir=root))
            original_rmtree = shutil.rmtree

            def fail_only_stale(path, *args, **kwargs):
                if Path(path) == stale:
                    raise PermissionError("locked")
                return original_rmtree(path, *args, **kwargs)

            with patch("fuckclassroom.plugins.manager.shutil.rmtree", side_effect=fail_only_stale):
                descriptors = manager.descriptor_map()

            self.assertIn("core_ui", descriptors)
            self.assertTrue(stale.exists())

    def test_install_rejects_plugin_that_would_create_dependency_cycle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = root / "plugins" / "plugin_a"
            installed.mkdir(parents=True)
            (installed / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "plugin_a",
                        "name": "Plugin A",
                        "requires": ["plugin_b"],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (installed / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin(): return PluginSpec(id='plugin_a', name='Plugin A', requires=('plugin_b',))\n",
                encoding="utf-8",
            )

            source = root / "candidate"
            source.mkdir()
            (source / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "plugin_b",
                        "name": "Plugin B",
                        "requires": ["plugin_a"],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (source / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin(): return PluginSpec(id='plugin_b', name='Plugin B', requires=('plugin_a',))\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root))

            with self.assertRaisesRegex(PluginManagerError, "依赖存在循环"):
                manager.install_from_path(str(source))

            self.assertFalse((root / "plugins" / "plugin_b").exists())
            self.assertTrue(installed.exists())

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


    def test_plugin_remove_api_deletes_disabled_external_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "plugins" / "remove_demo"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "remove_demo",
                        "name": "Remove Demo",
                        "version": "1.2.3",
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
                "    return PluginSpec(id='remove_demo', name='Remove Demo')\n",
                encoding="utf-8",
            )
            (root / "plugin_state.json").write_text(
                json.dumps({"selected": ["core_ui"]}),
                encoding="utf-8",
            )
            app = create_app(AppConfig(data_dir=root))
            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.delete("/api/plugins/remove_demo")

            self.assertEqual(response.status_code, 200)
            payload = response.json()
            self.assertTrue(payload["ok"])
            self.assertEqual(payload["action"], "remove")
            self.assertEqual(payload["plugin_id"], "remove_demo")
            self.assertEqual(payload["name"], "Remove Demo")
            self.assertEqual(payload["version"], "1.2.3")
            self.assertFalse(plugin_root.exists())

    def test_plugin_remove_api_returns_404_for_missing_plugin(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                response = client.delete("/api/plugins/not_found")

            self.assertEqual(response.status_code, 404)
            self.assertEqual(response.json()["detail"], "插件不存在：not_found")

    def test_plugin_remove_api_rejects_builtin_and_running_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "plugins" / "running_demo"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "running_demo",
                        "name": "Running Demo",
                        "api_version": PLUGIN_API_VERSION,
                        "requires": ["core_ui"],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='running_demo', name='Running Demo', requires=('core_ui',))\n",
                encoding="utf-8",
            )
            (root / "plugin_state.json").write_text(
                json.dumps({"selected": ["core_ui", "running_demo"]}),
                encoding="utf-8",
            )
            app = create_app(AppConfig(data_dir=root))
            from fastapi.testclient import TestClient

            with TestClient(app) as client:
                builtin = client.delete("/api/plugins/core_ui")
                running = client.delete("/api/plugins/running_demo")

            self.assertEqual(builtin.status_code, 409)
            self.assertIn("内置插件不能删除", builtin.json()["detail"])
            self.assertEqual(running.status_code, 409)
            self.assertIn("当前仍在运行", running.json()["detail"])
            self.assertTrue(plugin_root.exists())

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
                        "api_version": PLUGIN_API_VERSION + 1,
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
            self.assertIn("插件 API", detail)

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
            compatibility = manager.compatibility_status(descriptor)
            self.assertTrue(compatibility["compatible"])
            self.assertEqual(compatibility["state"], "compatible")
            self.assertEqual(compatibility["label"], "✓ 兼容")

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
            self.assertEqual(compatibility["state"], "rpc_api_incompatible")
            self.assertEqual(compatibility["label"], "✕ RPC API 不兼容")
            self.assertIn("Plugin RPC API", compatibility["message"])
            with self.assertRaisesRegex(PluginManagerError, "不兼容"):
                manager.enable("future_rpc")

    def test_registry_in_process_manifest_accepts_null_rpc_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin_root = root / "data" / "plugins" / "lab_selection"
            plugin_root.mkdir(parents=True)
            (plugin_root / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "lab_selection",
                        "name": "实验课选课",
                        "api_version": PLUGIN_API_VERSION,
                        "execution": "in_process",
                        "route_prefix": None,
                        "rpc_api_version": None,
                        "rpc_permissions": [],
                        "requires": ["core_ui"],
                        "entry": "plugin.py",
                    }
                ),
                encoding="utf-8",
            )
            (plugin_root / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin():\n"
                "    return PluginSpec(id='lab_selection', name='实验课选课', requires=('core_ui',))\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root / "data"))

            descriptor = manager.descriptor_map()["lab_selection"]

            self.assertEqual(descriptor.execution, "in_process")
            self.assertEqual(descriptor.route_prefix, "")
            self.assertEqual(descriptor.rpc_api_version, PLUGIN_RPC_API_VERSION)
            self.assertEqual(descriptor.rpc_permissions, ())
            self.assertTrue(manager.compatibility_status(descriptor)["compatible"])

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
            self.assertIn("已请求自动应用", unquote(task["result_url"]))

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
