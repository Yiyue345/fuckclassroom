from __future__ import annotations

import base64
import hashlib
import logging
import json
import os
import ssl
import re
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .. import __version__ as APP_VERSION
from ..core.plugins import PLUGIN_API_VERSION
from .manager import PluginDescriptor, PluginManager, PluginManagerError
from .rpc import PLUGIN_RPC_API_VERSION


DEFAULT_PLUGIN_REGISTRY_URL = (
    "https://classroom.yiyuemeow.com/api/v1/registry.json"
)
MAX_REGISTRY_JSON_BYTES = 2 * 1024 * 1024
CATALOG_CACHE_SECONDS = 60
CATALOG_MAX_WORKERS = 8
MAX_PARALLEL_PLUGIN_DOWNLOADS = 4
MAX_PARALLEL_REGISTRY_REQUESTS = 2
REGISTRY_FETCH_ATTEMPTS = 3
REGISTRY_RETRY_DELAY_SECONDS = 0.5
REGISTRY_REQUEST_TIMEOUT_SECONDS = 10
MAX_ARTIFACT_BYTES = 100 * 1024 * 1024
_ARTIFACT_PATH_RE = re.compile(
    r"^/[^/]+/[^/]+/releases/download/[^/]+/[^/]+\.zip$"
)
_ARTIFACT_REDIRECT_HOSTS = frozenset(
    {
        "github.com",
        "release-assets.githubusercontent.com",
        "objects.githubusercontent.com",
    }
)
_SIGNATURE_SCHEME = "ed25519-sha256-v1"

logger = logging.getLogger(__name__)


class PluginRegistryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RegistryInstallPlan:
    plugin_id: str
    name: str
    summary: str
    publisher: str
    official: bool
    status: str
    installed_version: str
    target_version: str
    action: str
    update_blocked: bool
    compatibility_state: str
    compatibility_label: str
    compatibility_message: str
    compatibility_severity: str
    app_version_satisfied: bool
    channel: str
    released_at: str
    signed: bool
    required_plugins: tuple[str, ...]
    missing_plugin_dependencies: tuple[str, ...]
    version_record: Mapping[str, object] | None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "id": self.plugin_id,
            "name": self.name,
            "summary": self.summary,
            "publisher": self.publisher,
            "official": self.official,
            "status": self.status,
            "installed_version": self.installed_version,
            "target_version": self.target_version,
            "action": self.action,
            "update_blocked": self.update_blocked,
            "compatibility_state": self.compatibility_state,
            "compatibility_label": self.compatibility_label,
            "compatibility_message": self.compatibility_message,
            "compatibility_severity": self.compatibility_severity,
            "app_version_satisfied": self.app_version_satisfied,
            "channel": self.channel,
            "released_at": self.released_at,
            "signed": self.signed,
            "required_plugins": list(self.required_plugins),
            "missing_plugin_dependencies": list(self.missing_plugin_dependencies),
        }


class PluginRegistryClient:
    def __init__(
        self,
        manager: PluginManager,
        registry_url: str | None = None,
    ) -> None:
        self.manager = manager
        self.registry_url = (
            registry_url
            or os.getenv("FUCKCLASSROOM_PLUGIN_REGISTRY_URL")
            or DEFAULT_PLUGIN_REGISTRY_URL
        ).strip()
        self._validate_transport(self.registry_url)
        self.api_base = urljoin(self.registry_url, "./")
        self._catalog_cache: dict[str, tuple[float, dict[str, object]]] = {}
        self._catalog_cache_lock = threading.Lock()
        self._download_slots = threading.BoundedSemaphore(MAX_PARALLEL_PLUGIN_DOWNLOADS)
        self._registry_request_slots = threading.BoundedSemaphore(MAX_PARALLEL_REGISTRY_REQUESTS)

    def catalog(
        self,
        *,
        current_enabled: tuple[str, ...] | list[str] | set[str] = (),
        channels: tuple[str, ...] = ("stable",),
    ) -> list[dict[str, object]]:
        index = self._index(fetch_json=self._cached_catalog_json)
        raw_plugins = index.get("plugins", [])
        if not isinstance(raw_plugins, list):
            raise PluginRegistryError("Registry index.plugins 格式无效")

        descriptors = self.manager.descriptor_map()
        desired = self.manager.desired_enabled()
        current = set(current_enabled)
        entries = [
            entry
            for entry in raw_plugins
            if isinstance(entry, Mapping) and str(entry.get("id") or "")
        ]
        if not entries:
            return []

        def build_row(entry: Mapping[str, object]) -> dict[str, object]:
            plan = self._plan_from_entry(
                entry,
                descriptors=descriptors,
                desired=desired,
                current=current,
                channels=channels,
                fetch_json=self._cached_catalog_json,
            )
            return plan.to_public_dict()

        # Network-bound per-plugin lookups should not block one another.
        # executor.map retains the Registry index's display order.
        with ThreadPoolExecutor(max_workers=min(CATALOG_MAX_WORKERS, len(entries))) as pool:
            return list(pool.map(build_row, entries))

    def resolve_install_plan(
        self,
        plugin_id: str,
        *,
        current_enabled: tuple[str, ...] | list[str] | set[str] = (),
        channels: tuple[str, ...] = ("stable",),
        progress=None,
    ) -> RegistryInstallPlan:
        fetch = (
            (lambda url: self._fetch_json(url, progress=progress))
            if progress is not None
            else self._fetch_json
        )
        index = self._index(fetch_json=fetch)
        raw_plugins = index.get("plugins", [])
        if not isinstance(raw_plugins, list):
            raise PluginRegistryError("Registry index.plugins 格式无效")
        entry = next(
            (
                item
                for item in raw_plugins
                if isinstance(item, Mapping) and str(item.get("id") or "") == plugin_id
            ),
            None,
        )
        if entry is None:
            raise PluginRegistryError(f"Registry 中不存在插件：{plugin_id}")
        return self._plan_from_entry(
            entry,
            descriptors=self.manager.descriptor_map(),
            desired=self.manager.desired_enabled(),
            current=set(current_enabled),
            channels=channels,
            fetch_json=fetch,
        )

    def install(
        self,
        plugin_id: str,
        *,
        current_enabled: tuple[str, ...] | list[str] | set[str] = (),
        channels: tuple[str, ...] = ("stable",),
        progress=None,
    ) -> tuple[PluginDescriptor, str, bool]:
        plan = self.resolve_install_plan(
            plugin_id,
            current_enabled=current_enabled,
            channels=channels,
            progress=progress,
        )
        if plan.action not in {"install", "update"} or plan.version_record is None:
            if plan.action == "installed":
                raise PluginRegistryError(f"插件 {plan.name} 已是当前可安装的最新版本")
            raise PluginRegistryError(
                plan.compatibility_message or f"插件 {plan.name} 没有可安装版本"
            )
        if plan.update_blocked:
            raise PluginRegistryError("更新插件前请先停用该插件及依赖它的插件")

        record = plan.version_record
        runtime = record.get("runtime")
        artifact = record.get("artifact")
        if not isinstance(runtime, Mapping) or not isinstance(artifact, Mapping):
            raise PluginRegistryError("Registry 版本记录缺少 runtime/artifact")

        self._report(progress, 5, f"正在准备下载 {plan.name} v{plan.target_version}")
        # Only bound the network transfer. Different plugins can resolve
        # versions, download and finish their own installation concurrently.
        self._report(progress, 8, "正在等待插件下载槽位")
        while not self._download_slots.acquire(timeout=0.2):
            self._check_cancelled(progress)
        try:
            self._check_cancelled(progress)
            zip_path = self._download_artifact(
                plugin_id=plan.plugin_id,
                version=plan.target_version,
                publisher=plan.publisher,
                artifact=artifact,
                progress=progress,
            )
        finally:
            self._download_slots.release()
        try:
            self._report(progress, 82, "正在验证插件包声明")
            descriptor = self.manager.install_from_path(
                str(zip_path),
                replace=plan.action == "update",
                expected_id=plan.plugin_id,
                expected_version=plan.target_version,
                expected_runtime=runtime,
            )
        finally:
            zip_path.unlink(missing_ok=True)

        self._report(
            progress,
            100,
            (
                f"插件 {descriptor.name} 已更新到 v{descriptor.version}"
                if plan.action == "update"
                else f"插件 {descriptor.name} v{descriptor.version} 已下载并安装"
            ),
        )
        return descriptor, plan.action, plan.signed

    def _plan_from_entry(
        self,
        entry: Mapping[str, object],
        *,
        descriptors: Mapping[str, PluginDescriptor],
        desired: set[str],
        current: set[str],
        channels: tuple[str, ...],
        fetch_json: Callable[[str], dict[str, object]] | None = None,
    ) -> RegistryInstallPlan:
        fetch = fetch_json or self._fetch_json
        plugin_id = str(entry.get("id") or "")
        detail_ref = str(entry.get("detail") or "")
        if not detail_ref:
            raise PluginRegistryError(f"插件 {plugin_id} 缺少 detail 链接")
        detail = fetch(self._resolve_api(detail_ref))
        if str(detail.get("id") or "") != plugin_id:
            raise PluginRegistryError(f"Registry 插件详情 ID 不匹配：{plugin_id}")

        chosen, rejected = self._select_version(
            detail, channels=channels, fetch_json=fetch
        )
        compatibility = (
            self._runtime_compatibility(chosen.get("runtime", {}))
            if chosen is not None
            else rejected
            or {
                "compatible": False,
                "state": "unavailable",
                "label": "✕ 无兼容版本",
                "severity": "error",
                "message": "没有符合当前 Plugin API / RPC API 的可安装版本",
                "app_version_satisfied": True,
            }
        )

        installed = descriptors.get(plugin_id)
        installed_version = ""
        action = "unavailable"
        update_blocked = False
        target_version = str(chosen.get("version") or "") if chosen else ""

        if installed is not None and installed.builtin:
            action = "blocked"
            compatibility = {
                **compatibility,
                "compatible": False,
                "state": "builtin_conflict",
                "label": "✕ 与内置插件冲突",
                "severity": "error",
                "message": "Registry 插件 ID 与内置插件相同，不能覆盖内置插件",
            }
        elif installed is not None:
            installed_version = installed.version
            if chosen is None:
                action = "unavailable"
            else:
                comparison = self._compare_versions(target_version, installed_version)
                if comparison > 0:
                    action = "update"
                    update_blocked = plugin_id in desired or plugin_id in current
                else:
                    action = "installed"
        elif chosen is not None:
            action = "install"

        artifact = chosen.get("artifact", {}) if chosen else {}
        runtime = chosen.get("runtime", {}) if chosen else {}
        raw_requires = runtime.get("requires", []) if isinstance(runtime, Mapping) else []
        required_plugins = (
            tuple(str(item) for item in raw_requires)
            if isinstance(raw_requires, list)
            else ()
        )
        missing_plugin_dependencies = tuple(
            dependency
            for dependency in required_plugins
            if dependency not in descriptors
        )
        return RegistryInstallPlan(
            plugin_id=plugin_id,
            name=str(detail.get("name") or entry.get("name") or plugin_id),
            summary=str(detail.get("summary") or entry.get("summary") or ""),
            publisher=str(detail.get("publisher") or entry.get("publisher") or ""),
            official=bool(detail.get("official", entry.get("official", False))),
            status=str(detail.get("status") or entry.get("status") or "active"),
            installed_version=installed_version,
            target_version=target_version,
            action=action,
            update_blocked=update_blocked,
            compatibility_state=str(compatibility.get("state") or "unavailable"),
            compatibility_label=str(compatibility.get("label") or "✕ 无兼容版本"),
            compatibility_message=str(compatibility.get("message") or ""),
            compatibility_severity=str(compatibility.get("severity") or "error"),
            app_version_satisfied=bool(
                compatibility.get("app_version_satisfied", True)
            ),
            channel=str(chosen.get("channel") or "") if chosen else "",
            released_at=str(chosen.get("released_at") or "") if chosen else "",
            signed=isinstance(artifact, Mapping) and isinstance(artifact.get("signature"), Mapping),
            required_plugins=required_plugins,
            missing_plugin_dependencies=missing_plugin_dependencies,
            version_record=chosen,
        )

    def _select_version(
        self,
        plugin_detail: Mapping[str, object],
        *,
        channels: tuple[str, ...],
        fetch_json: Callable[[str], dict[str, object]] | None = None,
    ) -> tuple[dict[str, object] | None, dict[str, object] | None]:
        fetch = fetch_json or self._fetch_json
        raw_versions = plugin_detail.get("versions", [])
        if not isinstance(raw_versions, list):
            raise PluginRegistryError("Registry plugin.versions 格式无效")

        candidates: list[tuple[Version, Mapping[str, object]]] = []
        for summary in raw_versions:
            if not isinstance(summary, Mapping):
                continue
            if bool(summary.get("yanked")):
                continue
            if str(summary.get("channel") or "") not in channels:
                continue
            try:
                parsed = Version(str(summary.get("version") or ""))
            except InvalidVersion:
                continue
            candidates.append((parsed, summary))
        candidates.sort(key=lambda item: item[0], reverse=True)
        if not candidates:
            return None, {
                "compatible": False,
                "state": "channel_unavailable",
                "label": "— 当前频道无版本",
                "severity": "warning",
                "message": "当前频道没有可安装版本；可尝试开启「包含测试版」",
                "app_version_satisfied": True,
            }

        rejected: dict[str, object] | None = None
        plugin_id = str(plugin_detail.get("id") or "")
        for _, summary in candidates:
            detail_ref = str(summary.get("detail") or "")
            if not detail_ref:
                continue
            record = fetch(self._resolve_api(detail_ref))
            if str(record.get("plugin_id") or "") != plugin_id:
                raise PluginRegistryError(
                    f"Registry 版本记录 plugin_id 不匹配：{plugin_id}"
                )
            if bool(record.get("yanked")):
                continue
            compatibility = self._runtime_compatibility(record.get("runtime", {}))
            if compatibility["compatible"]:
                return record, compatibility
            if rejected is None:
                rejected = compatibility
        return None, rejected

    @staticmethod
    def _runtime_compatibility(runtime: object) -> dict[str, object]:
        if not isinstance(runtime, Mapping):
            return {
                "compatible": False,
                "state": "invalid_runtime",
                "label": "✕ Registry 运行时声明无效",
                "severity": "error",
                "message": "版本记录缺少有效 runtime",
                "app_version_satisfied": True,
            }
        try:
            api_version = int(runtime.get("api_version"))
        except (TypeError, ValueError):
            api_version = -1
        execution = str(runtime.get("execution") or "in_process")
        rpc_value = runtime.get("rpc_api_version")
        try:
            rpc_api_version = int(rpc_value) if rpc_value is not None else None
        except (TypeError, ValueError):
            rpc_api_version = -1

        if api_version != PLUGIN_API_VERSION:
            return {
                "compatible": False,
                "state": "plugin_api_incompatible",
                "label": "✕ Plugin API 不兼容",
                "severity": "error",
                "message": (
                    f"插件 API {api_version}，当前支持 API {PLUGIN_API_VERSION}"
                ),
                "app_version_satisfied": True,
            }
        if execution == "process" and rpc_api_version != PLUGIN_RPC_API_VERSION:
            return {
                "compatible": False,
                "state": "rpc_api_incompatible",
                "label": "✕ RPC API 不兼容",
                "severity": "error",
                "message": (
                    f"Plugin RPC API {rpc_api_version}，"
                    f"当前支持 RPC API {PLUGIN_RPC_API_VERSION}"
                ),
                "app_version_satisfied": True,
            }

        app_requires = str(runtime.get("app_requires") or "")
        if app_requires:
            try:
                spec = SpecifierSet(app_requires)
                current = Version(APP_VERSION)
                satisfied = spec.contains(current, prereleases=current.is_prerelease)
            except (InvalidSpecifier, InvalidVersion):
                satisfied = False
            if not satisfied:
                return {
                    "compatible": True,
                    "state": "app_version_mismatch",
                    "label": "⚠ 主程序版本不满足",
                    "severity": "warning",
                    "message": (
                        f"需要 FuckClassroom {app_requires}，当前为 {APP_VERSION}"
                    ),
                    "app_version_satisfied": False,
                }

        return {
            "compatible": True,
            "state": "compatible",
            "label": "✓ 兼容",
            "severity": "success",
            "message": "Plugin API、RPC API 与主程序版本声明均兼容",
            "app_version_satisfied": True,
        }

    def _cached_catalog_json(self, url: str) -> dict[str, object]:
        now = time.monotonic()
        with self._catalog_cache_lock:
            cached = self._catalog_cache.get(url)
            if cached is not None and cached[0] > now:
                return cached[1]
        record = self._fetch_json(url)
        with self._catalog_cache_lock:
            self._catalog_cache[url] = (
                time.monotonic() + CATALOG_CACHE_SECONDS,
                record,
            )
        return record

    def _index(
        self,
        *,
        fetch_json: Callable[[str], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        fetch = fetch_json or self._fetch_json
        root = self._root(fetch_json=fetch)
        index_ref = str(root.get("index") or "index.json")
        index = fetch(self._resolve_api(index_ref))
        if int(index.get("schema_version") or 0) != 1:
            raise PluginRegistryError("不支持的 Registry index schema_version")
        return index

    def _root(
        self,
        *,
        fetch_json: Callable[[str], dict[str, object]] | None = None,
    ) -> dict[str, object]:
        # A fresh install/verification must not silently reuse browsing cache.
        fetch = fetch_json or self._fetch_json
        root = fetch(self.registry_url)
        if int(root.get("schema_version") or 0) != 1:
            raise PluginRegistryError("不支持的 Registry schema_version")
        return root

    def _publisher(self, publisher_id: str) -> dict[str, object]:
        root = self._root()
        prefix = str(root.get("publishers") or "publishers/").rstrip("/") + "/"
        record = self._fetch_json(
            self._resolve_api(f"{prefix}{publisher_id}.json")
        )
        if str(record.get("id") or "") != publisher_id:
            raise PluginRegistryError("Publisher ID 与 Registry 记录不一致")
        return record

    def _download_artifact(
        self,
        *,
        plugin_id: str,
        version: str,
        publisher: str,
        artifact: Mapping[str, object],
        progress=None,
    ) -> Path:
        filename = str(artifact.get("filename") or "")
        expected_filename = f"{plugin_id}-{version}.zip"
        if filename != expected_filename:
            raise PluginRegistryError(
                f"Artifact 文件名不匹配：期望 {expected_filename}，实际 {filename}"
            )
        url = str(artifact.get("url") or "")
        self._validate_artifact_url(url, expected_filename)
        try:
            expected_size = int(artifact.get("size"))
        except (TypeError, ValueError) as exc:
            raise PluginRegistryError("Artifact size 无效") from exc
        if expected_size < 1 or expected_size > MAX_ARTIFACT_BYTES:
            raise PluginRegistryError("Artifact 大小超出 100 MB 限制")
        expected_sha = str(artifact.get("sha256") or "").lower()
        if not re.fullmatch(r"[a-f0-9]{64}", expected_sha):
            raise PluginRegistryError("Artifact SHA-256 格式无效")

        with tempfile.NamedTemporaryFile(
            prefix="fuckclassroom-registry-",
            suffix=".zip",
            delete=False,
        ) as temporary:
            target = Path(temporary.name)

        digest = hashlib.sha256()
        total = 0
        try:
            request = Request(url, headers={"User-Agent": "FuckClassroom-Plugin-Downloader/1"})
            try:
                response = urlopen(request, timeout=45)
            except (HTTPError, URLError, OSError) as exc:
                raise PluginRegistryError(f"下载插件失败：{exc}") from exc
            with response:
                final_url = str(response.geturl())
                final = urlparse(final_url)
                if final.scheme != "https" or (final.hostname or "").lower() not in _ARTIFACT_REDIRECT_HOSTS:
                    raise PluginRegistryError(
                        f"Artifact 重定向到不受信任的地址：{final_url}"
                    )
                raw_length = response.headers.get("Content-Length")
                if raw_length:
                    try:
                        content_length = int(raw_length)
                    except ValueError:
                        content_length = 0
                    if content_length > MAX_ARTIFACT_BYTES:
                        raise PluginRegistryError("下载响应超过 100 MB 限制")

                with target.open("wb") as output:
                    while True:
                        self._check_cancelled(progress)
                        chunk = response.read(256 * 1024)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > expected_size or total > MAX_ARTIFACT_BYTES:
                            raise PluginRegistryError("Artifact 实际大小超过 Registry 声明")
                        digest.update(chunk)
                        output.write(chunk)
                        percent = 10 + int(60 * total / expected_size)
                        self._detail(
                            progress,
                            min(70, percent),
                            (
                                f"正在下载：{total / 1024 / 1024:.1f} / "
                                f"{expected_size / 1024 / 1024:.1f} MB"
                            ),
                        )

            if total != expected_size:
                raise PluginRegistryError(
                    f"Artifact 大小校验失败：Registry={expected_size}，实际={total}"
                )
            actual_sha = digest.hexdigest()
            if actual_sha != expected_sha:
                raise PluginRegistryError("Artifact SHA-256 校验失败")
            self._report(progress, 74, "SHA-256 校验通过")

            signature = artifact.get("signature")
            if signature is not None:
                if not isinstance(signature, Mapping):
                    raise PluginRegistryError("Artifact signature 格式无效")
                publisher_record = self._publisher(publisher)
                self.verify_artifact_signature(
                    expected_sha,
                    signature,
                    publisher_record,
                )
                self._report(progress, 78, "Ed25519 发布者签名验证通过")
            else:
                self._report(progress, 78, "Artifact 未提供签名，已完成 SHA-256 校验")
            self._clear_detail(progress)
            return target
        except Exception:
            target.unlink(missing_ok=True)
            raise

    @staticmethod
    def verify_artifact_signature(
        sha256: str,
        signature: Mapping[str, object],
        publisher: Mapping[str, object],
    ) -> None:
        if str(signature.get("scheme") or "") != _SIGNATURE_SCHEME:
            raise PluginRegistryError("不支持的 Artifact 签名方案")
        key_id = str(signature.get("key_id") or "")
        raw_keys = publisher.get("signing_keys", [])
        if not isinstance(raw_keys, list):
            raise PluginRegistryError("Publisher signing_keys 格式无效")
        key = next(
            (
                item
                for item in raw_keys
                if isinstance(item, Mapping) and str(item.get("id") or "") == key_id
            ),
            None,
        )
        if key is None:
            raise PluginRegistryError(f"Publisher 未登记签名密钥：{key_id}")
        if str(key.get("algorithm") or "") != "ed25519":
            raise PluginRegistryError("Publisher 签名密钥算法不是 Ed25519")
        try:
            public_key = base64.b64decode(
                str(key.get("public_key") or ""),
                validate=True,
            )
            signature_bytes = base64.b64decode(
                str(signature.get("value") or ""),
                validate=True,
            )
        except (ValueError, TypeError) as exc:
            raise PluginRegistryError("Publisher 公钥或 Artifact 签名不是有效 Base64") from exc
        if len(public_key) != 32 or len(signature_bytes) != 64:
            raise PluginRegistryError("Publisher Ed25519 公钥或签名长度无效")
        payload = (
            f"fuckclassroom-plugin-artifact-sha256-v1:{sha256}".encode("utf-8")
        )
        try:
            Ed25519PublicKey.from_public_bytes(public_key).verify(
                signature_bytes,
                payload,
            )
        except (ValueError, InvalidSignature) as exc:
            raise PluginRegistryError("Artifact Ed25519 签名验证失败") from exc

    @staticmethod
    def _registry_failure_kind(exc: OSError) -> str:
        reason = exc.reason if isinstance(exc, URLError) and not isinstance(exc, HTTPError) else exc
        detail = str(reason).lower()
        if isinstance(reason, ssl.SSLCertVerificationError) or (
            "certificate verify failed" in detail
            or "certificate_verify_failed" in detail
        ):
            return "certificate"
        if isinstance(exc, HTTPError):
            return "server" if exc.code in {429, 500, 502, 503, 504} else "http"
        if "handshake" in detail and ("timed out" in detail or "timeout" in detail):
            return "handshake_timeout"
        if isinstance(reason, TimeoutError) or "timed out" in detail or "timeout" in detail:
            return "timeout"
        if isinstance(reason, (ConnectionResetError, ConnectionAbortedError, BrokenPipeError)):
            return "connection"
        return "network"

    @staticmethod
    def _registry_failure_message(exc: OSError, *, attempts: int) -> str:
        kind = PluginRegistryClient._registry_failure_kind(exc)
        retry_note = f"（已尝试 {attempts} 次）" if attempts > 1 else ""
        if kind == "certificate":
            return "插件市场 HTTPS 证书验证失败；请检查系统时间和站点证书，程序不会跳过安全验证"
        if kind == "handshake_timeout":
            return (
                f"连接插件市场超时（HTTPS 握手未完成）{retry_note}；"
                "请稍后重试，并检查网络或代理能否访问 Registry 域名"
            )
        if kind == "timeout":
            return f"连接插件市场超时{retry_note}；请稍后重试或检查网络连接"
        if kind == "server":
            return f"插件市场服务器暂时不可用（HTTP {exc.code}）{retry_note}；请稍后重试"
        if kind == "http":
            return f"插件市场请求失败（HTTP {exc.code}）；请检查 Registry 地址"
        return f"无法连接插件市场{retry_note}；请检查网络、代理和 Registry 地址"

    def _fetch_json(self, url: str, *, progress=None) -> dict[str, object]:
        self._validate_registry_url(url)
        request = Request(url, headers={"User-Agent": "FuckClassroom-Registry/1"})
        for attempt in range(1, REGISTRY_FETCH_ATTEMPTS + 1):
            self._check_cancelled(progress)
            try:
                # Limit metadata handshakes independently of ZIP downloads.
                # Holding this slot while reading also bounds open TLS sessions.
                with self._registry_request_slots:
                    response = urlopen(request, timeout=REGISTRY_REQUEST_TIMEOUT_SECONDS)
                    with response:
                        final_url = str(response.geturl())
                        self._validate_registry_url(final_url)
                        raw_length = response.headers.get("Content-Length")
                        if raw_length:
                            try:
                                content_length = int(raw_length)
                            except ValueError:
                                content_length = 0
                            if content_length > MAX_REGISTRY_JSON_BYTES:
                                raise PluginRegistryError("Registry JSON 响应过大")
                        chunks: list[bytes] = []
                        total = 0
                        while True:
                            self._check_cancelled(progress)
                            chunk = response.read(64 * 1024)
                            if not chunk:
                                break
                            total += len(chunk)
                            if total > MAX_REGISTRY_JSON_BYTES:
                                raise PluginRegistryError("Registry JSON 响应过大")
                            chunks.append(chunk)
            except (HTTPError, URLError, OSError) as exc:
                kind = self._registry_failure_kind(exc)
                retryable = kind in {
                    "server", "handshake_timeout", "timeout", "connection"
                }
                logger.warning(
                    "Registry request %s failed (%d/%d): %r",
                    url, attempt, REGISTRY_FETCH_ATTEMPTS, exc,
                    exc_info=True,
                )
                if retryable and attempt < REGISTRY_FETCH_ATTEMPTS:
                    message = f"插件市场连接不稳定，正在重新连接（{attempt + 1}/{REGISTRY_FETCH_ATTEMPTS}）"
                    self._report(progress, 2, message)
                    self._detail(progress, 2, message)
                    # Wait outside the semaphore so another plugin can fetch.
                    # Cancellation is checked during the short backoff.
                    deadline = time.monotonic() + REGISTRY_RETRY_DELAY_SECONDS * attempt
                    while time.monotonic() < deadline:
                        self._check_cancelled(progress)
                        time.sleep(min(0.1, max(0.0, deadline - time.monotonic())))
                    continue
                raise PluginRegistryError(
                    self._registry_failure_message(exc, attempts=attempt)
                ) from exc

            try:
                payload = json.loads(b"".join(chunks).decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise PluginRegistryError("Registry 返回了无效 JSON") from exc
            if not isinstance(payload, dict):
                raise PluginRegistryError("Registry JSON 根节点必须是对象")
            return payload

        raise AssertionError("unreachable Registry fetch retry state")

    def _resolve_api(self, reference: str) -> str:
        if not reference:
            raise PluginRegistryError("Registry 链接为空")
        resolved = urljoin(self.api_base, reference)
        base = urlparse(self.api_base)
        parsed = urlparse(resolved)
        if (
            parsed.scheme != base.scheme
            or parsed.netloc != base.netloc
            or not parsed.path.startswith(base.path)
        ):
            raise PluginRegistryError(f"Registry 相对链接越界：{reference}")
        self._validate_registry_url(resolved)
        return resolved

    def _validate_registry_url(self, url: str) -> None:
        self._validate_transport(url)
        expected = urlparse(self.registry_url)
        parsed = urlparse(url)
        if parsed.scheme != expected.scheme or parsed.netloc != expected.netloc:
            raise PluginRegistryError("Registry JSON 不允许跨 Origin 跳转")

    @staticmethod
    def _validate_transport(url: str) -> None:
        parsed = urlparse(url)
        host = (parsed.hostname or "").lower()
        if parsed.username or parsed.password:
            raise PluginRegistryError("Registry URL 不允许包含认证信息")
        if parsed.scheme == "https":
            return
        if parsed.scheme == "http" and host in {"127.0.0.1", "localhost", "::1"}:
            return
        raise PluginRegistryError("Registry 必须使用 HTTPS；本地开发仅允许 loopback HTTP")

    @staticmethod
    def _validate_artifact_url(url: str, filename: str) -> None:
        parsed = urlparse(url)
        if (
            parsed.scheme != "https"
            or (parsed.hostname or "").lower() != "github.com"
            or not _ARTIFACT_PATH_RE.fullmatch(parsed.path)
            or Path(parsed.path).name != filename
            or parsed.username
            or parsed.password
        ):
            raise PluginRegistryError("Artifact URL 必须是 GitHub Release HTTPS ZIP")

    @staticmethod
    def _compare_versions(left: str, right: str) -> int:
        try:
            left_version = Version(left)
            right_version = Version(right)
        except InvalidVersion:
            return 1 if left != right else 0
        return (left_version > right_version) - (left_version < right_version)

    @staticmethod
    def _check_cancelled(progress) -> None:
        checker = getattr(progress, "raise_if_cancelled", None)
        if callable(checker):
            checker()

    @staticmethod
    def _report(progress, percent: int, message: str) -> None:
        if progress is not None:
            progress(percent, message)

    @staticmethod
    def _detail(progress, percent: int, message: str) -> None:
        detail = getattr(progress, "detail", None)
        if callable(detail):
            detail(percent, message)
        elif progress is not None:
            progress(percent, message)

    @staticmethod
    def _clear_detail(progress) -> None:
        clear = getattr(progress, "clear_detail", None)
        if callable(clear):
            clear()


__all__ = [
    "DEFAULT_PLUGIN_REGISTRY_URL",
    "PluginRegistryClient",
    "PluginRegistryError",
    "RegistryInstallPlan",
]
