from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from fuckclassroom.core.settings import AppSettings, SettingsStore


class AutoSelectionSettingsTests(unittest.TestCase):
    def test_intervals_are_bounded(self) -> None:
        settings = AppSettings.from_dict(
            {
                "auto_selection_retry_seconds": 1,
                "auto_selection_session_retry_seconds": 99999,
                "auto_selection_capacity_retry_seconds": "invalid",
            }
        )

        self.assertEqual(settings.auto_selection_retry_seconds, 5)
        self.assertEqual(settings.auto_selection_session_retry_seconds, 3600)
        self.assertEqual(settings.auto_selection_capacity_retry_seconds, 30)

    def test_intervals_persist_from_settings_form(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            updated = store.update_from_form(
                {
                    "auto_selection_retry_seconds": "12",
                    "auto_selection_session_retry_seconds": "48",
                    "auto_selection_capacity_retry_seconds": "75",
                }
            )
            restored = store.load()

        self.assertEqual(updated.auto_selection_retry_seconds, 12)
        self.assertEqual(updated.auto_selection_session_retry_seconds, 48)
        self.assertEqual(updated.auto_selection_capacity_retry_seconds, 75)
        self.assertEqual(restored, updated)

    def test_catalog_lesson_targets_are_normalized_and_persisted(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            updated = store.update_from_form(
                {
                    "catalog_lesson_name_targets": "数据结构-01\n数据结构-01， 操作系统-02",
                }
            )
            restored = store.load()

        self.assertEqual(updated.catalog_lesson_name_targets, ("数据结构-01", "操作系统-02"))
        self.assertEqual(restored.catalog_lesson_name_targets, updated.catalog_lesson_name_targets)

    def test_legacy_hy2_source_path_is_removed_on_save(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            path = Path(directory) / "settings.json"
            path.write_text(
                json.dumps(
                    {
                        "hy2_enabled": True,
                        "hy2_source_dir": "E:/project/guethub/rust_module/rust",
                    }
                ),
                encoding="utf-8",
            )
            store = SettingsStore(path)

            store.update_from_form({"hy2_enabled": "on"})

            self.assertNotIn("hy2_source_dir", json.loads(path.read_text(encoding="utf-8")))


if __name__ == "__main__":
    unittest.main()
