from __future__ import annotations

import hashlib
import importlib
import importlib.util
import importlib.metadata
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Iterable

from packaging.requirements import InvalidRequirement, Requirement
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.version import InvalidVersion, Version

from .. import __version__ as APP_VERSION
from ..core.config import PROJECT_ROOT, AppConfig
from ..core.plugins import PLUGIN_API_VERSION, PluginSpec
from .rpc import PLUGIN_RPC_API_VERSION


_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


class PluginManagerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class DependencySpec:
    requirement: str
    import_name: str
    label: str = ""
    optional: bool = False
    install_target: str = ""
    post_install: tuple[tuple[str, ...], ...] = ()
    probe: str = ""

    @property
    def display_name(self) -> str:
        if self.label:
            return self.label
        if self.requirement:
            try:
                return Requirement(self.requirement).name
            except InvalidRequirement:
                return self.requirement
        return self.import_name


@dataclass(frozen=True, slots=True)
class PluginDescriptor:
    id: str
    name: str
    description: str
    version: str
    order: int
    requires: tuple[str, ...]
    builtin: bool
    module: str = ""
    root: Path | None = None
    entry: str = "plugin.py"
    python_requirements: tuple[DependencySpec, ...] = ()
    api_version: int = PLUGIN_API_VERSION
    app_requires: str = ""
    execution: str = "in_process"
    route_prefix: str = ""
    rpc_api_version: int = PLUGIN_RPC_API_VERSION
    rpc_permissions: tuple[str, ...] = ()

    @property
    def source_label(self) -> str:
        if self.builtin:
            return "内置"
        return "外部 · 独立进程" if self.execution == "process" else "外部"


_PLAYWRIGHT = DependencySpec(
    "playwright>=1.45",
    "playwright",
    "Playwright + Chromium",
    post_install=(("{python}", "-m", "playwright", "install", "chromium"),),
    probe="playwright_chromium",
)

_BUILTINS: tuple[PluginDescriptor, ...] = (
    PluginDescriptor(
        "core_ui", "通用", "应用基础 UI、设置、任务与认证入口。", "builtin", 0, (), True,
        "fuckclassroom.plugins.core_ui",
        python_requirements=(),
    ),
    PluginDescriptor(
        "classroom", "课程录播", "课程、直播、回放、PPT 与下载基础能力。", "builtin", 10, (), True,
        "fuckclassroom.plugins.classroom",
        python_requirements=(_PLAYWRIGHT,),
    ),
    PluginDescriptor(
        "processing", "课次处理", "课次文字处理编排与结果页面。", "builtin", 20, ("classroom",), True,
        "fuckclassroom.plugins.processing",
        python_requirements=(DependencySpec("Pillow>=10.0", "PIL", "Pillow"),),
    ),
    PluginDescriptor(
        "transcription", "本地语音转写", "使用 faster-whisper 从课程录像生成本地转写。", "builtin", 30, ("processing",), True,
        "fuckclassroom.plugins.transcription",
        python_requirements=(
            DependencySpec("faster-whisper>=1.1", "faster_whisper", "faster-whisper"),
            DependencySpec("huggingface-hub>=0.32", "huggingface_hub", "Hugging Face Hub"),
            DependencySpec("opencc-python-reimplemented>=0.1.7", "opencc", "OpenCC"),
        ),
    ),
    PluginDescriptor(
        "ocr", "PPT OCR", "使用 RapidOCR、Tesseract 或视觉模型识别课件图片文字。", "builtin", 40, ("processing",), True,
        "fuckclassroom.plugins.ocr",
        python_requirements=(
            DependencySpec("rapidocr-onnxruntime>=1.2", "rapidocr_onnxruntime", "RapidOCR"),
        ),
    ),
    PluginDescriptor(
        "ai_summary", "AI 总结", "将转写与 PPT 文本整理为结构化课程总结。", "builtin", 50, ("processing",), True,
        "fuckclassroom.plugins.ai_summary",
        python_requirements=(),
    ),
    PluginDescriptor(
        "attendance", "二维码签到", "识别畅课/课堂派二维码并处理签到。", "builtin", 60, ("classroom",), True,
        "fuckclassroom.plugins.attendance",
        python_requirements=(
            DependencySpec("cryptography>=43.0", "cryptography", "cryptography"),
            DependencySpec("numpy>=1.24", "numpy", "NumPy"),
            DependencySpec("opencv-python>=4.8", "cv2", "OpenCV"),
            DependencySpec(
                "",
                "fuckclassroom_wxscan",
                "wxscan 原生扩展",
                optional=True,
                install_target=str(PROJECT_ROOT / "rust_module" / "wxscan_py"),
            ),
        ),
    ),
    PluginDescriptor(
        "course_selection", "本科选课", "本科选课、全校开课、定时选课与 Hy2。", "builtin", 20, (), True,
        "fuckclassroom.plugins.course_selection",
        python_requirements=(_PLAYWRIGHT,),
    ),
    PluginDescriptor(
        "lab_selection", "实验课选课", "实验课程、整组与时间段选课、自动候补，共享本科教务会话。", "builtin", 21, (), True,
        "fuckclassroom.plugins.lab_selection",
        python_requirements=(_PLAYWRIGHT,),
    ),
)


class PluginManager:
    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.plugin_dir = config.data_dir / "plugins"
        self.state_path = config.data_dir / "plugin_state.json"
        self._runtime_override: frozenset[str] | None = None

    @property
    def builtin_descriptors(self) -> tuple[PluginDescriptor, ...]:
        return _BUILTINS

    @property
    def api_version(self) -> int:
        return PLUGIN_API_VERSION

    @property
    def app_version(self) -> str:
        return APP_VERSION

    def set_runtime_override(self, enabled: Iterable[str] | None) -> None:
        self._runtime_override = None if enabled is None else frozenset(enabled)

    @property
    def selection_source(self) -> str:
        if self._runtime_override is not None:
            return "argument"
        if os.environ.get("FUCKCLASSROOM_PLUGINS") is not None:
            return "environment"
        if self.state_path.exists():
            return "state"
        return "default"

    @property
    def mutable(self) -> bool:
        return self.selection_source in {"state", "default"}

    def descriptors(self) -> tuple[PluginDescriptor, ...]:
        items = list(self.builtin_descriptors)
        items.extend(self._external_descriptors())
        seen: set[str] = set()
        for item in items:
            if item.id in seen:
                raise PluginManagerError(f"插件 ID 重复：{item.id}")
            seen.add(item.id)
        return tuple(sorted(items, key=lambda item: (item.order, item.id)))

    def descriptor_map(self) -> dict[str, PluginDescriptor]:
        return {item.id: item for item in self.descriptors()}

    def desired_enabled(self) -> set[str]:
        if self._runtime_override is not None:
            selected = set(self._runtime_override)
        else:
            raw_env = os.environ.get("FUCKCLASSROOM_PLUGINS")
            if raw_env is not None:
                selected = {
                    value.strip()
                    for value in raw_env.replace(";", ",").split(",")
                    if value.strip()
                }
            else:
                selected = self._load_selected()
                # A manually removed external plugin should not make the whole app
                # unbootable. Missing direct selections are dropped; missing
                # dependencies of an installed/enabled plugin still fail below.
                selected.intersection_update(self.descriptor_map())
        return self._expand_dependencies(selected)

    def configured_selected(self) -> set[str]:
        return self._load_selected()

    def enable(self, plugin_id: str) -> set[str]:
        self._ensure_mutable()
        descriptors = self.descriptor_map()
        if plugin_id not in descriptors:
            raise PluginManagerError(f"插件不存在：{plugin_id}")
        selected = self._load_selected()
        selected.add(plugin_id)
        desired = self._expand_dependencies(selected)
        # Validate external code before persisting the enable request so a broken
        # package does not make the next application start fail immediately.
        for enabled_id in sorted(desired):
            descriptor = descriptors[enabled_id]
            if not descriptor.builtin:
                compatibility = self.compatibility_status(descriptor)
                if not compatibility["compatible"]:
                    raise PluginManagerError(
                        f"插件 {descriptor.name} 不兼容：{compatibility['message']}"
                    )
                missing = [
                    item["label"]
                    for item in self.dependency_status(enabled_id)
                    if not item["installed"] and not item["optional"]
                ]
                if missing:
                    raise PluginManagerError(
                        f"插件 {descriptor.name} 缺少依赖："
                        + "、".join(str(item) for item in missing)
                        + "；请先安装依赖"
                    )
                self.load_spec(descriptor)
        self._save_selected(selected)
        return desired

    def disable(self, plugin_id: str) -> set[str]:
        self._ensure_mutable()
        if plugin_id == "core_ui":
            raise PluginManagerError("core_ui 是应用基础插件，不能禁用")
        descriptors = self.descriptor_map()
        if plugin_id not in descriptors:
            raise PluginManagerError(f"插件不存在：{plugin_id}")
        selected = self._load_selected()
        selected.difference_update(self._dependent_closure(plugin_id, descriptors))
        self._save_selected(selected)
        return self._expand_dependencies(selected)

    def install_from_path(self, raw_path: str) -> PluginDescriptor:
        self._ensure_mutable()
        value = raw_path.strip().strip('"')
        if not value:
            raise PluginManagerError("请输入插件目录或 ZIP 文件路径")
        source = Path(value).expanduser()
        if not source.exists():
            raise PluginManagerError("插件路径不存在")

        self.plugin_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="fuckclassroom-plugin-") as temp_dir:
            temporary = Path(temp_dir)
            if source.is_dir():
                root = self._locate_manifest_root(source)
            elif source.is_file() and source.suffix.lower() == ".zip":
                self._safe_extract_zip(source, temporary)
                root = self._locate_manifest_root(temporary)
            else:
                raise PluginManagerError("只支持插件目录或 .zip 插件包")

            descriptor = self._read_external_manifest(root)
            if descriptor.id in {item.id for item in self.builtin_descriptors}:
                raise PluginManagerError("不能覆盖内置插件")
            destination = self.plugin_dir / descriptor.id
            if destination.exists():
                raise PluginManagerError(f"插件已存在：{descriptor.id}")
            try:
                shutil.copytree(root, destination)
            except OSError as exc:
                raise PluginManagerError(f"复制插件文件失败：{exc}") from exc

        return self._read_external_manifest(self.plugin_dir / descriptor.id)

    def remove(self, plugin_id: str, *, current_enabled: Iterable[str] = ()) -> None:
        self._ensure_mutable()
        descriptor = self.descriptor_map().get(plugin_id)
        if descriptor is None:
            raise PluginManagerError(f"插件不存在：{plugin_id}")
        if descriptor.builtin:
            raise PluginManagerError("内置插件不能删除")
        if plugin_id in set(current_enabled):
            raise PluginManagerError("插件当前仍在运行，请先禁用并重启应用后再删除")
        selected = self._load_selected()
        if plugin_id in self._expand_dependencies(selected):
            raise PluginManagerError("插件仍被配置为启用，请先禁用")
        destination = self.plugin_dir / plugin_id
        if destination.exists():
            shutil.rmtree(destination)

    def load_spec(self, descriptor: PluginDescriptor) -> PluginSpec:
        compatibility = self.compatibility_status(descriptor)
        if not compatibility["compatible"]:
            raise PluginManagerError(
                f"插件 {descriptor.name} 不兼容：{compatibility['message']}"
            )
        if not descriptor.builtin and descriptor.execution == "process":
            from .process_runtime import build_process_plugin_spec

            return build_process_plugin_spec(descriptor)
        if descriptor.builtin:
            module = importlib.import_module(descriptor.module)
        else:
            module = self._load_external_module(descriptor)
        builder = getattr(module, "build_plugin", None)
        if not callable(builder):
            raise PluginManagerError(f"插件 {descriptor.id} 未提供 build_plugin()")
        spec = builder()
        if not isinstance(spec, PluginSpec):
            raise PluginManagerError(f"插件 {descriptor.id} 的 build_plugin() 未返回 PluginSpec")
        if spec.id != descriptor.id:
            raise PluginManagerError(
                f"插件清单 ID {descriptor.id} 与 PluginSpec ID {spec.id} 不一致"
            )
        if tuple(spec.requires) != tuple(descriptor.requires):
            raise PluginManagerError(
                f"插件 {descriptor.id} 的 requires 与 plugin.json 不一致"
                if not descriptor.builtin
                else f"内置插件 {descriptor.id} 的依赖声明不一致"
            )
        if int(spec.api_version) != int(descriptor.api_version):
            raise PluginManagerError(
                f"插件 {descriptor.id} 的 PluginSpec.api_version="
                f"{spec.api_version} 与清单 api_version={descriptor.api_version} 不一致"
            )
        return spec

    def compatibility_status(
        self,
        descriptor: PluginDescriptor,
    ) -> dict[str, object]:
        reasons: list[str] = []
        if int(descriptor.api_version) != PLUGIN_API_VERSION:
            reasons.append(
                f"插件 API {descriptor.api_version}，当前支持 API {PLUGIN_API_VERSION}"
            )

        if descriptor.execution == "process" and int(descriptor.rpc_api_version) != PLUGIN_RPC_API_VERSION:
            reasons.append(
                f"Plugin RPC API {descriptor.rpc_api_version}，当前支持 RPC API {PLUGIN_RPC_API_VERSION}"
            )

        if descriptor.app_requires:
            try:
                app_spec = SpecifierSet(descriptor.app_requires)
                current_version = Version(APP_VERSION)
            except (InvalidSpecifier, InvalidVersion):
                reasons.append("应用版本约束无效")
            else:
                if not app_spec.contains(current_version, prereleases=current_version.is_prerelease):
                    reasons.append(
                        f"需要 FuckClassroom {descriptor.app_requires}，当前为 {APP_VERSION}"
                    )

        return {
            "compatible": not reasons,
            "message": "；".join(reasons) if reasons else "兼容",
            "api_version": descriptor.api_version,
            "current_api_version": PLUGIN_API_VERSION,
            "app_requires": descriptor.app_requires,
            "app_version": APP_VERSION,
        }

    def dependency_status(self, plugin_id: str) -> list[dict[str, object]]:
        descriptor = self.descriptor_map().get(plugin_id)
        if descriptor is None:
            raise PluginManagerError(f"插件不存在：{plugin_id}")
        return [self._dependency_status(item) for item in descriptor.python_requirements]

    def install_dependencies(self, plugin_id: str, progress=None) -> None:
        descriptor = self.descriptor_map().get(plugin_id)
        if descriptor is None:
            raise PluginManagerError(f"插件不存在：{plugin_id}")
        dependencies = descriptor.python_requirements
        if not dependencies:
            if progress:
                progress(100, "该插件没有额外依赖")
            return

        required_targets = [
            item.install_target or item.requirement
            for item in dependencies
            if not item.optional and (item.install_target or item.requirement)
        ]
        optional_targets = [
            item.install_target or item.requirement
            for item in dependencies
            if item.optional and (item.install_target or item.requirement)
        ]
        if required_targets:
            self._run_install_command(
                [sys.executable, "-m", "pip", "install", *required_targets],
                progress,
                5,
                68,
                "正在安装必需依赖",
            )

        for index, target in enumerate(optional_targets):
            try:
                self._run_install_command(
                    [sys.executable, "-m", "pip", "install", target],
                    progress,
                    69 + index * 4,
                    min(80, 72 + index * 4),
                    "正在安装可选增强",
                )
            except PluginManagerError as exc:
                if progress:
                    logger = getattr(progress, "log", None)
                    if callable(logger):
                        logger(f"可选依赖安装失败，可稍后重试：{exc}", "warning")

        post_commands: list[tuple[str, ...]] = []
        seen_commands: set[tuple[str, ...]] = set()
        for dependency in dependencies:
            for command in dependency.post_install:
                resolved = tuple(
                    sys.executable if part == "{python}" else part
                    for part in command
                )
                if resolved not in seen_commands:
                    seen_commands.add(resolved)
                    post_commands.append(resolved)

        if post_commands:
            span = max(1, 20 // len(post_commands))
            for index, command in enumerate(post_commands):
                self._run_install_command(
                    list(command),
                    progress,
                    81 + index * span,
                    min(99, 81 + (index + 1) * span),
                    "正在安装运行时组件",
                )

        remaining = [
            item["label"]
            for item in self.dependency_status(plugin_id)
            if not item["installed"] and not item["optional"]
        ]
        if remaining:
            raise PluginManagerError(
                "依赖安装完成后仍缺少：" + "、".join(str(item) for item in remaining)
            )
        if progress:
            progress(100, "插件依赖安装完成")

    def _run_install_command(
        self,
        command: list[str],
        progress,
        start_percent: int,
        end_percent: int,
        message: str,
    ) -> None:
        if progress:
            progress(start_percent, message)
            logger = getattr(progress, "log", None)
            if callable(logger):
                logger("$ " + " ".join(command), "info")
        try:
            process = subprocess.Popen(
                command,
                cwd=str(PROJECT_ROOT),
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except OSError as exc:
            raise PluginManagerError(f"无法启动依赖安装命令：{exc}") from exc

        while True:
            try:
                output, _ = process.communicate(timeout=0.5)
                break
            except subprocess.TimeoutExpired:
                if progress:
                    raise_if_cancelled = getattr(progress, "raise_if_cancelled", None)
                    if callable(raise_if_cancelled):
                        try:
                            raise_if_cancelled()
                        except BaseException:
                            process.terminate()
                            try:
                                process.wait(timeout=5)
                            except subprocess.TimeoutExpired:
                                process.kill()
                            raise
                    current = min(end_percent - 1, start_percent + 1)
                    progress(current, message)

        if progress:
            logger = getattr(progress, "log", None)
            if callable(logger) and output:
                for line in output.splitlines():
                    if line.strip():
                        logger(line, "info")
        if process.returncode != 0:
            tail = "\n".join(output.splitlines()[-12:]) if output else ""
            raise PluginManagerError(
                f"依赖安装命令失败（exit {process.returncode}）"
                + (f"：{tail}" if tail else "")
            )
        if progress:
            progress(end_percent, message + "完成")

    def _dependency_status(self, dependency: DependencySpec) -> dict[str, object]:
        try:
            installed = importlib.util.find_spec(dependency.import_name) is not None
        except (ImportError, ModuleNotFoundError, ValueError):
            installed = False
        version = ""
        requirement_name = ""
        version_ok = True
        if dependency.requirement:
            try:
                parsed = Requirement(dependency.requirement)
                requirement_name = parsed.name
                if parsed.marker is not None and not parsed.marker.evaluate():
                    return {
                        "label": dependency.display_name,
                        "requirement": dependency.requirement,
                        "import_name": dependency.import_name,
                        "installed": True,
                        "version": "",
                        "optional": dependency.optional,
                        "skipped": True,
                    }
                try:
                    version = importlib.metadata.version(parsed.name)
                except importlib.metadata.PackageNotFoundError:
                    installed = False
                if installed and version and parsed.specifier:
                    try:
                        version_ok = Version(version) in parsed.specifier
                    except InvalidVersion:
                        version_ok = False
            except InvalidRequirement:
                installed = False
        installed = bool(installed and version_ok)
        if installed and dependency.probe:
            installed = self._probe_dependency(dependency.probe)
        return {
            "label": dependency.display_name,
            "requirement": dependency.requirement or dependency.install_target,
            "import_name": dependency.import_name,
            "installed": installed,
            "version": version,
            "optional": dependency.optional,
            "skipped": False,
        }

    def status_rows(self, current_enabled: Iterable[str]) -> list[dict[str, object]]:
        current = set(current_enabled)
        desired = self.desired_enabled()
        descriptor_map = self.descriptor_map()
        rows: list[dict[str, object]] = []
        for item in descriptor_map.values():
            dependents = sorted(
                other.id for other in descriptor_map.values() if item.id in other.requires
            )
            dependencies = self.dependency_status(item.id)
            compatibility = self.compatibility_status(item)
            missing_required = [
                dependency for dependency in dependencies
                if not dependency["installed"] and not dependency["optional"]
            ]
            missing_optional = [
                dependency for dependency in dependencies
                if not dependency["installed"] and dependency["optional"]
            ]
            dependency_state = (
                "required_missing"
                if missing_required
                else "optional_missing"
                if missing_optional
                else "ready"
            )
            rows.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "description": item.description,
                    "version": item.version,
                    "requires": item.requires,
                    "dependents": dependents,
                    "builtin": item.builtin,
                    "source_label": item.source_label,
                    "execution": item.execution,
                    "isolated": item.execution == "process",
                    "route_prefix": item.route_prefix,
                    "rpc_api_version": item.rpc_api_version,
                    "rpc_permissions": item.rpc_permissions,
                    "current_enabled": item.id in current,
                    "desired_enabled": item.id in desired,
                    "restart_required": (item.id in current) != (item.id in desired),
                    "locked": item.id == "core_ui",
                    "dependencies": dependencies,
                    "missing_required_dependencies": missing_required,
                    "missing_optional_dependencies": missing_optional,
                    "dependency_state": dependency_state,
                    "dependency_issue": bool(missing_required),
                    "compatible": compatibility["compatible"],
                    "compatibility_message": compatibility["message"],
                    "api_version": compatibility["api_version"],
                    "current_api_version": compatibility["current_api_version"],
                    "app_requires": compatibility["app_requires"],
                    "app_version": compatibility["app_version"],
                }
            )
        return sorted(
            rows,
            key=lambda item: (
                descriptor_map[str(item["id"])].order,
                str(item["id"]),
            ),
        )

    def _load_selected(self) -> set[str]:
        if not self.state_path.exists():
            return {item.id for item in self.builtin_descriptors}
        try:
            payload = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginManagerError(f"读取插件状态失败：{exc}") from exc
        values = payload.get("selected", [])
        if not isinstance(values, list):
            raise PluginManagerError("plugin_state.json 中 selected 必须是数组")
        selected = {str(item).strip() for item in values if str(item).strip()}
        selected.add("core_ui")
        return selected

    def _save_selected(self, selected: Iterable[str]) -> None:
        values = sorted(set(selected) | {"core_ui"})
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps({"selected": values}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self.state_path)

    def _expand_dependencies(self, selected: Iterable[str]) -> set[str]:
        descriptors = self.descriptor_map()
        enabled = set(selected)
        enabled.add("core_ui")
        unknown = enabled.difference(descriptors)
        if unknown:
            raise PluginManagerError(f"未知插件：{', '.join(sorted(unknown))}")
        pending = list(enabled)
        while pending:
            plugin_id = pending.pop()
            for dependency in descriptors[plugin_id].requires:
                if dependency not in descriptors:
                    raise PluginManagerError(
                        f"插件 {plugin_id} 缺少依赖：{dependency}"
                    )
                if dependency not in enabled:
                    enabled.add(dependency)
                    pending.append(dependency)
        return enabled

    def _dependent_closure(
        self,
        plugin_id: str,
        descriptors: dict[str, PluginDescriptor],
    ) -> set[str]:
        removed = {plugin_id}
        changed = True
        while changed:
            changed = False
            for item in descriptors.values():
                if item.id in removed:
                    continue
                if any(dependency in removed for dependency in item.requires):
                    removed.add(item.id)
                    changed = True
        return removed

    def _ensure_mutable(self) -> None:
        if self.selection_source == "environment":
            raise PluginManagerError(
                "当前由 FUCKCLASSROOM_PLUGINS 环境变量控制插件，网页不能修改"
            )
        if self.selection_source == "argument":
            raise PluginManagerError("当前由 create_app(enabled_plugins=...) 控制插件，网页不能修改")

    def _external_descriptors(self) -> list[PluginDescriptor]:
        if not self.plugin_dir.exists():
            return []
        items: list[PluginDescriptor] = []
        for child in sorted(self.plugin_dir.iterdir()):
            if not child.is_dir() or not (child / "plugin.json").is_file():
                continue
            items.append(self._read_external_manifest(child))
        return items

    def _read_external_manifest(self, root: Path) -> PluginDescriptor:
        manifest_path = root / "plugin.json"
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except FileNotFoundError as exc:
            raise PluginManagerError("插件缺少 plugin.json") from exc
        except (OSError, json.JSONDecodeError) as exc:
            raise PluginManagerError(f"读取 plugin.json 失败：{exc}") from exc

        plugin_id = str(payload.get("id") or "").strip()
        if not _PLUGIN_ID_RE.fullmatch(plugin_id):
            raise PluginManagerError(
                "插件 ID 必须以小写字母开头，只能包含小写字母、数字、_、-，长度 2-64"
            )
        name = str(payload.get("name") or plugin_id).strip() or plugin_id
        description = str(payload.get("description") or "").strip()
        version = str(payload.get("version") or "unknown").strip() or "unknown"
        entry = str(payload.get("entry") or "plugin.py").strip()
        entry_path = Path(entry)
        if entry_path.is_absolute() or ".." in entry_path.parts or entry_path.suffix != ".py":
            raise PluginManagerError("plugin.json 的 entry 必须是插件目录内的 .py 文件")
        if not (root / entry_path).is_file():
            raise PluginManagerError(f"插件入口不存在：{entry}")

        requires_payload = payload.get("requires", [])
        if not isinstance(requires_payload, list):
            raise PluginManagerError("plugin.json 的 requires 必须是数组")
        requires = tuple(str(item).strip() for item in requires_payload if str(item).strip())
        for dependency in requires:
            if not _PLUGIN_ID_RE.fullmatch(dependency):
                raise PluginManagerError(f"非法依赖插件 ID：{dependency}")

        try:
            order = int(payload.get("order", 100))
        except (TypeError, ValueError):
            order = 100

        try:
            api_version = int(payload.get("api_version", PLUGIN_API_VERSION))
        except (TypeError, ValueError) as exc:
            raise PluginManagerError("plugin.json 的 api_version 必须是整数") from exc
        if api_version < 1:
            raise PluginManagerError("plugin.json 的 api_version 必须大于等于 1")

        app_requires = str(payload.get("app_requires") or "").strip()
        if app_requires:
            try:
                SpecifierSet(app_requires)
            except InvalidSpecifier as exc:
                raise PluginManagerError(
                    f"plugin.json 的 app_requires 无效：{app_requires}"
                ) from exc

        python_requirements = self._parse_external_requirements(
            payload.get("python_requires", [])
        )

        execution = str(payload.get("execution") or "in_process").strip().lower()
        if execution not in {"in_process", "process"}:
            raise PluginManagerError("plugin.json 的 execution 只支持 in_process 或 process")
        route_prefix = str(payload.get("route_prefix") or "").strip()
        if execution == "process":
            route_prefix = route_prefix or f"/plugins-runtime/{plugin_id}"
            if (
                not route_prefix.startswith("/plugins-runtime/")
                or "{" in route_prefix
                or "}" in route_prefix
                or "//" in route_prefix
                or ".." in Path(route_prefix).parts
            ):
                raise PluginManagerError(
                    "进程隔离插件 route_prefix 必须位于 /plugins-runtime/ 下且不能包含动态路径"
                )
            route_prefix = route_prefix.rstrip("/")
        elif route_prefix:
            raise PluginManagerError("route_prefix 仅适用于 execution=process 的插件")

        try:
            rpc_api_version = int(payload.get("rpc_api_version", PLUGIN_RPC_API_VERSION))
        except (TypeError, ValueError) as exc:
            raise PluginManagerError("plugin.json 的 rpc_api_version 必须是整数") from exc

        rpc_permissions_payload = payload.get("rpc_permissions", [])
        if not isinstance(rpc_permissions_payload, list):
            raise PluginManagerError("plugin.json 的 rpc_permissions 必须是数组")
        rpc_permissions: list[str] = []
        rpc_method_re = re.compile(r"^[a-z][a-z0-9_-]*(?:\.[a-z][a-z0-9_-]*)+$")
        for raw_permission in rpc_permissions_payload:
            permission = str(raw_permission or "").strip()
            if not rpc_method_re.fullmatch(permission):
                raise PluginManagerError(f"非法 RPC 权限：{permission or raw_permission}")
            if permission.startswith("internal."):
                raise PluginManagerError(
                    f"RPC 权限 {permission} 为 FuckClassroom 内部 Worker 保留能力"
                )
            if permission not in rpc_permissions:
                rpc_permissions.append(permission)
        if execution != "process" and (rpc_permissions or "rpc_api_version" in payload):
            raise PluginManagerError(
                "rpc_api_version/rpc_permissions 仅适用于 execution=process 的插件"
            )

        return PluginDescriptor(
            id=plugin_id,
            name=name,
            description=description,
            version=version,
            order=order,
            requires=requires,
            builtin=False,
            root=root,
            entry=entry,
            python_requirements=python_requirements,
            api_version=api_version,
            app_requires=app_requires,
            execution=execution,
            route_prefix=route_prefix,
            rpc_api_version=rpc_api_version,
            rpc_permissions=tuple(rpc_permissions),
        )

    @staticmethod
    def _probe_dependency(probe: str) -> bool:
        if probe == "playwright_chromium":
            try:
                from playwright.sync_api import sync_playwright

                with sync_playwright() as playwright:
                    return Path(playwright.chromium.executable_path).is_file()
            except Exception:
                return False
        return True

    @staticmethod
    def _parse_external_requirements(payload) -> tuple[DependencySpec, ...]:
        if payload in (None, []):
            return ()
        if not isinstance(payload, list):
            raise PluginManagerError("plugin.json 的 python_requires 必须是数组")

        items: list[DependencySpec] = []
        for raw in payload:
            if isinstance(raw, str):
                requirement_text = raw.strip()
                import_name = ""
                label = ""
                optional = False
            elif isinstance(raw, dict):
                requirement_text = str(raw.get("requirement") or "").strip()
                import_name = str(raw.get("import") or "").strip()
                label = str(raw.get("label") or "").strip()
                optional = bool(raw.get("optional", False))
            else:
                raise PluginManagerError("python_requires 项必须是字符串或对象")

            try:
                parsed = Requirement(requirement_text)
            except InvalidRequirement as exc:
                raise PluginManagerError(
                    f"非法 Python 依赖：{requirement_text or raw}"
                ) from exc
            if parsed.url is not None:
                raise PluginManagerError("外部插件 python_requires 不允许 URL/本地路径依赖")
            guessed_import = parsed.name.replace("-", "_").replace(".", "_")
            import_value = import_name or guessed_import
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.]*", import_value):
                raise PluginManagerError(f"非法依赖 import 名称：{import_value}")
            items.append(
                DependencySpec(
                    requirement=requirement_text,
                    import_name=import_value,
                    label=label or parsed.name,
                    optional=optional,
                )
            )
        return tuple(items)

    @staticmethod
    def _locate_manifest_root(source: Path) -> Path:
        if (source / "plugin.json").is_file():
            return source
        children = [
            child for child in source.iterdir()
            if child.is_dir() and (child / "plugin.json").is_file()
        ]
        if len(children) == 1:
            return children[0]
        raise PluginManagerError("未找到唯一的 plugin.json 插件根目录")

    @staticmethod
    def _safe_extract_zip(source: Path, destination: Path) -> None:
        try:
            with zipfile.ZipFile(source) as archive:
                for item in archive.infolist():
                    normalized = item.filename.replace("\\", "/")
                    path = Path(normalized)
                    first_part = path.parts[0] if path.parts else ""
                    if (
                        path.is_absolute()
                        or ".." in path.parts
                        or ":" in first_part
                    ):
                        raise PluginManagerError("ZIP 中包含不安全路径")
                    mode = (item.external_attr >> 16) & 0o170000
                    if mode == 0o120000:
                        raise PluginManagerError("ZIP 插件包不允许包含符号链接")
                archive.extractall(destination)
        except zipfile.BadZipFile as exc:
            raise PluginManagerError("插件 ZIP 文件损坏") from exc

    @staticmethod
    def _load_external_module(descriptor: PluginDescriptor) -> ModuleType:
        if descriptor.root is None:
            raise PluginManagerError(f"外部插件 {descriptor.id} 缺少安装目录")
        entry = descriptor.root / descriptor.entry
        module_suffix = hashlib.sha256(descriptor.id.encode("utf-8")).hexdigest()[:10]
        safe_id = descriptor.id.replace("-", "_")
        module_name = f"fuckclassroom_external_{safe_id}_{module_suffix}"
        spec = importlib.util.spec_from_file_location(
            module_name,
            entry,
            submodule_search_locations=[str(descriptor.root)],
        )
        if spec is None or spec.loader is None:
            raise PluginManagerError(f"无法加载插件入口：{entry}")
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        try:
            spec.loader.exec_module(module)
        except Exception:
            sys.modules.pop(module_name, None)
            raise
        return module


def build_registry(
    manager: PluginManager,
    enabled: Iterable[str] | None = None,
):
    if enabled is not None:
        manager.set_runtime_override(enabled)
    desired = manager.desired_enabled()
    descriptors = manager.descriptor_map()

    blocked = {
        plugin_id
        for plugin_id in desired
        if not descriptors[plugin_id].builtin
        and (
            not manager.compatibility_status(descriptors[plugin_id])["compatible"]
            or any(
                not item["installed"] and not item["optional"]
                for item in manager.dependency_status(plugin_id)
            )
        )
    }
    changed = True
    while changed:
        changed = False
        for plugin_id in desired:
            if plugin_id in blocked:
                continue
            if any(dependency in blocked for dependency in descriptors[plugin_id].requires):
                blocked.add(plugin_id)
                changed = True

    active = desired.difference(blocked)
    specs = [manager.load_spec(descriptors[plugin_id]) for plugin_id in active]
    from ..core.plugins import PluginRegistry

    return PluginRegistry(specs)


__all__ = [
    "DependencySpec",
    "PluginDescriptor",
    "PluginManager",
    "PluginManagerError",
    "build_registry",
]
