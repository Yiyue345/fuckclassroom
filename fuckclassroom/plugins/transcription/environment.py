from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path


_COMPUTE_TYPE_ORDER = ("int8", "int8_float16", "float16", "float32", "default")


@dataclass(frozen=True, slots=True)
class AsrRuntimeCapabilities:
    cpu_compute_types: tuple[str, ...]
    cuda_compute_types: tuple[str, ...]
    cuda_available: bool
    cuda_reason: str
    cuda_device_count: int
    auto_device: str
    ctranslate2_version: str

    def compute_types_for(self, device: str) -> tuple[str, ...]:
        resolved = self.auto_device if device == "auto" else device
        return self.cuda_compute_types if resolved == "cuda" else self.cpu_compute_types

    def to_context(self) -> dict[str, object]:
        return {
            "cuda_available": self.cuda_available,
            "cuda_reason": self.cuda_reason,
            "cuda_device_count": self.cuda_device_count,
            "auto_device": self.auto_device,
            "ctranslate2_version": self.ctranslate2_version,
            "compute_types": {
                "cpu": list(self.cpu_compute_types),
                "cuda": list(self.cuda_compute_types),
            },
        }


def detect_asr_runtime() -> AsrRuntimeCapabilities:
    try:
        import ctranslate2
    except Exception as exc:
        return AsrRuntimeCapabilities(
            cpu_compute_types=("int8", "float32", "default"),
            cuda_compute_types=(),
            cuda_available=False,
            cuda_reason=f"CTranslate2 不可用：{exc}",
            cuda_device_count=0,
            auto_device="cpu",
            ctranslate2_version="",
        )

    version = str(getattr(ctranslate2, "__version__", "") or "")
    cpu_types = _supported_compute_types(ctranslate2, "cpu", ("int8", "float32", "default"))

    try:
        cuda_device_count = max(0, int(ctranslate2.get_cuda_device_count()))
    except Exception:
        cuda_device_count = 0

    cuda_reason = ""
    if cuda_device_count <= 0:
        cuda_reason = "未检测到可用的 NVIDIA CUDA 设备"
    else:
        missing = _missing_cuda_runtime_libraries(ctranslate2)
        if missing:
            cuda_reason = "缺少或无法加载 " + "、".join(missing)

    cuda_available = not cuda_reason
    cuda_types = (
        _supported_compute_types(
            ctranslate2,
            "cuda",
            ("int8", "int8_float16", "float16", "float32", "default"),
        )
        if cuda_available
        else ()
    )
    if cuda_available and not cuda_types:
        cuda_available = False
        cuda_reason = "CTranslate2 未报告可用的 CUDA 计算精度"

    return AsrRuntimeCapabilities(
        cpu_compute_types=cpu_types,
        cuda_compute_types=cuda_types,
        cuda_available=cuda_available,
        cuda_reason=cuda_reason,
        cuda_device_count=cuda_device_count,
        auto_device="cuda" if cuda_available else "cpu",
        ctranslate2_version=version,
    )


def build_settings_context(services, request) -> dict[str, object]:
    del services, request
    return {"asr_runtime": detect_asr_runtime().to_context()}


def resolve_auto_device() -> str:
    return detect_asr_runtime().auto_device


def _supported_compute_types(module, device: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
    try:
        supported = {str(item) for item in module.get_supported_compute_types(device)}
    except Exception:
        supported = set(fallback)
    supported.add("default")
    ordered = tuple(item for item in _COMPUTE_TYPE_ORDER if item in supported)
    return ordered or tuple(item for item in fallback if item in _COMPUTE_TYPE_ORDER)


def _missing_cuda_runtime_libraries(ctranslate2_module) -> list[str]:
    if sys.platform == "win32":
        package_dir = Path(ctranslate2_module.__file__).resolve().parent
        required = (
            ("cublas64_12.dll", "CUDA 12 cuBLAS"),
            ("cudnn64_9.dll", "cuDNN 9"),
        )
        missing: list[str] = []
        for filename, label in required:
            if not _loadable_windows_library(filename, package_dir):
                missing.append(label)
        return missing

    if sys.platform.startswith("linux"):
        required = (
            ("libcublas.so.12", "CUDA 12 cuBLAS"),
            ("libcudnn.so.9", "cuDNN 9"),
        )
        missing = []
        for filename, label in required:
            try:
                ctypes.CDLL(filename)
            except OSError:
                missing.append(label)
        return missing

    return ["当前操作系统不支持 CUDA 转写"]


def _loadable_windows_library(filename: str, package_dir: Path) -> bool:
    candidates: list[Path] = [package_dir / filename]

    cuda_path = os.environ.get("CUDA_PATH", "").strip()
    if cuda_path:
        candidates.append(Path(cuda_path) / "bin" / filename)

    for raw_dir in os.environ.get("PATH", "").split(os.pathsep):
        raw_dir = raw_dir.strip().strip('"')
        if raw_dir:
            candidates.append(Path(raw_dir) / filename)

    seen: set[str] = set()
    for candidate in candidates:
        key = str(candidate).lower()
        if key in seen or not candidate.is_file():
            continue
        seen.add(key)
        try:
            add_directory = getattr(os, "add_dll_directory", None)
            if callable(add_directory):
                handle = add_directory(str(candidate.parent))
                try:
                    ctypes.WinDLL(str(candidate))
                finally:
                    handle.close()
            else:
                ctypes.WinDLL(str(candidate))
            return True
        except OSError:
            continue

    try:
        ctypes.WinDLL(filename)
        return True
    except OSError:
        return False
