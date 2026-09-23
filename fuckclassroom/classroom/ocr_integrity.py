"""Backward-compatible OCR guard hook.

The all-images-failed guard now lives directly in the processing extraction engine.
"""


def install_ocr_failure_guard() -> None:
    return None


__all__ = ["install_ocr_failure_guard"]
