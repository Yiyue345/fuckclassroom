from __future__ import annotations

from functools import wraps
from typing import Any


def install_ocr_failure_guard() -> None:
    """Make a total OCR backend failure fail the extraction task.

    The extraction helper intentionally tolerates individual image failures so one
    corrupt PPT asset does not discard all useful OCR output. However, if every
    scheduled image raises, reporting the whole extraction task as successful is
    misleading and hides configuration/backend failures. Wrap the helper once and
    promote only that all-failed condition to ExtractionError.
    """
    from . import extraction

    original = extraction._run_ocr_jobs
    if getattr(original, "_fuckclassroom_failure_guard", False):
        return

    @wraps(original)
    def guarded_run_ocr_jobs(
        jobs: list[tuple[int, str, bytes]],
        ocr_image,
        *,
        max_workers: int,
        progress,
        cancel_check,
    ):
        results, warnings = original(
            jobs,
            ocr_image,
            max_workers=max_workers,
            progress=progress,
            cancel_check=cancel_check,
        )
        if jobs and not results and len(warnings) >= len(jobs):
            first_error = warnings[0] if warnings else "未知 OCR 后端错误"
            raise extraction.ExtractionError(
                f"课件 OCR 全部失败（{len(jobs)}/{len(jobs)}）：{first_error}"
            )
        return results, warnings

    guarded_run_ocr_jobs._fuckclassroom_failure_guard = True  # type: ignore[attr-defined]
    extraction._run_ocr_jobs = guarded_run_ocr_jobs
