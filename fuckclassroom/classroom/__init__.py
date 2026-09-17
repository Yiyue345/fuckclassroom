"""Classroom platform client and lesson-processing services."""

from .rapidocr_compat import install_rapidocr_compat

install_rapidocr_compat()

from .client import (
    ClassroomClient,
    ClassroomClientError,
    Course,
    CourseDetail,
    ExportedFile,
    Lesson,
    LessonExportTarget,
    LessonResource,
    LiveLesson,
    LiveLessonInfo,
    LivePptSlide,
    Term,
    is_allowed_live_media_url,
    rewrite_live_playlist,
)
from .ocr_integrity import install_ocr_failure_guard
from .semester_sync import SemesterSnapshot, SemesterSyncService

install_ocr_failure_guard()

__all__ = [
    "ClassroomClient",
    "ClassroomClientError",
    "Course",
    "CourseDetail",
    "ExportedFile",
    "Lesson",
    "LessonExportTarget",
    "LessonResource",
    "LiveLesson",
    "LiveLessonInfo",
    "LivePptSlide",
    "Term",
    "is_allowed_live_media_url",
    "rewrite_live_playlist",
    "SemesterSnapshot",
    "SemesterSyncService",
]
