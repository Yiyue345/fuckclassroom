"""Classroom platform client and course playback services."""

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
from .semester_sync import SemesterSnapshot, SemesterSyncService


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
