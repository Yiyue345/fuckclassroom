from __future__ import annotations

import hashlib
import mimetypes
import re
import shutil
import threading
from importlib.metadata import PackageNotFoundError, version as package_version
from pathlib import Path

from .compat import install_rapidocr_compat
from .engine import (
    increment_ocr_stat,
    rapidocr_ocr,
    tesseract_ocr,
    write_cache_text,
)
from ..processing.extraction import ExtractionError
from ...core.plugins import PluginContext


class OcrService:
    def __init__(self, config) -> None:
        self.config = config
        self.ai = None

    def ocr_image(
        self,
        image_bytes: bytes,
        image_name: str,
        *,
        stats: dict[str, int] | None = None,
        stats_lock: threading.Lock | None = None,
    ) -> str:
        engine = self.config.ocr_engine
        if engine in ("auto", "rapidocr"):
            try:
                cache_path = self._rapidocr_cache_path(image_bytes)
                if cache_path is not None and cache_path.exists():
                    try:
                        text = cache_path.read_text(encoding="utf-8")
                    except OSError:
                        pass
                    else:
                        increment_ocr_stat(stats, stats_lock, "cache_hits")
                        return text

                text = rapidocr_ocr(
                    image_bytes,
                    onnx_threads=self.config.ocr_onnx_threads,
                    max_side=self.config.ocr_max_side,
                    use_angle_cls=self.config.ocr_use_angle_cls,
                )
                increment_ocr_stat(stats, stats_lock, "inferences")
                if cache_path is not None:
                    write_cache_text(cache_path, text)
                return text
            except ImportError:
                if engine == "rapidocr":
                    raise ExtractionError(
                        "缺少 rapidocr-onnxruntime，请先安装项目依赖"
                    ) from None

        if engine in ("auto", "tesseract") and shutil.which("tesseract"):
            increment_ocr_stat(stats, stats_lock, "inferences")
            return tesseract_ocr(image_bytes, image_name)

        if engine in ("auto", "ai"):
            if self.ai is None:
                from ..ai_summary.client import AIClient

                self.ai = AIClient(self.config)
            media_type = mimetypes.guess_type(image_name)[0] or "image/png"
            increment_ocr_stat(stats, stats_lock, "inferences")
            return self.ai.ocr_image(image_bytes, media_type)

        raise ExtractionError("未找到可用 OCR 引擎。")

    def can_ocr(self, warnings: list[str]) -> bool:
        engine = self.config.ocr_engine
        if engine in ("auto", "rapidocr"):
            try:
                import rapidocr_onnxruntime  # noqa: F401

                return True
            except ImportError:
                if engine == "rapidocr":
                    warnings.append("已跳过 OCR：缺少 rapidocr-onnxruntime。")
                    return False
        if engine in ("auto", "tesseract") and shutil.which("tesseract"):
            return True
        if engine in ("auto", "ai") and self.config.ai_api_key:
            return True
        warnings.append("已跳过 OCR：未找到可用 OCR 引擎。")
        return False

    def _rapidocr_cache_path(self, image_bytes: bytes) -> Path | None:
        if not self.config.ocr_cache_enabled:
            return None
        digest = hashlib.sha256(image_bytes).hexdigest()
        try:
            rapidocr_version = package_version("rapidocr-onnxruntime")
        except PackageNotFoundError:
            rapidocr_version = "unknown"
        normalized_version = re.sub(r"[^A-Za-z0-9._-]+", "_", rapidocr_version)
        namespace = (
            f"rapidocr-{normalized_version}-side{self.config.ocr_max_side}-"
            f"cls{int(self.config.ocr_use_angle_cls)}"
        )
        return (
            self.config.cache_dir
            / "ocr"
            / namespace
            / digest[:2]
            / f"{digest}.txt"
        )


def setup_services(context: PluginContext) -> None:
    install_rapidocr_compat()
    context.services.add("ocr_service", OcrService(context.config))


__all__ = ["OcrService", "setup_services"]
