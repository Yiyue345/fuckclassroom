from __future__ import annotations

import io
import tempfile
import unittest
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

from fuckclassroom.plugins.processing.extraction import extract_pptx_text


class PptImageDedupTests(unittest.TestCase):
    def test_only_referenced_unique_information_rich_images_are_ocrd(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            pptx = Path(directory) / "dedup.pptx"
            image_small = self._diagram(200, 100)
            image_large = self._diagram(400, 200)
            image_other = self._checkerboard(240, 120)
            image_other_duplicate = image_other
            image_unreferenced = self._solid(180, 90)

            with zipfile.ZipFile(pptx, "w") as archive:
                for index in range(1, 5):
                    archive.writestr(
                        f"ppt/slides/slide{index}.xml",
                        f'<root xmlns:a="urn:test"><a:t>第{index}页</a:t></root>',
                    )
                self._write_rel(archive, 1, "image1.png")
                self._write_rel(archive, 2, "image2.png")
                self._write_rel(archive, 3, "image3.png")
                self._write_rel(archive, 4, "image4.png")
                archive.writestr("ppt/media/image1.png", image_small)
                archive.writestr("ppt/media/image2.png", image_large)
                archive.writestr("ppt/media/image3.png", image_other)
                archive.writestr("ppt/media/image4.png", image_other_duplicate)
                archive.writestr("ppt/media/image5.png", image_unreferenced)

            calls: list[str] = []
            logs: list[tuple[str, str]] = []

            def fake_ocr(image_bytes: bytes, image_name: str) -> str:
                calls.append(image_name)
                return f"OCR:{image_name}"

            text, warnings = extract_pptx_text(
                pptx,
                fake_ocr,
                max_images=10,
                ocr_workers=2,
                event_log=lambda message, level="info": logs.append((level, message)),
            )

            self.assertFalse(warnings)
            self.assertEqual(set(calls), {"image2.png", "image3.png"})
            self.assertNotIn("image1.png", calls)
            self.assertNotIn("image4.png", calls)
            self.assertNotIn("image5.png", calls)
            self.assertIn("OCR:image2.png", text)
            self.assertIn("OCR:image3.png", text)
            summary = "\n".join(message for _, message in logs)
            self.assertIn("媒体 5 张", summary)
            self.assertIn("幻灯片引用 4 张", summary)
            self.assertIn("完全重复移除 1 张", summary)
            self.assertIn("高度相似移除 1 张", summary)
            self.assertIn("最终 OCR 2 张", summary)

    @staticmethod
    def _write_rel(archive: zipfile.ZipFile, slide_index: int, image_name: str) -> None:
        archive.writestr(
            f"ppt/slides/_rels/slide{slide_index}.xml.rels",
            (
                '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
                '<Relationship Id="rId1" '
                'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" '
                f'Target="../media/{image_name}"/>'
                "</Relationships>"
            ),
        )

    @staticmethod
    def _diagram(width: int, height: int) -> bytes:
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        draw.rectangle((width * 0.08, height * 0.2, width * 0.35, height * 0.8), outline="black", width=max(1, width // 100))
        draw.line((width * 0.35, height * 0.5, width * 0.7, height * 0.5), fill="black", width=max(1, width // 100))
        draw.rectangle((width * 0.7, height * 0.2, width * 0.92, height * 0.8), outline="black", width=max(1, width // 100))
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def _checkerboard(width: int, height: int) -> bytes:
        image = Image.new("RGB", (width, height), "white")
        draw = ImageDraw.Draw(image)
        block = max(8, width // 12)
        for y in range(0, height, block):
            for x in range(0, width, block):
                if ((x // block) + (y // block)) % 2 == 0:
                    draw.rectangle((x, y, min(width, x + block), min(height, y + block)), fill="black")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()

    @staticmethod
    def _solid(width: int, height: int) -> bytes:
        image = Image.new("RGB", (width, height), "gray")
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        return buffer.getvalue()


if __name__ == "__main__":
    unittest.main()
