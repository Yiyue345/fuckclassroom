from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fuckclassroom.plugins.ocr import engine
from fuckclassroom.plugins.ocr.engine import rapidocr_ocr
from fuckclassroom.plugins.ocr.services import OcrService
from fuckclassroom.core.config import AppConfig


class OcrRuntimeOptimizationTests(unittest.TestCase):
    def tearDown(self) -> None:
        for name in ("engine", "engine_key"):
            if hasattr(engine._RAPID_OCR_LOCAL, name):
                delattr(engine._RAPID_OCR_LOCAL, name)

    def test_rapidocr_receives_bounded_onnx_threads_and_disables_angle_cls(self) -> None:
        observed: dict[str, object] = {}

        class FakeRapidOCR:
            def __init__(self, **kwargs):
                observed["init"] = kwargs

            def __call__(self, image_bytes: bytes, **kwargs):
                observed["call"] = kwargs
                return [[[0, 0], "课堂文字", 0.95]], [0.1, 0.2, 0.3]

        fake_module = SimpleNamespace(RapidOCR=FakeRapidOCR)
        with patch.dict(sys.modules, {"rapidocr_onnxruntime": fake_module}):
            text = rapidocr_ocr(
                b"image",
                onnx_threads=1,
                max_side=1600,
                use_angle_cls=False,
            )

        self.assertEqual(text, "课堂文字")
        init = observed["init"]
        self.assertEqual(init["intra_op_num_threads"], 1)
        self.assertEqual(init["inter_op_num_threads"], 1)
        self.assertEqual(init["max_side_len"], 1600)
        self.assertEqual(init["det_limit_side_len"], 1600)
        self.assertEqual(init["det_limit_type"], "max")
        self.assertFalse(observed["call"]["use_cls"])

    def test_rapidocr_result_is_persistently_cached_by_image_and_output_config(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            settings_path = data_dir / "settings.json"
            settings_path.write_text(
                json.dumps(
                    {
                        "ocr_engine": "rapidocr",
                        "ocr_workers": 4,
                        "ocr_onnx_threads": 1,
                        "ocr_max_side": 1600,
                        "ocr_use_angle_cls": False,
                        "ocr_cache_enabled": True,
                    }
                ),
                encoding="utf-8",
            )
            service = OcrService(AppConfig(data_dir=data_dir))
            stats = {"cache_hits": 0, "inferences": 0}
            stats_lock = threading.Lock()

            with patch(
                "fuckclassroom.plugins.ocr.services.rapidocr_ocr",
                return_value="缓存课堂文字",
            ) as mocked:
                first = service.ocr_image(
                    b"same-image",
                    "first.jpg",
                    stats=stats,
                    stats_lock=stats_lock,
                )
                second = service.ocr_image(
                    b"same-image",
                    "renamed.jpg",
                    stats=stats,
                    stats_lock=stats_lock,
                )

            self.assertEqual(first, "缓存课堂文字")
            self.assertEqual(second, "缓存课堂文字")
            self.assertEqual(mocked.call_count, 1)
            self.assertEqual(stats["inferences"], 1)
            self.assertEqual(stats["cache_hits"], 1)
            cache_files = list((data_dir / "cache" / "ocr").rglob("*.txt"))
            self.assertEqual(len(cache_files), 1)
            self.assertEqual(cache_files[0].read_text(encoding="utf-8"), "缓存课堂文字")


if __name__ == "__main__":
    unittest.main()
