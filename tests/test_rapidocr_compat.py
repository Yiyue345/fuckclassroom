from __future__ import annotations

import io
import unittest

from PIL import Image

from fuckclassroom.classroom.rapidocr_compat import _resize_byte_image, install_rapidocr_compat


class RapidOcrCompatTests(unittest.TestCase):
    def test_legacy_constructor_accepts_project_optimization_options(self) -> None:
        install_rapidocr_compat()
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR(
            max_side_len=1600,
            det_limit_side_len=1600,
            det_limit_type="max",
            intra_op_num_threads=1,
            inter_op_num_threads=1,
            print_verbose=False,
        )

        self.assertEqual(getattr(engine, "_fuckclassroom_max_side", None), 1600)

    def test_large_byte_image_is_resized_before_legacy_engine(self) -> None:
        source = Image.new("RGB", (2400, 1200), "white")
        buffer = io.BytesIO()
        source.save(buffer, format="PNG")

        resized = _resize_byte_image(buffer.getvalue(), 800)

        self.assertEqual(tuple(resized.shape[:2]), (400, 800))


if __name__ == "__main__":
    unittest.main()
