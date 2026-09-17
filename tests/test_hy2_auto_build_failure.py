from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fuckclassroom.config import AppConfig
from fuckclassroom.course_selection.proxy import Hy2ProxyManager


class Hy2AutoBuildFailureTests(unittest.TestCase):
    def _config(self, data_dir: Path) -> AppConfig:
        data_dir.mkdir(parents=True, exist_ok=True)
        (data_dir / "settings.json").write_text(
            json.dumps({"hy2_enabled": True}),
            encoding="utf-8",
        )
        return AppConfig(data_dir=data_dir)

    def test_missing_cargo_failure_is_not_erased_by_status_reads(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = self._config(Path(directory))
            with (
                patch(
                    "fuckclassroom.course_selection.hy2_prebuilt.install_prebuilt_runner",
                    return_value=False,
                ),
                patch("fuckclassroom.course_selection.proxy.shutil.which", return_value=None),
            ):
                manager = Hy2ProxyManager(config)
                self.assertTrue(manager.wait_for_auto_build(2.0))

                first = manager.get_status()
                second = manager.get_status()

            self.assertFalse(first.building)
            self.assertIn("自动构建失败", first.message)
            self.assertIn("没有找到 Cargo", first.message)
            self.assertEqual(second.message, first.message)
            self.assertFalse(second.building)

    def test_unexpected_build_exception_is_exposed_in_status(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = self._config(Path(directory))
            with (
                patch(
                    "fuckclassroom.course_selection.hy2_prebuilt.install_prebuilt_runner",
                    return_value=False,
                ),
                patch.object(
                    Hy2ProxyManager,
                    "_compile_runner",
                    side_effect=ValueError("boom"),
                ),
            ):
                manager = Hy2ProxyManager(config)
                self.assertTrue(manager.wait_for_auto_build(2.0))
                status = manager.get_status()

            self.assertFalse(status.building)
            self.assertIn("自动构建失败", status.message)
            self.assertIn("ValueError: boom", status.message)


if __name__ == "__main__":
    unittest.main()
