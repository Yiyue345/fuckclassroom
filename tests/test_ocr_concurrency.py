from __future__ import annotations

import tempfile
import threading
import time
import unittest
import zipfile
from pathlib import Path

from fuckclassroom.plugins.processing.extraction import extract_pptx_text


class OcrConcurrencyTests(unittest.TestCase):
    def test_ppt_images_are_ocrd_concurrently_and_kept_in_source_order(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pptx = Path(directory) / "test.pptx"
            with zipfile.ZipFile(pptx, "w") as archive:
                archive.writestr(
                    "ppt/slides/slide1.xml",
                    '<root xmlns:a="urn:test"><a:t>课件正文</a:t></root>',
                )
                for index in range(1, 7):
                    archive.writestr(f"ppt/media/image{index}.png", f"image-{index}".encode())

            lock = threading.Lock()
            active = 0
            peak = 0
            progress: list[tuple[int, int, str]] = []

            def fake_ocr(image_bytes: bytes, image_name: str) -> str:
                nonlocal active, peak
                with lock:
                    active += 1
                    peak = max(peak, active)
                try:
                    time.sleep(0.05)
                    return image_bytes.decode()
                finally:
                    with lock:
                        active -= 1

            text, warnings = extract_pptx_text(
                pptx,
                fake_ocr,
                max_images=10,
                progress=lambda done, total, name: progress.append((done, total, name)),
                ocr_workers=3,
            )

            self.assertGreaterEqual(peak, 2)
            self.assertFalse(warnings)
            self.assertEqual(len(progress), 6)
            self.assertIn("课件正文", text)
            positions = [text.index(f"## 图片 {index}: image{index}.png") for index in range(1, 7)]
            self.assertEqual(positions, sorted(positions))


if __name__ == "__main__":
    unittest.main()
