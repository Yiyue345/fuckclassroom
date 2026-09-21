"""Compatibility imports for the transcription plugin.

New code should import from fuckclassroom.plugins.transcription.engine.
"""

from ..plugins.transcription.engine import *  # noqa: F401,F403
from ..plugins.transcription import engine as _impl

_remote_media_supports_ranges = _impl._remote_media_supports_ranges
_decode_audio_chunk = _impl._decode_audio_chunk
