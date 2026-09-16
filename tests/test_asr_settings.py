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
                    "ocr_onnx_threads": "2",
                    "ocr_max_side": "1920",
                    "ocr_use_angle_cls": "",
                    "ocr_cache_enabled": "on",
                    "asr_model": "small",
                    "asr_device": "cpu",
                    "asr_compute_type": "int8",
                    "asr_cpu_threads": "6",
                    "asr_num_workers": "3",
                    "asr_max_concurrent_tasks": "3",
                    "audio_prefetch_queue_size": "6",
                    "asr_batch_size": "12",
                    "asr_use_batched": "on",
                    "asr_simplified_chinese": "on",
                }
            )

            self.assertEqual(settings.ocr_workers, 5)
            self.assertEqual(settings.ocr_onnx_threads, 2)
            self.assertEqual(settings.ocr_max_side, 1920)
            self.assertFalse(settings.ocr_use_angle_cls)
            self.assertTrue(settings.ocr_cache_enabled)
            self.assertEqual(settings.asr_cpu_threads, 6)
            self.assertEqual(settings.asr_num_workers, 3)
            self.assertEqual(settings.asr_max_concurrent_tasks, 3)
            self.assertEqual(settings.audio_prefetch_queue_size, 6)
            self.assertEqual(settings.asr_batch_size, 12)
            self.assertTrue(settings.asr_use_batched)
            self.assertTrue(settings.asr_simplified_chinese)

            config = AppConfig(data_dir=data_dir)
            self.assertEqual(config.ocr_workers, 5)
            self.assertEqual(config.ocr_onnx_threads, 2)
            self.assertEqual(config.ocr_max_side, 1920)
            self.assertFalse(config.ocr_use_angle_cls)
            self.assertTrue(config.ocr_cache_enabled)
            self.assertEqual(config.asr_cpu_threads, 6)
            self.assertEqual(config.asr_num_workers, 3)
            self.assertEqual(config.asr_max_concurrent_tasks, 3)
            self.assertEqual(config.audio_prefetch_queue_size, 6)
            self.assertEqual(config.asr_batch_size, 12)

    def test_audio_prefetch_queue_size_defaults_and_is_bounded(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            data_dir = Path(directory)
            config = AppConfig(data_dir=data_dir)
            self.assertEqual(config.audio_prefetch_queue_size, 3)

            store = SettingsStore(data_dir / "settings.json")
            self.assertEqual(
                store.update_from_form({"audio_prefetch_queue_size": "99"}).audio_prefetch_queue_size,
                8,
            )
            self.assertEqual(
                store.update_from_form({"audio_prefetch_queue_size": "0"}).audio_prefetch_queue_size,
                1,
            )

    def test_asr_and_ocr_checkboxes_can_be_disabled(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            store = SettingsStore(Path(directory) / "settings.json")
            settings = store.update_from_form(
                {
                    "ocr_use_angle_cls": "",
                    "ocr_cache_enabled": "",
                    "asr_use_batched": "",
                    "asr_simplified_chinese": "",
                }
            )
            self.assertFalse(settings.ocr_use_angle_cls)
            self.assertFalse(settings.ocr_cache_enabled)
            self.assertFalse(settings.asr_use_batched)
            self.assertFalse(settings.asr_simplified_chinese)


if __name__ == "__main__":
    unittest.main()
