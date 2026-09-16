from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fuckclassroom.core.config import AppConfig
from fuckclassroom.core.settings import SettingsStore


class AsrSettingsTests(unittest.TestCase):
    def test_asr_performance_settings_round_trip_from_form(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            store = SettingsStore(data_dir / "settings.json")
            settings = store.update_from_form(
                {
                    "ocr_workers": "5",
                    "asr_model": "small",
                    "asr_device": "cpu",
                    "asr_compute_type": "int8",
                    "asr_cpu_threads": "6",
                    "asr_num_workers": "3",
                    "asr_max_concurrent_tasks": "3",
                    "asr_batch_size": "12",
                    "asr_use_batched": "on",
                    "asr_simplified_chinese": "on",
                }
            )

            self.assertEqual(settings.ocr_workers, 5)
            self.assertEqual(settings.asr_cpu_threads, 6)
            self.assertEqual(settings.asr_num_workers, 3)
            self.assertEqual(settings.asr_max_concurrent_tasks, 3)
            self.assertEqual(settings.asr_batch_size, 12)
            self.assertTrue(settings.asr_use_batched)
            self.assertTrue(settings.asr_simplified_chinese)

            config = AppConfig(data_dir=data_dir)
            self.assertEqual(config.ocr_workers, 5)
            self.assertEqual(config.asr_cpu_threads, 6)
            self.assertEqual(config.asr_num_workers, 3)
            self.assertEqual(config.asr_max_concurrent_tasks, 3)
            self.assertEqual(config.asr_batch_size, 12)

    def test_asr_checkboxes_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            settings = store.update_from_form(
                {
                    "asr_use_batched": "",
                    "asr_simplified_chinese": "",
                }
            )
            self.assertFalse(settings.asr_use_batched)
            self.assertFalse(settings.asr_simplified_chinese)


if __name__ == "__main__":
    unittest.main()
