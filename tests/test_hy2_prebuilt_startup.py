from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fuckclassroom.config import AppConfig
from fuckclassroom.course_selection.proxy import Hy2ProxyManager


class Hy2PrebuiltStartupTests(unittest.TestCase):
    def test_enabled_hy2_uses_verified_prebuilt_without_cargo(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            data_dir = Path(directory)
            runner_name = "hy2_serve.exe" if os.name == "nt" else "hy2_serve"
            expected_runner = data_dir / "hy2" / runner_name

            with (
                patch.dict(os.environ, {"FUCKCLASSROOM_HY2_ENABLED": "1"}),
                patch("fuckclassroom.course_selection.proxy.shutil.which", return_value=None),
                patch("fuckclassroom.course_selection.proxy.subprocess.Popen") as popen,
            ):
                manager = Hy2ProxyManager(AppConfig(data_dir=data_dir))
                self.assertTrue(manager.wait_for_auto_build(1.0))
                status = manager.get_status()

            self.assertTrue(expected_runner.is_file())
            self.assertFalse(popen.called)
            self.assertFalse(status.building)
            self.assertIn("已就绪", status.message)


if __name__ == "__main__":
    unittest.main()
