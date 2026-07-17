"""Classroom platform client and lesson-processing services."""

from .client import (
    ClassroomClient,
    ClassroomClientError,
    Course,
    CourseDetail,
    ExportedFile,
    Lesson,
    LessonExportTarget,
    LessonResource,
    Term,
)

__all__ = [
    "ClassroomClient",
    "ClassroomClientError",
    "Course",
    "CourseDetail",
    "ExportedFile",
    "Lesson",
    "LessonExportTarget",
    "LessonResource",
    "Term",
]

