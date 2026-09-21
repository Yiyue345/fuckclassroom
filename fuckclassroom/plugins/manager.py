from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import os
import re
import shutil
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path
from types import ModuleType
from typing import Iterable

from ..core.config import AppConfig
from ..core.plugins import PluginSpec


_PLUGIN_ID_RE = re.compile(r"^[a-z][a-z0-9_-]{1,63}$")


class PluginManagerError(RuntimeError):
    pass


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

    @property
    def source_label(self) -> str:
        return "内置" if self.builtin else "外部"


_BUILTINS: tuple[PluginDescriptor, ...] = (
    PluginDescriptor("core_ui", "通用", "应用基础 UI、设置、任务与认证入口。", "builtin", 0, (), True, "fuckclassroom.plugins.core_ui"),
    PluginDescriptor("classroom", "课程录播", "课程、直播、回放、PPT 与下载基础能力。", "builtin", 10, (), True, "fuckclassroom.plugins.classroom"),
    PluginDescriptor("processing", "课次处理", "课次文字处理编排与结果页面。", "builtin", 20, ("classroom",), True, "fuckclassroom.plugins.processing"),
    PluginDescriptor("transcription", "本地语音转写", "使用 faster-whisper 从课程录像生成本地转写。", "builtin", 30, ("processing",), True, "fuckclassroom.plugins.transcription"),
    PluginDescriptor("ocr", "PPT OCR", "使用 RapidOCR、Tesseract 或视觉模型识别课件图片文字。", "builtin", 40, ("processing",), True, "fuckclassroom.plugins.ocr"),
    PluginDescriptor("ai_summary", "AI 总结", "将转写与 PPT 文本整理为结构化课程总结。", "builtin", 50, ("processing",), True, "fuckclassroom.plugins.ai_summary"),
    PluginDescriptor("attendance", "二维码签到", "识别畅课/课堂派二维码并处理签到。", "builtin", 60, ("classroom",), True, "fuckclassroom.plugins.attendance"),
    PluginDescriptor("course_selection", "本科选课", "本科选课、全校开课、定时选课与 Hy2。", "builtin", 20, (), True, "fuckclassroom.plugins.course_selection"),
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
        return spec

    def status_rows(self, current_enabled: Iterable[str]) -> list[dict[str, object]]:
        current = set(current_enabled)
        desired = self.desired_enabled()
        descriptor_map = self.descriptor_map()
        rows: list[dict[str, object]] = []
        for item in descriptor_map.values():
            dependents = sorted(
                other.id for other in descriptor_map.values() if item.id in other.requires
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
                    "current_enabled": item.id in current,
                    "desired_enabled": item.id in desired,
                    "restart_required": (item.id in current) != (item.id in desired),
                    "locked": item.id == "core_ui",
                }
            )
        return sorted(rows, key=lambda item: (descriptor_map[str(item["id"])].order, str(item["id"])))

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
        )

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
    specs = [manager.load_spec(descriptors[plugin_id]) for plugin_id in desired]
    from ..core.plugins import PluginRegistry

    return PluginRegistry(specs)


__all__ = [
    "PluginDescriptor",
    "PluginManager",
    "PluginManagerError",
    "build_registry",
]
