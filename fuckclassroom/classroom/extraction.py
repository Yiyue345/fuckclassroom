"""Compatibility imports for the processing plugin.

New code should import from fuckclassroom.plugins.processing.extraction.
"""

from ..plugins.processing.extraction import *  # noqa: F401,F403
from ..plugins.processing import extraction as _impl
from ..plugins.ocr import engine as _ocr_engine

# Private helpers were historically imported by tests and downstream tools.
_platform_transcript_is_usable = _impl._platform_transcript_is_usable
_run_ocr_jobs = _impl._run_ocr_jobs
_rapidocr_ocr = _ocr_engine._rapidocr_ocr
_increment_ocr_stat = _ocr_engine._increment_ocr_stat
_tesseract_ocr = _ocr_engine._tesseract_ocr
_write_cache_text = _ocr_engine._write_cache_text
