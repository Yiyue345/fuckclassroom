from __future__ import annotations

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


if __name__ == "__main__":
    unittest.main()
