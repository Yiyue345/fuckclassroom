from __future__ import annotations

import base64
import unittest

from fuckclassroom.plugins.ai_summary.services import AiSummaryProcessProxy


class _Host:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, object]]] = []

    def call_sync(self, method, params=None, **_kwargs):
        payload = dict(params or {})
        self.calls.append((method, payload))
        return "识别结果"


class AiSummaryServiceTests(unittest.TestCase):
    def test_ocr_proxy_encodes_binary_payload(self) -> None:
        host = _Host()
        proxy = AiSummaryProcessProxy(host)  # type: ignore[arg-type]

        result = proxy.ocr_image(b"image-bytes", "image/png")

        self.assertEqual(result, "识别结果")
        self.assertEqual(host.calls[0][0], "ai_summary.ocr")
        self.assertEqual(
            host.calls[0][1]["image_b64"],
            base64.b64encode(b"image-bytes").decode("ascii"),
        )
        self.assertEqual(host.calls[0][1]["media_type"], "image/png")


if __name__ == "__main__":
    unittest.main()
