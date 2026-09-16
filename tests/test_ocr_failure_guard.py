from __future__ import annotations

import unittest

from fuckclassroom.classroom.extraction import ExtractionError, _run_ocr_jobs


class OcrFailureGuardTests(unittest.TestCase):
    def test_total_ocr_failure_fails_the_extraction_stage(self) -> None:
        jobs = [
            (1, "a.png", b"a"),
            (2, "b.png", b"b"),
        ]

        def always_fail(image_bytes: bytes, image_name: str) -> str:
            raise KeyError("model_path")

        with self.assertRaisesRegex(ExtractionError, "OCR 全部失败"):
            _run_ocr_jobs(
                jobs,
                always_fail,
                max_workers=2,
                progress=None,
                cancel_check=None,
            )

    def test_partial_ocr_failure_keeps_useful_results(self) -> None:
        jobs = [
            (1, "ok.png", b"ok"),
            (2, "bad.png", b"bad"),
        ]

        def partly_fail(image_bytes: bytes, image_name: str) -> str:
            if image_name == "bad.png":
                raise RuntimeError("broken image")
            return "可用文字"

        results, warnings = _run_ocr_jobs(
            jobs,
            partly_fail,
            max_workers=2,
            progress=None,
            cancel_check=None,
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0][2], "可用文字")
        self.assertEqual(len(warnings), 1)
        self.assertIn("bad.png OCR 失败", warnings[0])


if __name__ == "__main__":
    unittest.main()
