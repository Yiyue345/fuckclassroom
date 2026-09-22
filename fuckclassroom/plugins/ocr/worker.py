from __future__ import annotations

import base64

from fuckclassroom.core.config import AppConfig
from fuckclassroom.plugins.ocr.compat import install_rapidocr_compat
from fuckclassroom.plugins.ocr.services import OcrService


_service: OcrService | None = None


def _get_service(context) -> OcrService:
    global _service
    if _service is None:
        install_rapidocr_compat()
        _service = OcrService(AppConfig(data_dir=context.data_dir))
    return _service


def handle_call(method, params, context, progress):
    service = _get_service(context)
    if method == "ocr.can_ocr":
        warnings: list[str] = []
        return {
            "available": service.can_ocr(warnings),
            "warnings": warnings,
        }
    if method == "ocr.image":
        encoded = str(params.get("image_b64") or "")
        image_name = str(params.get("image_name") or "image.png")
        image_bytes = base64.b64decode(encoded, validate=False)
        stats = {"cache_hits": 0, "inferences": 0}
        text = service.ocr_image(
            image_bytes,
            image_name,
            stats=stats,
        )
        return {"text": text, "stats": stats}
    raise ValueError(f"未知 OCR Worker 方法：{method}")
