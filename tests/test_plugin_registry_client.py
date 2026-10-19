from __future__ import annotations

import base64
import json
import tempfile
import unittest
import zipfile
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.plugins import PLUGIN_API_VERSION
from fuckclassroom.plugins.manager import PluginManager, PluginManagerError
from fuckclassroom.plugins.registry_client import (
    DEFAULT_PLUGIN_REGISTRY_URL,
    PluginRegistryClient,
)
from fuckclassroom.plugins.rpc import PLUGIN_RPC_API_VERSION


class FakeRegistryClient(PluginRegistryClient):
    def __init__(self, manager: PluginManager, responses: dict[str, dict[str, object]]):
        super().__init__(
            manager,
            "https://registry.example/api/v1/registry.json",
        )
        self.responses = responses

    def _fetch_json(self, url: str) -> dict[str, object]:
        try:
            return self.responses[url]
        except KeyError as exc:
            raise AssertionError(f"Unexpected Registry request: {url}") from exc


def runtime(
    *,
    api_version: int = PLUGIN_API_VERSION,
    app_requires: str = ">=99",
    requires: list[str] | None = None,
):
    return {
        "api_version": api_version,
        "app_requires": app_requires,
        "execution": "in_process",
        "entry": "plugin.py",
        "requires": list(requires or []),
        "python_requires": [],
        "route_prefix": None,
        "rpc_api_version": None,
        "rpc_permissions": [],
    }


def artifact(version: str):
    return {
        "filename": f"demo-{version}.zip",
        "url": f"https://github.com/example/demo/releases/download/v{version}/demo-{version}.zip",
        "size": 123,
        "sha256": "a" * 64,
    }


def registry_responses() -> dict[str, dict[str, object]]:
    base = "https://registry.example/api/v1/"
    return {
        base + "registry.json": {
            "schema_version": 1,
            "index": "index.json",
            "publishers": "publishers/",
        },
        base + "index.json": {
            "schema_version": 1,
            "plugins": [
                {
                    "id": "demo",
                    "name": "Demo",
                    "summary": "Demo plugin",
                    "publisher": "example",
                    "official": False,
                    "status": "active",
                    "detail": "plugins/demo.json",
                }
            ],
        },
        base + "plugins/demo.json": {
            "schema_version": 1,
            "id": "demo",
            "name": "Demo",
            "summary": "Demo plugin",
            "publisher": "example",
            "official": False,
            "status": "active",
            "versions": [
                {
                    "version": "2.0.0",
                    "channel": "stable",
                    "released_at": "2026-09-28T00:00:00Z",
                    "yanked": False,
                    "detail": "plugins/demo/versions/2.0.0.json",
                },
                {
                    "version": "1.5.0",
                    "channel": "stable",
                    "released_at": "2026-09-27T00:00:00Z",
                    "yanked": False,
                    "detail": "plugins/demo/versions/1.5.0.json",
                },
            ],
        },
        base + "plugins/demo/versions/2.0.0.json": {
            "schema_version": 1,
            "plugin_id": "demo",
            "version": "2.0.0",
            "channel": "stable",
            "released_at": "2026-09-28T00:00:00Z",
            "yanked": False,
            "runtime": runtime(api_version=PLUGIN_API_VERSION + 1),
            "artifact": artifact("2.0.0"),
        },
        base + "plugins/demo/versions/1.5.0.json": {
            "schema_version": 1,
            "plugin_id": "demo",
            "version": "1.5.0",
            "channel": "stable",
            "released_at": "2026-09-27T00:00:00Z",
            "yanked": False,
            "runtime": runtime(),
            "artifact": artifact("1.5.0"),
        },
    }


class PluginRegistryClientTests(unittest.TestCase):
    def test_default_registry_url_points_to_custom_domain(self) -> None:
        self.assertEqual(
            DEFAULT_PLUGIN_REGISTRY_URL,
            "https://classroom.yiyuemeow.com/api/v1/registry.json",
        )
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            client = PluginRegistryClient(manager)
            self.assertEqual(client.registry_url, DEFAULT_PLUGIN_REGISTRY_URL)
            self.assertEqual(
                client.api_base,
                "https://classroom.yiyuemeow.com/api/v1/",
            )

    def test_catalog_selects_highest_api_compatible_version(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            client = FakeRegistryClient(manager, registry_responses())

            rows = client.catalog()

            self.assertEqual(len(rows), 1)
            row = rows[0]
            self.assertEqual(row["target_version"], "1.5.0")
            self.assertEqual(row["action"], "install")
            self.assertEqual(row["compatibility_state"], "app_version_mismatch")
            self.assertEqual(row["compatibility_label"], "⚠ 主程序版本不满足")


    def test_beta_only_release_requires_beta_channel(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            responses = registry_responses()
            base = "https://registry.example/api/v1/"
            beta_version = "0.1.0-beta.2"
            responses[base + "plugins/demo.json"]["versions"] = [
                {
                    "version": beta_version,
                    "channel": "beta",
                    "released_at": "2026-10-02T00:00:00Z",
                    "yanked": False,
                    "detail": f"plugins/demo/versions/{beta_version}.json",
                }
            ]
            responses[base + f"plugins/demo/versions/{beta_version}.json"] = {
                "schema_version": 1,
                "plugin_id": "demo",
                "version": beta_version,
                "channel": "beta",
                "released_at": "2026-10-02T00:00:00Z",
                "yanked": False,
                "runtime": runtime(app_requires=">=0"),
                "artifact": artifact(beta_version),
            }
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            client = FakeRegistryClient(manager, responses)

            stable = client.catalog()[0]
            self.assertEqual(stable["compatibility_state"], "channel_unavailable")
            self.assertEqual(stable["action"], "unavailable")
            self.assertEqual(stable["target_version"], "")

            beta = client.catalog(channels=("stable", "beta"))[0]
            self.assertEqual(beta["compatibility_state"], "compatible")
            self.assertEqual(beta["action"], "install")
            self.assertEqual(beta["target_version"], beta_version)

            with patch.object(
                client, "_download_artifact",
                return_value=Path(directory) / "demo.zip",
            ), patch.object(
                manager, "install_from_path",
                return_value=SimpleNamespace(name="Demo", version=beta_version),
            ) as installer:
                client.install("demo", channels=("stable", "beta"))

            self.assertEqual(installer.call_args.kwargs["expected_version"], beta_version)

    def test_beta_toggle_prefers_newest_compatible_release(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            responses = registry_responses()
            base = "https://registry.example/api/v1/"
            beta_version = "2.5.0-beta.1"
            responses[base + "plugins/demo.json"]["versions"].insert(
                0,
                {
                    "version": beta_version,
                    "channel": "beta",
                    "released_at": "2026-10-02T00:00:00Z",
                    "yanked": False,
                    "detail": f"plugins/demo/versions/{beta_version}.json",
                },
            )
            responses[base + f"plugins/demo/versions/{beta_version}.json"] = {
                "schema_version": 1,
                "plugin_id": "demo",
                "version": beta_version,
                "channel": "beta",
                "released_at": "2026-10-02T00:00:00Z",
                "yanked": False,
                "runtime": runtime(app_requires=">=0"),
                "artifact": artifact(beta_version),
            }
            client = FakeRegistryClient(
                PluginManager(AppConfig(data_dir=Path(directory))), responses
            )

            self.assertEqual(client.catalog()[0]["target_version"], "1.5.0")
            self.assertEqual(
                client.catalog(channels=("stable", "beta"))[0]["target_version"],
                beta_version,
            )

    def test_catalog_reports_missing_plugin_dependencies_before_install(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            responses = registry_responses()
            key = "https://registry.example/api/v1/plugins/demo/versions/1.5.0.json"
            responses[key] = {
                **responses[key],
                "runtime": runtime(requires=["base_plugin"]),
            }
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            client = FakeRegistryClient(manager, responses)

            rows = client.catalog()

            self.assertEqual(rows[0]["required_plugins"], ["base_plugin"])
            self.assertEqual(rows[0]["missing_plugin_dependencies"], ["base_plugin"])
            self.assertEqual(rows[0]["action"], "install")

    def test_update_is_blocked_while_installed_plugin_is_active(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugins" / "demo"
            plugin.mkdir(parents=True)
            (plugin / "plugin.json").write_text(
                json.dumps(
                    {
                        "id": "demo",
                        "name": "Demo",
                        "version": "1.0.0",
                        "api_version": PLUGIN_API_VERSION,
                        "app_requires": ">=0",
                        "execution": "in_process",
                        "entry": "plugin.py",
                        "requires": [],
                        "python_requires": [],
                    }
                ),
                encoding="utf-8",
            )
            (plugin / "plugin.py").write_text(
                "from fuckclassroom.core.plugins import PluginSpec\n"
                "def build_plugin(): return PluginSpec(id='demo', name='Demo')\n",
                encoding="utf-8",
            )
            manager = PluginManager(AppConfig(data_dir=root))
            client = FakeRegistryClient(manager, registry_responses())

            rows = client.catalog(current_enabled={"demo"})

            self.assertEqual(rows[0]["action"], "update")
            self.assertTrue(rows[0]["update_blocked"])

    def test_ed25519_signature_verification_uses_registry_domain_payload(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        public_key = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        sha256 = "b" * 64
        payload = f"fuckclassroom-plugin-artifact-sha256-v1:{sha256}".encode("utf-8")
        signature = private_key.sign(payload)

        PluginRegistryClient.verify_artifact_signature(
            sha256,
            {
                "scheme": "ed25519-sha256-v1",
                "key_id": "release-2026",
                "value": base64.b64encode(signature).decode("ascii"),
            },
            {
                "id": "example",
                "signing_keys": [
                    {
                        "id": "release-2026",
                        "algorithm": "ed25519",
                        "public_key": base64.b64encode(public_key).decode("ascii"),
                    }
                ],
            },
        )

    def test_atomic_registry_update_rejects_runtime_mismatch_before_replacing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugins" / "demo"
            plugin.mkdir(parents=True)
            old_manifest = {
                "id": "demo",
                "name": "Demo",
                "version": "1.0.0",
                "api_version": PLUGIN_API_VERSION,
                "app_requires": ">=0",
                "execution": "in_process",
                "entry": "plugin.py",
                "requires": [],
                "python_requires": [],
            }
            (plugin / "plugin.json").write_text(json.dumps(old_manifest), encoding="utf-8")
            (plugin / "plugin.py").write_text("# old\n", encoding="utf-8")

            archive = root / "demo-2.0.0.zip"
            new_manifest = {
                **old_manifest,
                "version": "2.0.0",
                "route_prefix": None,
                "rpc_api_version": None,
                "rpc_permissions": [],
            }
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr("plugin.json", json.dumps(new_manifest))
                package.writestr("plugin.py", "# new\n")

            manager = PluginManager(AppConfig(data_dir=root))
            expected_runtime = {
                "api_version": PLUGIN_API_VERSION + 1,
                "app_requires": ">=0",
                "execution": "in_process",
                "entry": "plugin.py",
                "requires": [],
                "python_requires": [],
                "route_prefix": None,
                "rpc_api_version": None,
                "rpc_permissions": [],
            }
            with self.assertRaisesRegex(PluginManagerError, "Registry"):
                manager.install_from_path(
                    str(archive),
                    replace=True,
                    expected_id="demo",
                    expected_version="2.0.0",
                    expected_runtime=expected_runtime,
                )
            self.assertEqual(manager.descriptor_map()["demo"].version, "1.0.0")

            expected_runtime["api_version"] = PLUGIN_API_VERSION
            descriptor = manager.install_from_path(
                str(archive),
                replace=True,
                expected_id="demo",
                expected_version="2.0.0",
                expected_runtime=expected_runtime,
            )
            self.assertEqual(descriptor.version, "2.0.0")
            self.assertEqual((root / "plugins" / "demo" / "plugin.py").read_text(), "# new\n")


if __name__ == "__main__":
    unittest.main()
