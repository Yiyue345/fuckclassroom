from __future__ import annotations

import unittest
from pathlib import Path


class Hy2ToolchainTests(unittest.TestCase):
    def test_hy2_rust_toolchain_is_pinned_to_smart_app_control_compatible_version(self) -> None:
        path = Path(__file__).resolve().parents[1] / "rust_module" / "hy2_proxy" / "rust-toolchain.toml"
        content = path.read_text(encoding="utf-8")

        self.assertIn('channel = "1.96.0"', content)
        self.assertIn('profile = "minimal"', content)


if __name__ == "__main__":
    unittest.main()
