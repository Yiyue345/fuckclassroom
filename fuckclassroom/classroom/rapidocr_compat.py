"""Compatibility imports for the OCR plugin.

New code should import from fuckclassroom.plugins.ocr.compat.
"""

from ..plugins.ocr.compat import *  # noqa: F401,F403
from ..plugins.ocr import compat as _impl

_resize_byte_image = _impl._resize_byte_image
