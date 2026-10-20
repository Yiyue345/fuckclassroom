from __future__ import annotations

import base64
import io
import unittest
from pathlib import Path

from PIL import Image

from fuckclassroom.classroom import ClassroomClientError
from fuckclassroom.core.plugins import PluginServiceError
from fuckclassroom.plugins.ocr.services import (
    _RPC_IMAGE_TARGET_BYTES,
    _prepare_rpc_image,
)
from fuckclassroom.plugins.processing import extraction
from fuckclassroom.plugins.processing.extraction import (
    LessonExtractionService,
    _resolve_ocr_availability,
)
from fuckclassroom.plugins.rpc import PluginRpcInvalidParams, _decode_image_b64


class PluginSplitRegressionTests(unittest.TestCase):
    def test_classroom_errors_use_stable_plugin_service_contract(self) -> None:
        self.assertTrue(issubclass(ClassroomClientError, PluginServiceError))

    def test_processing_has_no_classroom_implementation_import(self) -> None:
        processing_dir = Path(extraction.__file__).resolve().parent
        source = "\n".join(
            (processing_dir / name).read_text(encoding="utf-8")
            for name in ("extraction.py", "routes.py")
        )
        self.assertNotIn("from ...classroom", source)
        self.assertNotIn("from fuckclassroom.classroom", source)

        service = LessonExtractionService(service_lookup=None)
        self.assertIsNone(service.classroom)
        with self.assertRaises(PluginServiceError):
            service._classroom_service()

    def test_ocr_availability_failure_is_optional_capability(self) -> None:
        warnings: list[str] = []

        def broken_can_ocr(_warnings: list[str]) -> bool:
            raise PluginServiceError("worker disconnected")

        available = _resolve_ocr_availability(
            True,
            broken_can_ocr,
            warnings,
        )

        self.assertFalse(available)
        self.assertEqual(len(warnings), 1)
        self.assertIn("OCR 插件不可用，已跳过", warnings[0])
        self.assertIn("worker disconnected", warnings[0])

    def test_ai_ocr_base64_is_strictly_validated(self) -> None:
        payload = b"valid-image"
        encoded = base64.b64encode(payload).decode("ascii")
        self.assertEqual(_decode_image_b64(encoded), payload)

        for invalid in ("", "%%%%", "YWJj$A=="):
            with self.subTest(invalid=invalid):
                with self.assertRaises(PluginRpcInvalidParams):
                    _decode_image_b64(invalid)

    def test_large_local_ocr_image_preserves_original_bytes(self) -> None:
        import tempfile
        from types import SimpleNamespace
        from unittest.mock import Mock
        from fuckclassroom.plugins.ocr.services import OcrProcessProxy
        from fuckclassroom.plugins.ocr.worker import handle_call
        from fuckclassroom.core.config import AppConfig

        class StubWorkerContext:
            def __init__(self, data_dir):
                self.data_dir = data_dir

        with tempfile.TemporaryDirectory() as folder:
            image = Image.new("RGB", (2048, 2048), "white")
            buffer = io.BytesIO()
            image.save(buffer, format="BMP")
            raw = buffer.getvalue()
            self.assertGreater(len(raw), _RPC_IMAGE_TARGET_BYTES)
            sent = {}

            def call(method, params, *, timeout):
                sent.update(params)
                self.assertEqual(method, "ocr.image")
                self.assertNotIn("image_b64", params)
                path = Path(params["image_path"])
                self.assertEqual(path.read_bytes(), raw)
                return {"text": "OCR 文本", "stats": {"cache_hits": 0, "inferences": 1}}

            host = SimpleNamespace(data_dir=Path(folder), call_sync=call)
            proxy = OcrProcessProxy(host)
            self.assertEqual(proxy.ocr_image(raw, "text.bmp"), "OCR 文本")
            self.assertFalse(Path(sent["image_path"]).exists())

    def test_large_ocr_image_is_bounded_before_rpc(self) -> None:
        source = Image.new("RGB", (2048, 2048), "white")
        buffer = io.BytesIO()
        source.save(buffer, format="BMP")
        raw = buffer.getvalue()
        self.assertGreater(len(raw), 3 * 1024 * 1024)

        prepared, prepared_name = _prepare_rpc_image(
            raw,
            "slide.bmp",
            max_side=1600,
        )

        self.assertLessEqual(len(prepared), _RPC_IMAGE_TARGET_BYTES)
        self.assertTrue(prepared_name.endswith(".jpg"))
        with Image.open(io.BytesIO(prepared)) as image:
            self.assertLessEqual(max(image.size), 1600)


if __name__ == "__main__":
    unittest.main()
