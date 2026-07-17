"""Course-selection services with compatibility exports."""

from .assistant import CourseSelectionAssistant
from .helpers import *
from .models import (
    CaptureFile,
    CourseSelectionApiError,
    ProgressCallback,
    SELECTION_INFO_VIEWS,
    SelectionAccess,
    SelectionBatch,
    SelectionCandidate,
    SelectionInfoCache,
    SelectionTarget,
    WebVpnSessionStatus,
)

__all__ = [
    "CaptureFile",
    "CourseSelectionApiError",
    "CourseSelectionAssistant",
    "ProgressCallback",
    "SELECTION_INFO_VIEWS",
    "SelectionAccess",
    "SelectionBatch",
    "SelectionCandidate",
    "SelectionInfoCache",
    "SelectionTarget",
    "WebVpnSessionStatus",
    *[name for name in globals() if name.startswith("_") and not name.startswith("__")],
]

