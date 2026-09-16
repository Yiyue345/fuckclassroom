from __future__ import annotations

import html
import mimetypes
import shutil
import subprocess
import tempfile
import threading
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from .ai import AIClient, AIClientError
from .client import ClassroomClient, ClassroomClientError, CourseDetail, Lesson, LessonResource
from .transcription import LocalTranscriptionError, LocalTranscriptionService
from ..core.config import AppConfig


class ExtractionError(RuntimeError):
    pass


ProgressCallback = Callable[[int, str], None]
CancelCheck = Callable[[], None]


@dataclass(frozen=True)
class ExtractionResult:
    course_id: str
    lesson_id: str
    output_dir: Path
    transcript_path: Path
    ppt_text_path: Path
    combined_path: Path
    summary_path: Path | None
    warnings: list[str]


class LessonExtractionService:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()
        self.classroom = ClassroomClient(self.config)
        self.ai = AIClient(self.config)
        self.transcription = LocalTranscriptionService(self.config)

    def extract_lesson(
        self,
        course_id: str,
        lesson_id: str,
        run_ocr: bool = True,
        progress: ProgressCallback | None = None,
    ) -> ExtractionResult:
        _report(progress, 5, "正在读取课次信息")
        detail = self.classroom.get_course_detail(course_id)
        lesson = _find_lesson(detail, lesson_id)
        work_dir = self._lesson_dir(self.config.downloads_dir, detail, lesson)
        output_dir = self._lesson_dir(self.config.outputs_dir, detail, lesson)
        work_dir.mkdir(parents=True, exist_ok=True)
        output_dir.mkdir(parents=True, exist_ok=True)

        warnings: list[str] = []
        transcript_path = output_dir / "transcript.md"
        transcript_text = ""

        platform_transcript = _find_resource(lesson, "transcript")
        if platform_transcript is not None:
            _report(progress, 12, "正在检查平台转写")
            try:
                transcript_file, transcript_from_cache = self._download_export(
                    course_id, lesson_id, "transcript", work_dir
                )
                if transcript_from_cache:
                    _report(progress, 16, "已复用本地平台转写")
                else:
                    _report(progress, 16, "平台转写已下载")
                transcript_text = extract_docx_text(transcript_file)
                if transcript_text.strip():
                    transcript_path.write_text(
                        _section("平台转写", transcript_text), encoding="utf-8"
                    )
                else:
                    warning = "平台转写为空，已尝试改用本地语音转写。"
                    warnings.append(warning)
                    _task_log(progress, warning, "warning")
            except (
                ClassroomClientError,
                OSError,
                zipfile.BadZipFile,
                ET.ParseError,
            ) as exc:
                warning = f"平台转写不可用，已尝试改用本地语音转写：{exc}"
                warnings.append(warning)
                _task_log(progress, warning, "warning")
        else:
            warning = "平台未提供可用转写，已尝试改用本地语音转写。"
            warnings.append(warning)
            _task_log(progress, warning, "warning")

        if not transcript_text.strip():
            video_resource = _find_resource(lesson, "video")
            if video_resource is not None and video_resource.url:
                def asr_progress(value: int, message: str) -> None:
                    _report(progress, 18 + int(value * 0.34), message)

                try:
                    transcript_path = self._transcribe_lesson(
                        detail,
                        lesson,
                        progress=asr_progress,
                    )
                    transcript_text = transcript_path.read_text(encoding="utf-8")
                except LocalTranscriptionError as exc:
                    warning = str(exc)
                    warnings.append(warning)
                    _task_log(progress, warning, "warning")
                    transcript_path.write_text(
                        _section("语音转写", "（本地语音转写失败）"), encoding="utf-8"
                    )
            else:
                warning = "本课次没有可用录像，无法执行本地语音转写。"
                warnings.append(warning)
                _task_log(progress, warning, "warning")
                transcript_path.write_text(
                    _section("语音转写", "（本课次暂无可用转写或录像）"), encoding="utf-8"
                )

        ppt_text_path = output_dir / "ppt_ocr.md"
        ppt_text = ""
        ppt_warnings: list[str] = []
        ppt_resource = _find_resource(lesson, "ppt")

        if ppt_resource is not None:
            _report(progress, 55, "正在检查课件")
            try:
                ppt_file, ppt_from_cache = self._download_export(
                    course_id, lesson_id, "ppt", work_dir
                )
                if ppt_from_cache:
                    _report(progress, 58, "已复用本地课件")
                else:
                    _report(progress, 58, "课件已下载")
                ocr_callback = self._ocr_image if run_ocr and self._can_ocr(ppt_warnings) else None

                def ppt_progress(done: int, total: int, image_name: str) -> None:
                    if total <= 0:
                        _report(progress, 64, "正在读取课件文本")
                        return
                    percent = 60 + int((done / total) * 25)
                    _report(progress, percent, f"正在 OCR 课件图片 {done}/{total}：{image_name}")

                _report(
                    progress,
                    60,
                    f"正在提取课件文字（OCR Worker {self.config.ocr_workers}）",
                )
                _task_log(
                    progress,
                    f"PPT OCR 配置：engine={self.config.ocr_engine}, workers={self.config.ocr_workers}, max_images={self.config.ocr_max_images}",
                    "debug",
                )
                cancel_check = getattr(progress, "raise_if_cancelled", None)
                ppt_text, ocr_warnings = extract_pptx_text(
                    ppt_file,
                    ocr_callback,
                    self.config.ocr_max_images,
                    progress=ppt_progress,
                    ocr_workers=self.config.ocr_workers,
                    cancel_check=cancel_check if callable(cancel_check) else None,
                )
                ppt_warnings.extend(ocr_warnings)
                for warning in ocr_warnings:
                    _task_log(progress, warning, "warning")
            except (
                ClassroomClientError,
                OSError,
                zipfile.BadZipFile,
                ET.ParseError,
            ) as exc:
                warning = f"课件提取失败：{exc}"
                ppt_warnings.append(warning)
                _task_log(progress, warning, "warning")
        else:
            warning = "本课次没有可用 PPT，已跳过课件文字提取。"
            ppt_warnings.append(warning)
            _task_log(progress, warning, "warning")

        warnings.extend(ppt_warnings)
        warning_text = "\n".join(f"- {item}" for item in ppt_warnings)
        ppt_body = ppt_text
        if warning_text:
            ppt_body = f"{ppt_body}\n\n## 提醒\n{warning_text}".strip()
        ppt_text_path.write_text(_section("课件 OCR", ppt_body), encoding="utf-8")

        _report(progress, 90, "正在合并提取结果")
        combined_text = "\n\n".join(
            [
                f"# {detail.course.title} - {lesson.title}",
                transcript_path.read_text(encoding="utf-8"),
                ppt_text_path.read_text(encoding="utf-8"),
            ]
        )
        if warnings:
            combined_text += "\n\n## 提取提醒\n" + "\n".join(
                f"- {item}" for item in warnings
            )
        combined_path = output_dir / "combined.md"
        combined_path.write_text(combined_text, encoding="utf-8")
        _report(progress, 100, "文字提取完成")

        return ExtractionResult(
            course_id=course_id,
            lesson_id=lesson_id,
            output_dir=output_dir,
            transcript_path=transcript_path,
            ppt_text_path=ppt_text_path,
            combined_path=combined_path,
            summary_path=output_dir / "summary.md" if (output_dir / "summary.md").exists() else None,
            warnings=warnings,
        )

    def transcribe_lesson(
        self,
        course_id: str,
        lesson_id: str,
        progress: ProgressCallback | None = None,
        *,
        force: bool = False,
    ) -> Path:
        _report(progress, 5, "正在读取课次信息")
        detail = self.classroom.get_course_detail(course_id)
        lesson = _find_lesson(detail, lesson_id)
        return self._transcribe_lesson(detail, lesson, progress=progress, force=force)

    def _transcribe_lesson(
        self,
        detail: CourseDetail,
        lesson: Lesson,
        progress: ProgressCallback | None = None,
        *,
        force: bool = False,
    ) -> Path:
        video_resource = _find_resource(lesson, "video")
        if video_resource is None or not video_resource.url:
            raise LocalTranscriptionError("本课次没有可用的课堂录像")

        output_dir = self._lesson_dir(self.config.outputs_dir, detail, lesson)
        output_dir.mkdir(parents=True, exist_ok=True)
        cache_path = output_dir / "local_transcript.txt"
        result = self.transcription.transcribe_url(
            video_resource.url,
            cache_path,
            progress=progress,
            force=force,
        )
        transcript_path = output_dir / "transcript.md"
        transcript_path.write_text(
            _section("本地语音转写", result.text), encoding="utf-8"
        )
        return transcript_path

    def summarize_lesson(
        self,
        course_id: str,
        lesson_id: str,
        progress: ProgressCallback | None = None,
    ) -> Path:
        _report(progress, 10, "正在读取课次信息")
        detail = self.classroom.get_course_detail(course_id)
        lesson = _find_lesson(detail, lesson_id)
        output_dir = self._lesson_dir(self.config.outputs_dir, detail, lesson)
        combined_path = output_dir / "combined.md"
        if not combined_path.exists():
            raise ExtractionError("请先提取文字，再生成总结")
        _report(progress, 35, "正在读取合并文本")
        combined_text = combined_path.read_text(encoding="utf-8")
        _report(progress, 55, "正在请求 AI 生成总结")
        summary = self.ai.summarize_course_text(combined_text)
        summary_path = output_dir / "summary.md"
        _report(progress, 90, "正在写入总结")
        summary_path.write_text(_section("AI 课程总结", summary), encoding="utf-8")
        _report(progress, 100, "AI 总结完成")
        return summary_path

    def get_existing_outputs(self, course_id: str, lesson_id: str) -> dict[str, str]:
        detail = self.classroom.get_course_detail(course_id)
        lesson = _find_lesson(detail, lesson_id)
        output_dir = self._lesson_dir(self.config.outputs_dir, detail, lesson)
        result: dict[str, str] = {}
        for name in ("transcript.md", "ppt_ocr.md", "combined.md", "summary.md"):
            path = output_dir / name
            if path.exists():
                result[name] = path.read_text(encoding="utf-8")
        return result

    def _download_export(self, course_id: str, lesson_id: str, kind: str, work_dir: Path) -> tuple[Path, bool]:
        target = self.classroom.get_lesson_export_target(course_id, lesson_id, kind)
        if target.exists:
            return target.path, True
        exported = self.classroom.download_lesson_export(course_id, lesson_id, kind)
        return exported.saved_path, exported.from_cache

    def _ocr_image(self, image_bytes: bytes, image_name: str) -> str:
        engine = self.config.ocr_engine
        if engine in ("auto", "rapidocr"):
            try:
                return _rapidocr_ocr(image_bytes)
            except ImportError:
                if engine == "rapidocr":
                    raise ExtractionError("缺少 rapidocr-onnxruntime，请先安装项目依赖") from None

        if engine in ("auto", "tesseract") and shutil.which("tesseract"):
            return _tesseract_ocr(image_bytes, image_name)

        if engine in ("auto", "ai"):
            media_type = mimetypes.guess_type(image_name)[0] or "image/png"
            return self.ai.ocr_image(image_bytes, media_type)

        raise ExtractionError(
            "未找到可用 OCR 引擎。请安装 Tesseract，或配置 FUCKCLASSROOM_AI_API_KEY 使用 AI 视觉 OCR。"
        )

    def _can_ocr(self, warnings: list[str]) -> bool:
        engine = self.config.ocr_engine
        if engine in ("auto", "rapidocr"):
            try:
                import rapidocr_onnxruntime  # noqa: F401

                return True
            except ImportError:
                if engine == "rapidocr":
                    warnings.append("已跳过 OCR：缺少 rapidocr-onnxruntime。")
                    return False
        if engine in ("auto", "tesseract") and shutil.which("tesseract"):
            return True
        if engine in ("auto", "ai") and self.config.ai_api_key:
            return True
        warnings.append("已跳过 OCR：未找到 Tesseract，也没有配置 FUCKCLASSROOM_AI_API_KEY。")
        return False

    @staticmethod
    def _lesson_dir(root: Path, detail: CourseDetail, lesson: Lesson) -> Path:
        return root / _safe_path(detail.course.title) / _safe_path(lesson.title)


def extract_docx_text(path: Path) -> str:
    texts: list[str] = []
    with zipfile.ZipFile(path) as archive:
        names = [
            name
            for name in archive.namelist()
            if name.startswith("word/") and name.endswith(".xml")
        ]
        for name in names:
            if not any(part in name for part in ("document", "header", "footer")):
                continue
            root = ET.fromstring(archive.read(name))
            texts.extend(_xml_texts(root))
    return "\n".join(line for line in texts if line.strip()).strip()


def extract_pptx_text(
    path: Path,
    ocr_image: Callable[[bytes, str], str] | None,
    max_images: int = 120,
    progress: Callable[[int, int, str], None] | None = None,
    *,
    ocr_workers: int = 1,
    cancel_check: CancelCheck | None = None,
) -> tuple[str, list[str]]:
    lines: list[str] = []
    warnings: list[str] = []
    image_jobs: list[tuple[int, str, bytes]] = []

    _check_cancel(cancel_check)
    with zipfile.ZipFile(path) as archive:
        slide_names = sorted(
            name for name in archive.namelist() if name.startswith("ppt/slides/slide") and name.endswith(".xml")
        )
        for index, name in enumerate(slide_names, start=1):
            _check_cancel(cancel_check)
            root = ET.fromstring(archive.read(name))
            slide_text = "\n".join(_xml_texts(root)).strip()
            if slide_text:
                lines.append(f"## 第 {index} 页 XML 文本\n{slide_text}")

        image_names = sorted(
            name for name in archive.namelist() if name.startswith("ppt/media/") and _is_image_name(name)
        )
        if image_names and ocr_image is None:
            warnings.append(f"课件中检测到 {len(image_names)} 张图片，未执行 OCR。")
            return "\n\n".join(lines).strip(), warnings

        limited_image_names = image_names[:max_images]
        if len(image_names) > max_images:
            warnings.append(f"课件中共有 {len(image_names)} 张图片，本次只 OCR 前 {max_images} 张。")

        # Read image bytes serially from ZipFile, then close the archive before running OCR
        # in parallel. ZipFile itself is not used concurrently by worker threads.
        for index, name in enumerate(limited_image_names, start=1):
            _check_cancel(cancel_check)
            image_jobs.append((index, name, archive.read(name)))

    if ocr_image is not None and image_jobs:
        results, ocr_warnings = _run_ocr_jobs(
            image_jobs,
            ocr_image,
            max_workers=max(1, min(int(ocr_workers), len(image_jobs))),
            progress=progress,
            cancel_check=cancel_check,
        )
        warnings.extend(ocr_warnings)
        for index, image_name, text in sorted(results, key=lambda item: item[0]):
            if text:
                lines.append(f"## 图片 {index}: {Path(image_name).name}\n{text}")

    if not lines and not warnings:
        warnings.append("课件中没有检测到可提取的文本或图片")
    return "\n\n".join(lines).strip(), warnings


def _run_ocr_jobs(
    jobs: list[tuple[int, str, bytes]],
    ocr_image: Callable[[bytes, str], str],
    *,
    max_workers: int,
    progress: Callable[[int, int, str], None] | None,
    cancel_check: CancelCheck | None,
) -> tuple[list[tuple[int, str, str]], list[str]]:
    total = len(jobs)
    completed = 0
    results: list[tuple[int, str, str]] = []
    warning_items: list[tuple[int, str]] = []
    executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="ppt-ocr")
    pending: dict[Future[str], tuple[int, str, bytes]] = {}
    iterator = iter(jobs)
    aborted = False

    def submit_next() -> bool:
        try:
            item = next(iterator)
        except StopIteration:
            return False
        index, name, image_bytes = item
        future = executor.submit(ocr_image, image_bytes, Path(name).name)
        pending[future] = (index, name, image_bytes)
        return True

    for _ in range(max_workers):
        if not submit_next():
            break

    try:
        while pending:
            _check_cancel(cancel_check)
            done, _ = wait(tuple(pending), timeout=0.2, return_when=FIRST_COMPLETED)
            if not done:
                continue
            for future in done:
                index, name, _ = pending.pop(future)
                display_name = Path(name).name
                try:
                    text = future.result().strip()
                except Exception as exc:  # One bad image should not fail the whole lesson extraction.
                    text = ""
                    warning = f"{display_name} OCR 失败：{exc}"
                    warning_items.append((index, warning))
                    display_name = f"{display_name}（失败）"
                else:
                    results.append((index, name, text))

                completed += 1
                if progress:
                    progress(completed, total, display_name)
                _check_cancel(cancel_check)
                submit_next()
    except BaseException:
        aborted = True
        for future in pending:
            future.cancel()
        raise
    finally:
        executor.shutdown(wait=not aborted, cancel_futures=True)

    warnings = [message for _, message in sorted(warning_items, key=lambda item: item[0])]
    return results, warnings


_RAPID_OCR_LOCAL = threading.local()


def _rapidocr_ocr(image_bytes: bytes) -> str:
    engine = getattr(_RAPID_OCR_LOCAL, "engine", None)
    if engine is None:
        from rapidocr_onnxruntime import RapidOCR

        engine = RapidOCR()
        _RAPID_OCR_LOCAL.engine = engine

    result, _ = engine(image_bytes)
    if not result:
        return ""
    lines: list[str] = []
    for item in result:
        if len(item) < 2:
            continue
        text = str(item[1]).strip()
        confidence = float(item[2]) if len(item) > 2 else 0.0
        if text and confidence >= 0.35:
            lines.append(text)
    return "\n".join(lines)


def _tesseract_ocr(image_bytes: bytes, image_name: str) -> str:
    suffix = Path(image_name).suffix or ".png"
    with tempfile.NamedTemporaryFile(delete=False, suffix=suffix) as image_file:
        image_file.write(image_bytes)
        image_path = Path(image_file.name)
    try:
        result = subprocess.run(
            ["tesseract", str(image_path), "stdout", "-l", "chi_sim+eng"],
            check=True,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=120,
        )
        return result.stdout.strip()
    finally:
        image_path.unlink(missing_ok=True)


def _xml_texts(root: ET.Element) -> list[str]:
    texts: list[str] = []
    for element in root.iter():
        if element.tag.endswith("}t") and element.text:
            text = html.unescape(element.text.strip())
            if text:
                texts.append(text)
    return texts


def _find_lesson(detail: CourseDetail, lesson_id: str) -> Lesson:
    lesson = next((item for item in detail.lessons if item.id == lesson_id), None)
    if lesson is None:
        raise ExtractionError("未找到指定课次")
    return lesson


def _find_resource(lesson: Lesson, kind: str) -> LessonResource | None:
    return next(
        (
            resource
            for resource in lesson.resources
            if resource.kind == kind and resource.is_downloadable
        ),
        None,
    )


def _safe_path(value: str) -> str:
    cleaned = "".join(char if char not in r'\/:*?"<>|' else "_" for char in value).strip()
    return cleaned[:80] or "未命名"


def _is_image_name(name: str) -> bool:
    return Path(name).suffix.lower() in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff"}


def _section(title: str, body: str) -> str:
    return f"# {title}\n\n{body.strip() or '（无内容）'}\n"


def _report(progress: ProgressCallback | None, percent: int, message: str) -> None:
    if progress:
        progress(percent, message)


def _task_log(progress: ProgressCallback | None, message: str, level: str = "info") -> None:
    if progress is None:
        return
    logger = getattr(progress, "log", None)
    if callable(logger):
        logger(message, level)


def _check_cancel(cancel_check: CancelCheck | None) -> None:
    if cancel_check:
        cancel_check()
