from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fuckclassroom.course_selection.hy2_prebuilt import (
    install_prebuilt_runner,
    package_prebuilt_artifacts,
    source_fingerprint,
)


class Hy2PrebuiltTests(unittest.TestCase):
    def _source(self, root: Path) -> Path:
        source = root / "hy2_proxy"
        (source / "src").mkdir(parents=True)
        (source / "Cargo.toml").write_text("[package]\nname='hy2-test'\n", encoding="utf-8")
        (source / "Cargo.lock").write_text("# lock\n", encoding="utf-8")
        (source / "rust-toolchain.toml").write_text(
            '[toolchain]\nchannel = "1.96.0"\n', encoding="utf-8"
        )
        (source / "src" / "main.rs").write_text("fn main() {}\n", encoding="utf-8")
        return source

    def test_packages_and_installs_matching_runner(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = self._source(root)
            artifacts = root / "artifacts"
            artifact = artifacts / "hy2-windows-x86_64"
            artifact.mkdir(parents=True)
            (artifact / "hy2_serve.exe").write_bytes(b"verified-runner")
            output = source / "prebuilt"

            package_prebuilt_artifacts(artifacts, source, output)

            manifest = json.loads((output / "manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(manifest["schema"], 1)
            self.assertEqual(manifest["source_sha256"], source_fingerprint(source))
            self.assertIn("windows-x86_64", manifest["runners"])

            runner = root / "data" / "hy2" / "hy2_serve.exe"
            self.assertTrue(
                install_prebuilt_runner(
                    source,
                    runner,
                    platform_key="windows-x86_64",
                )
            )
            self.assertEqual(runner.read_bytes(), b"verified-runner")

    def test_rejects_prebuilt_after_source_changes(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            root = Path(directory)
            source = self._source(root)
            artifacts = root / "artifacts"
            artifact = artifacts / "hy2-linux-x86_64"
            artifact.mkdir(parents=True)
            (artifact / "hy2_serve").write_bytes(b"old-runner")
            output = source / "prebuilt"
            package_prebuilt_artifacts(artifacts, source, output)

            (source / "src" / "main.rs").write_text("fn main() { println!(\"new\"); }\n", encoding="utf-8")
            runner = root / "data" / "hy2" / "hy2_serve"

            self.assertFalse(
                install_prebuilt_runner(
                    source,
                    runner,
                    platform_key="linux-x86_64",
                )
            )
            self.assertFalse(runner.exists())


if __name__ == "__main__":
    unittest.main()
