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
    Term,
)
from .ocr_integrity import install_ocr_failure_guard

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
    "Term",
]
