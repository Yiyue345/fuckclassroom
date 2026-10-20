"""Tests run without installing FuckClassroom: validate the publishing contract."""

import hashlib
import importlib.util
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace

from scripts.build_registry_artifact import ROOT, build


class PackageTests(unittest.TestCase):
    def test_manifest_matches_plugin_entry(self):
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        entry_source = (ROOT / manifest["entry"]).read_text(encoding="utf-8")
        self.assertRegex(manifest["id"], r"^[a-z][a-z0-9_-]{1,63}$")
        self.assertEqual(manifest["api_version"], 1)
        self.assertEqual(manifest["execution"], "in_process")
        self.assertEqual(manifest["requires"], ["core_ui"])
        self.assertIn(f'id="{manifest["id"]}"', entry_source)
        self.assertIn('requires=("core_ui",)', entry_source)
        self.assertIn("def build_plugin()", entry_source)

    def test_beta_package_has_root_manifest_and_checksum(self):
        with tempfile.TemporaryDirectory() as temp:
            zip_path, checksum_path = build("0.1.0-beta.1", output_dir=Path(temp))
            plugin_id = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))["id"]
            self.assertEqual(zip_path.name, f"{plugin_id}-0.1.0-beta.1.zip")
            self.assertEqual(checksum_path.name, zip_path.name + ".sha256")
            expected_hash = hashlib.sha256(zip_path.read_bytes()).hexdigest()
            self.assertEqual(checksum_path.read_text().strip(), f"{expected_hash}  {zip_path.name}")
            with zipfile.ZipFile(zip_path) as archive:
                self.assertEqual(set(archive.namelist()), {"plugin.json", "plugin.py"})
                manifest = json.loads(archive.read("plugin.json"))
                self.assertEqual(manifest["id"], plugin_id)
                self.assertEqual(manifest["version"], "0.1.0-beta.1")
                self.assertEqual(manifest["entry"], "plugin.py")

    def test_actual_plugin_spec_when_host_is_available(self):
        # Standalone publishing checks do not require the host to be installed.
        try:
            from fastapi import APIRouter
            from fuckclassroom.core.plugins import PluginSpec
        except ImportError:
            self.skipTest("FuckClassroom/FastAPI not installed in template-only environment")
        module_spec = importlib.util.spec_from_file_location(
            "sample_plugin_template_runtime", ROOT / "plugin.py"
        )
        self.assertIsNotNone(module_spec)
        self.assertIsNotNone(module_spec.loader)
        module = importlib.util.module_from_spec(module_spec)
        module_spec.loader.exec_module(module)
        plugin = module.build_plugin()
        manifest = json.loads((ROOT / "plugin.json").read_text(encoding="utf-8"))
        self.assertIsInstance(plugin, PluginSpec)
        self.assertEqual(plugin.id, manifest["id"])
        self.assertEqual(plugin.api_version, manifest["api_version"])
        self.assertEqual(list(plugin.requires), manifest["requires"])
        self.assertIsNotNone(plugin.route_factory)
        router = plugin.route_factory(SimpleNamespace(plugin_id=manifest["id"]))
        self.assertIsInstance(router, APIRouter)
        routes = [route for route in router.routes if route.path == "/sample-plugin/hello"]
        self.assertEqual(len(routes), 1)
        self.assertEqual(routes[0].endpoint()["plugin_id"], manifest["id"])

    def test_invalid_versions_fail_closed(self):
        with tempfile.TemporaryDirectory() as temp:
            for version in ("0.2.0", "v0.1.0", "../evil", "0.1"):
                with self.subTest(version=version):
                    with self.assertRaises(ValueError):
                        build(version, output_dir=Path(temp))


if __name__ == "__main__":
    unittest.main()
