from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from fuckclassroom.plugins.transcription.environment import detect_asr_runtime


class AsrEnvironmentTests(unittest.TestCase):
    def _fake_ctranslate2(self):
        return SimpleNamespace(
            __file__=str(Path("fake") / "ctranslate2" / "__init__.py"),
            __version__="4.8.2",
            get_cuda_device_count=lambda: 1,
            get_supported_compute_types=lambda device: (
                {"int8", "float32"}
                if device == "cpu"
                else {"int8", "int8_float16", "float16", "float32"}
            ),
        )

    def test_cuda_is_disabled_when_cublas_is_missing(self) -> None:
        fake = self._fake_ctranslate2()

        def loadable(filename: str, package_dir: Path) -> bool:
            del package_dir
            return filename != "cublas64_12.dll"

        with patch.dict(sys.modules, {"ctranslate2": fake}), patch(
            "fuckclassroom.plugins.transcription.environment.sys.platform",
            "win32",
        ), patch(
            "fuckclassroom.plugins.transcription.environment._loadable_windows_library",
            side_effect=loadable,
        ):
            capabilities = detect_asr_runtime()

        self.assertFalse(capabilities.cuda_available)
        self.assertEqual(capabilities.auto_device, "cpu")
        self.assertEqual(capabilities.cuda_compute_types, ())
        self.assertIn("CUDA 12 cuBLAS", capabilities.cuda_reason)

    def test_cuda_exposes_supported_compute_types_when_runtime_is_complete(self) -> None:
        fake = self._fake_ctranslate2()
        with patch.dict(sys.modules, {"ctranslate2": fake}), patch(
            "fuckclassroom.plugins.transcription.environment.sys.platform",
            "win32",
        ), patch(
            "fuckclassroom.plugins.transcription.environment._loadable_windows_library",
            return_value=True,
        ):
            capabilities = detect_asr_runtime()

        self.assertTrue(capabilities.cuda_available)
        self.assertEqual(capabilities.auto_device, "cuda")
        self.assertIn("float16", capabilities.cuda_compute_types)
        self.assertIn("int8", capabilities.cpu_compute_types)


if __name__ == "__main__":
    unittest.main()
