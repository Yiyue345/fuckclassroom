from __future__ import annotations

import base64
import io
import json
import ssl
import tempfile
import threading
import time
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from urllib.error import HTTPError, URLError

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.plugins import PLUGIN_API_VERSION
from fuckclassroom.plugins.manager import PluginManager, PluginManagerError
from fuckclassroom.plugins.registry_client import (
    DEFAULT_PLUGIN_REGISTRY_URL,
    PluginRegistryClient,
    PluginRegistryError,
)
from fuckclassroom.plugins.rpc import PLUGIN_RPC_API_VERSION
from fuckclassroom.web import create_app


class FakeHTTPJSONResponse:
    def __init__(self, url: str, payload: dict[str, object]):
        self.url = url
        self.headers: dict[str, str] = {}
        self.reader = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.reader.close()

    def geturl(self) -> str:
        return self.url

    def read(self, size: int = -1) -> bytes:
        return self.reader.read(size)


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
    def test_registry_tls_handshake_timeout_retries_and_recovers(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            client = PluginRegistryClient(manager, "https://registry.example/api/v1/registry.json")
            timeout = URLError(TimeoutError("_ssl.c:1064: The handshake operation timed out"))
            record = {"schema_version": 1, "index": "index.json"}
            response = FakeHTTPJSONResponse(client.registry_url, record)
            with (
                patch(
                    "fuckclassroom.plugins.registry_client.urlopen",
                    side_effect=[timeout, timeout, response],
                ) as request,
                patch("fuckclassroom.plugins.registry_client.REGISTRY_RETRY_DELAY_SECONDS", 0),
            ):
                self.assertEqual(client._fetch_json(client.registry_url), record)
            self.assertEqual(request.call_count, 3)

    def test_registry_tls_timeout_error_is_readable_and_keeps_original_exception(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = PluginRegistryClient(
                PluginManager(AppConfig(data_dir=Path(directory))),
                "https://registry.example/api/v1/registry.json",
            )
            with (
                patch(
                    "fuckclassroom.plugins.registry_client.urlopen",
                    side_effect=URLError(
                        TimeoutError("_ssl.c:1064: The handshake operation timed out")
                    ),
                ) as request,
                patch("fuckclassroom.plugins.registry_client.REGISTRY_RETRY_DELAY_SECONDS", 0),
            ):
                with self.assertRaises(PluginRegistryError) as caught:
                    client._fetch_json(client.registry_url)

            self.assertEqual(request.call_count, 3)
            self.assertIn("HTTPS 握手未完成", str(caught.exception))
            self.assertIn("已尝试 3 次", str(caught.exception))
            self.assertNotIn("_ssl.c:1064", str(caught.exception))
            self.assertIsInstance(caught.exception.__cause__, URLError)

    def test_registry_certificate_failure_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = PluginRegistryClient(
                PluginManager(AppConfig(data_dir=Path(directory))),
                "https://registry.example/api/v1/registry.json",
            )
            with patch(
                "fuckclassroom.plugins.registry_client.urlopen",
                side_effect=URLError(
                    ssl.SSLCertVerificationError("certificate verify failed")
                ),
            ) as request:
                with self.assertRaisesRegex(PluginRegistryError, "HTTPS 证书验证失败"):
                    client._fetch_json(client.registry_url)
            self.assertEqual(request.call_count, 1)

    def test_registry_http_not_found_is_not_retried(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = PluginRegistryClient(
                PluginManager(AppConfig(data_dir=Path(directory))),
                "https://registry.example/api/v1/registry.json",
            )
            with patch(
                "fuckclassroom.plugins.registry_client.urlopen",
                side_effect=HTTPError(client.registry_url, 404, "not found", {}, None),
            ) as request:
                with self.assertRaisesRegex(PluginRegistryError, "HTTP 404"):
                    client._fetch_json(client.registry_url)
            self.assertEqual(request.call_count, 1)

    def test_registry_metadata_connections_are_bounded_during_parallel_installs(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            client = PluginRegistryClient(
                PluginManager(AppConfig(data_dir=Path(directory))),
                "https://registry.example/api/v1/registry.json",
            )
            release = threading.Event()
            first_two_started = threading.Event()
            lock = threading.Lock()
            active = 0
            peak = 0

            def slow_urlopen(request, *, timeout):
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                    if active == 2:
                        first_two_started.set()
                try:
                    if not release.wait(timeout=3):
                        raise RuntimeError("test timed out awaiting release")
                    return FakeHTTPJSONResponse(
                        client.registry_url,
                        {"schema_version": 1, "index": "index.json"},
                    )
                finally:
                    with lock:
                        active -= 1

            with patch(
                "fuckclassroom.plugins.registry_client.urlopen",
                side_effect=slow_urlopen,
            ):
                with ThreadPoolExecutor(max_workers=4) as pool:
                    futures = [
                        pool.submit(client._fetch_json, client.registry_url)
                        for _ in range(4)
                    ]
                    try:
                        self.assertTrue(first_two_started.wait(timeout=2))
                        self.assertEqual(peak, 2)
                    finally:
                        release.set()
                    for future in futures:
                        self.assertEqual(future.result(timeout=3)["schema_version"], 1)
            self.assertEqual(peak, 2)

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

    def test_registry_page_does_not_block_on_network(self) -> None:
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            with (
                patch.object(
                    PluginRegistryClient,
                    "catalog",
                    side_effect=AssertionError("page must not call the Registry"),
                ),
                TestClient(app) as client,
            ):
                page = client.get("/plugins/registry")

            self.assertEqual(page.status_code, 200)
            self.assertIn("正在加载在线插件", page.text)
            self.assertIn("data-registry-results-url", page.text)

    def test_registry_cards_fragment_handles_unavailable_registry(self) -> None:
        from fastapi.testclient import TestClient

        with tempfile.TemporaryDirectory() as directory:
            app = create_app(AppConfig(data_dir=Path(directory)))
            with TestClient(app) as client:
                with patch.object(PluginRegistryClient, "catalog", return_value=[]):
                    ok = client.get("/plugins/registry/results")
                with patch.object(
                    PluginRegistryClient,
                    "catalog",
                    side_effect=PluginRegistryError("Registry unavailable"),
                ):
                    failed = client.get("/plugins/registry/results")

            self.assertEqual(ok.status_code, 200)
            self.assertIn("Registry 暂无已发布插件", ok.text)
            self.assertEqual(ok.headers.get("cache-control"), "no-store")
            self.assertEqual(failed.status_code, 200)
            self.assertIn("Registry unavailable", failed.text)

    def test_catalog_fetches_plugin_details_concurrently_and_caches_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            responses = registry_responses()
            base = "https://registry.example/api/v1/"
            entries = responses[base + "index.json"]["plugins"]
            original_detail = responses[base + "plugins/demo.json"]
            original_version = responses[base + "plugins/demo/versions/1.5.0.json"]
            for plugin_id in ("extra_a", "extra_b", "extra_c"):
                entries.append(
                    {
                        "id": plugin_id,
                        "name": plugin_id,
                        "summary": plugin_id,
                        "publisher": "example",
                        "official": False,
                        "status": "active",
                        "detail": f"plugins/{plugin_id}.json",
                    }
                )
                responses[base + f"plugins/{plugin_id}.json"] = {
                    **original_detail,
                    "id": plugin_id,
                    "name": plugin_id,
                    "versions": [
                        {
                            **original_detail["versions"][1],
                            "detail": f"plugins/{plugin_id}/versions/1.5.0.json",
                        }
                    ],
                }
                responses[base + f"plugins/{plugin_id}/versions/1.5.0.json"] = {
                    **original_version,
                    "plugin_id": plugin_id,
                }
            client = FakeRegistryClient(manager, responses)
            fetch = client._fetch_json
            calls: list[str] = []
            lock = threading.Lock()
            active = 0
            peak = 0

            def instrumented_fetch(url: str) -> dict[str, object]:
                nonlocal active, peak
                with lock:
                    calls.append(url)
                    active += 1
                    peak = max(peak, active)
                try:
                    # Make overlapping requests observable without relying on
                    # wall-clock timing thresholds for the overall catalog.
                    if "/plugins/" in url:
                        time.sleep(0.04)
                    return fetch(url)
                finally:
                    with lock:
                        active -= 1

            with patch.object(client, "_fetch_json", side_effect=instrumented_fetch):
                rows = client.catalog()
                first_fetches = len(calls)
                cached_rows = client.catalog()

            self.assertEqual(len(rows), 4)
            self.assertEqual([row["id"] for row in rows], [
                "demo", "extra_a", "extra_b", "extra_c",
            ])
            self.assertEqual(cached_rows, rows)
            self.assertGreaterEqual(peak, 2)
            self.assertEqual(len(calls), first_fetches)

    def test_install_plan_refetches_remote_version_after_catalog_cache(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            responses = registry_responses()
            client = FakeRegistryClient(manager, responses)

            self.assertEqual(client.catalog()[0]["target_version"], "1.5.0")
            record = "https://registry.example/api/v1/plugins/demo/versions/2.0.0.json"
            responses[record] = {
                **responses[record],
                "runtime": runtime(),
            }

            plan = client.resolve_install_plan("demo")

            self.assertEqual(plan.target_version, "2.0.0")

    def test_single_unavailable_registry_entry_does_not_hide_healthy_plugins(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            responses = registry_responses()
            base = "https://registry.example/api/v1/"
            entries = responses[base + "index.json"]["plugins"]
            for name in ("broken", "healthy"):
                entries.append({
                    "id": name,
                    "name": name.capitalize(),
                    "summary": "Test",
                    "publisher": "example",
                    "official": False,
                    "status": "active",
                    "detail": f"plugins/{name}.json",
                })
            responses[base + "plugins/healthy.json"] = {
                **responses[base + "plugins/demo.json"],
                "id": "healthy",
                "versions": [{
                    "version": "1.5.0",
                    "channel": "stable",
                    "detail": "plugins/healthy/versions/1.5.0.json",
                }],
            }
            responses[base + "plugins/healthy/versions/1.5.0.json"] = {
                **responses[base + "plugins/demo/versions/1.5.0.json"],
                "plugin_id": "healthy",
            }
            class FailingClient(FakeRegistryClient):
                def _fetch_json(self, url: str) -> dict[str, object]:
                    if url.endswith("/plugins/broken.json"):
                        raise PluginRegistryError("TLS handshake timed out: private diagnostic")
                    return super()._fetch_json(url)

            client = FailingClient(manager, responses)
            rows = client.catalog()
            self.assertEqual([row["id"] for row in rows], ["demo", "broken", "healthy"])
            self.assertEqual(rows[0]["action"], "install")
            self.assertEqual(rows[2]["action"], "install")
            self.assertEqual(rows[1]["action"], "unavailable")
            self.assertEqual(rows[1]["compatibility_label"], "暂不可用")
            self.assertNotIn("private diagnostic", rows[1]["compatibility_message"])

            # Only index/root failures should still fail the entire catalog.
            responses[base + "index.json"]["schema_version"] = 999
            other = FailingClient(manager, responses)
            with self.assertRaises(PluginRegistryError):
                other.catalog()

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

    def test_registry_changelog_link_is_exposed_and_restricted_to_https(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            manager = PluginManager(AppConfig(data_dir=Path(directory)))
            responses = registry_responses()
            record = responses[
                "https://registry.example/api/v1/plugins/demo/versions/1.5.0.json"
            ]
            release_url = "https://github.com/example/demo/releases/tag/v1.5.0"
            record["changelog"] = {"url": release_url}
            client = FakeRegistryClient(manager, responses)
            self.assertEqual(client.catalog()[0]["changelog_url"], release_url)
            for value in (
                "javascript:alert(1)",
                "http://github.com/example/demo/releases",
                "https://user:password@github.com/example/demo",
                "https://github.com/evil\\redirect",
                "https://github.com/example\nredirect",
            ):
                with self.subTest(value=value):
                    record["changelog"] = {"url": value}
                    self.assertEqual(
                        FakeRegistryClient(manager, responses).catalog()[0]["changelog_url"], ""
                    )
            record.pop("changelog", None)
            self.assertEqual(FakeRegistryClient(manager, responses).catalog()[0]["changelog_url"], "")

    def test_active_plugin_updates_are_available_without_manual_disable(self) -> None:
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
            self.assertFalse(rows[0]["update_blocked"])

    def test_active_registry_update_uses_runtime_transaction_without_disabling(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            plugin = root / "plugins" / "demo"
            plugin.mkdir(parents=True)
            base_manifest = {
                "id": "demo",
                "name": "Demo",
                "version": "1.0.0",
                "api_version": PLUGIN_API_VERSION,
                "app_requires": ">=99",
                "execution": "in_process",
                "entry": "plugin.py",
                "requires": [],
                "python_requires": [],
            }
            (plugin / "plugin.json").write_text(
                json.dumps(base_manifest), encoding="utf-8"
            )
            (plugin / "plugin.py").write_text("# old", encoding="utf-8")
            manager = PluginManager(AppConfig(data_dir=root))
            manager._save_selected({"core_ui", "demo"})
            responses = registry_responses()
            client = FakeRegistryClient(manager, responses)
            archive = root / "demo-1.5.0.zip"
            with zipfile.ZipFile(archive, "w") as package:
                package.writestr(
                    "plugin.json",
                    json.dumps({**base_manifest, "version": "1.5.0"}),
                )
                package.writestr("plugin.py", "# new")
            with self.assertRaisesRegex(PluginManagerError, "安全 Runtime 切换"):
                manager.install_from_path(str(archive), replace=True)

            calls = []

            def apply_update(plugin_id, installer):
                self.assertEqual(plugin_id, "demo")
                self.assertIn("demo", manager.desired_enabled())
                self.assertEqual(
                    manager.descriptor_map()["demo"].version, "1.0.0"
                )
                calls.append(plugin_id)
                return installer()

            with patch.object(client, "_download_artifact", return_value=archive):
                descriptor, action, signed = client.install(
                    "demo",
                    current_enabled=("demo",),
                    update_transaction=apply_update,
                )
            self.assertEqual(calls, ["demo"])
            self.assertEqual(action, "update")
            self.assertFalse(signed)
            self.assertEqual(descriptor.version, "1.5.0")
            self.assertEqual(manager.descriptor_map()["demo"].version, "1.5.0")
            self.assertIn("demo", manager.desired_enabled())
            self.assertEqual(
                (plugin / "plugin.py").read_text(encoding="utf-8"), "# new"
            )

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
