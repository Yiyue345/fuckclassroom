from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from ..core.cache import JsonCache
from ..core.config import AppConfig


BASE_URL = "https://classroom.guet.edu.cn"
COURSE_LIST_PATH = "/personal/courseapi/vlabpassportapi/v1/account-profile/course"
COURSE_DETAIL_PATH = "/personal/courseapi/vlabpassportapi/v1/projects/{course_id}"
TERM_LIST_PATH = "/personal/courseapi/vlabpassportapi/v1/course/term-list"
PPT_EXPORT_PATH = "/personal/courseapi/vlabpassportapi/v1/account-profile/rcourse/export/download-sub-ppt"
TRANS_EXPORT_PATH = "/personal/courseapi/vlabpassportapi/v1/account-profile/rcourse/export/download-sub-trans"


class ClassroomClientError(RuntimeError):
    pass


@dataclass(frozen=True)
class Term:
    id: str
    name: str
    year: str
    season: str
    begin_date: str
    end_date: str
    current: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Course:
    id: str
    title: str
    teacher: str
    type: str
    model: str
    start_at: str
    end_at: str
    lesson_count: int | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LessonResource:
    kind: str
    title: str
    url: str | None = None
    file_type: str | None = None
    token: str | None = None
    status: str | None = None

    @property
    def is_downloadable(self) -> bool:
        return bool(self.url)


@dataclass(frozen=True)
class Lesson:
    id: str
    title: str
    status_label: str
    start_at: str
    end_at: str
    duration_seconds: int | None
    lecturer: str
    resources: list[LessonResource]
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CourseDetail:
    course: Course
    lessons: list[Lesson]


@dataclass(frozen=True)
class ExportedFile:
    filename: str
    content: bytes
    content_type: str
    saved_path: Path
    from_cache: bool = False


@dataclass(frozen=True)
class LessonExportTarget:
    kind: str
    filename: str
    path: Path
    content_type: str
    exists: bool


class ClassroomClient:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()
        self.cache = JsonCache(self.config.cache_dir / "classroom")

    def verify_session(self) -> None:
        self._get_json(COURSE_LIST_PATH, {"nowpage": "1", "per-page": "1"})

    def list_terms(self) -> list[Term]:
        payload = self._get_json_cached("terms", TERM_LIST_PATH)
        return self._terms_from_payload(payload)

    def refresh_terms(self) -> list[Term]:
        payload = self._get_json(TERM_LIST_PATH)
        self.cache.set("terms", payload)
        return self._terms_from_payload(payload)

    def _terms_from_payload(self, payload: dict[str, Any]) -> list[Term]:
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        rows = params.get("list", [])
        if not isinstance(rows, list):
            return []
        return [self._term_from_row(row) for row in rows if isinstance(row, dict)]

    def list_courses(
        self,
        page: int = 1,
        page_size: int = 50,
        term_id: str | None = None,
        term: Term | None = None,
    ) -> list[Course]:
        payload = self._get_json_cached(
            f"courses_page_{page}_size_{page_size}",
            COURSE_LIST_PATH,
            {"nowpage": str(page), "per-page": str(page_size)},
        )
        result = self._result(payload)
        rows = result.get("data", [])
        selected_term = term
        if selected_term is None and term_id:
            selected_term = next((item for item in self.list_terms() if item.id == term_id), None)
        if selected_term is not None:
            rows = [row for row in rows if self._course_matches_term(row, selected_term)]
        return [self._course_from_row(row) for row in rows]

    def get_course_detail(self, course_id: str) -> CourseDetail:
        payload = self._get_json_cached(
            f"course_detail_{course_id}",
            COURSE_DETAIL_PATH.format(course_id=course_id),
        )
        return self._course_detail_from_payload(payload)

    def refresh_course_detail(self, course_id: str) -> CourseDetail:
        payload = self._get_json(COURSE_DETAIL_PATH.format(course_id=course_id))
        self.cache.set(f"course_detail_{course_id}", payload)
        return self._course_detail_from_payload(payload)

    def _course_detail_from_payload(self, payload: dict[str, Any]) -> CourseDetail:
        row = self._result(payload)
        course = self._course_from_detail(row)
        lessons = [self._lesson_from_row(item, course.id) for item in row.get("subject", [])]
        return CourseDetail(course=course, lessons=lessons)

    def build_ppt_export_url(self, lesson_id: str) -> str:
        return self._absolute_url(PPT_EXPORT_PATH, {"sub_id": lesson_id})

    def build_trans_export_url(self, lesson_id: str) -> str:
        return self._absolute_url(TRANS_EXPORT_PATH, {"sub_id": lesson_id})

    def get_lesson_export_target(self, course_id: str, lesson_id: str, export_kind: str) -> LessonExportTarget:
        detail = self.get_course_detail(course_id)
        lesson = next((item for item in detail.lessons if item.id == lesson_id), None)
        if lesson is None:
            raise ClassroomClientError("未找到指定课次")
        label, extension, content_type = _export_kind_info(export_kind)
        filename = f"{_safe_filename(detail.course.title)}_{_safe_filename(lesson.title)}_{label}{extension}"
        path = (
            self.config.downloads_dir
            / _safe_filename(detail.course.title)
            / _safe_filename(lesson.title)
            / filename
        )
        return LessonExportTarget(
            kind=export_kind,
            filename=filename,
            path=path,
            content_type=content_type,
            exists=path.exists(),
        )

    def download_lesson_export(self, course_id: str, lesson_id: str, export_kind: str) -> ExportedFile:
        target = self.get_lesson_export_target(course_id, lesson_id, export_kind)
        if target.exists:
            return ExportedFile(
                filename=target.filename,
                content=target.path.read_bytes(),
                content_type=target.content_type,
                saved_path=target.path,
                from_cache=True,
            )
        if export_kind == "ppt":
            path = PPT_EXPORT_PATH
        elif export_kind == "transcript":
            path = TRANS_EXPORT_PATH
        else:
            raise ClassroomClientError("不支持的导出类型")

        content, response_type = self._get_bytes(
            path,
            {"course_id": course_id, "sub_id": lesson_id},
        )
        if content.lstrip().startswith(b"{"):
            try:
                payload = json.loads(content.decode("utf-8", errors="replace"))
            except json.JSONDecodeError as exc:
                raise ClassroomClientError("平台导出响应解析失败") from exc
            message = payload.get("message") or payload.get("msg") or "平台导出失败"
            raise ClassroomClientError(str(message))

        target.path.parent.mkdir(parents=True, exist_ok=True)
        target.path.write_bytes(content)
        return ExportedFile(
            filename=target.filename,
            content=content,
            content_type=(
                response_type
                if response_type
                and "json" not in response_type
                and "text/html" not in response_type
                else target.content_type
            ),
            saved_path=target.path,
        )

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = self._absolute_url(path, params)
        request = Request(url, headers=_request_headers("application/json"))
        opener = build_opener(HTTPCookieProcessor(self._cookie_jar()))
        try:
            with opener.open(request, timeout=30) as response:
                content_type = response.headers.get("content-type", "")
                body = response.read().decode("utf-8", errors="replace")
        except HTTPError as exc:
            message = _http_error_message(exc)
            raise ClassroomClientError(message) from exc
        except URLError as exc:
            raise ClassroomClientError(f"平台接口请求失败：{exc.reason}") from exc

        if "json" not in content_type and not body.lstrip().startswith("{"):
            raise ClassroomClientError("平台返回的不是 JSON，登录会话可能已失效")

        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ClassroomClientError("平台 JSON 解析失败") from exc

        status = payload.get("status") or payload.get("code")
        if status not in (200, 1000):
            message = payload.get("message") or payload.get("msg") or "平台接口返回失败"
            raise ClassroomClientError(str(message))
        return payload

    def _get_json_cached(
        self,
        cache_name: str,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        cached = self.cache.get(cache_name)
        if cached is not None:
            return cached
        payload = self._get_json(path, params)
        self.cache.set(cache_name, payload)
        return payload

    def _get_bytes(self, path: str, params: dict[str, str]) -> tuple[bytes, str]:
        url = self._absolute_url(path, params)
        request = Request(url, headers=_request_headers("*/*"))
        opener = build_opener(HTTPCookieProcessor(self._cookie_jar()))
        try:
            with opener.open(request, timeout=60) as response:
                return response.read(), response.headers.get("content-type", "")
        except HTTPError as exc:
            message = _http_error_message(exc)
            raise ClassroomClientError(message) from exc
        except URLError as exc:
            raise ClassroomClientError(f"平台文件下载失败：{exc.reason}") from exc

    def _cookie_jar(self) -> CookieJar:
        if not self.config.storage_state_path.exists():
            raise ClassroomClientError("尚未保存登录会话，请先登录")

        storage_state = json.loads(self.config.storage_state_path.read_text(encoding="utf-8"))
        jar = CookieJar()
        for item in storage_state.get("cookies", []):
            domain = item.get("domain") or urlparse(BASE_URL).hostname or ""
            jar.set_cookie(
                Cookie(
                    version=0,
                    name=item["name"],
                    value=item["value"],
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=domain.startswith("."),
                    domain_initial_dot=domain.startswith("."),
                    path=item.get("path") or "/",
                    path_specified=True,
                    secure=bool(item.get("secure")),
                    expires=int(item["expires"]) if item.get("expires", -1) not in (-1, None) else None,
                    discard=item.get("expires", -1) in (-1, None),
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
        return jar

    @staticmethod
    def _result(payload: dict[str, Any]) -> dict[str, Any]:
        params = payload.get("params")
        if isinstance(params, dict):
            result = params.get("result")
            if isinstance(result, dict):
                return result
            data = params.get("data")
            if isinstance(data, dict):
                return data

        data = payload.get("data")
        if isinstance(data, dict):
            nested = data.get("data")
            if isinstance(nested, dict):
                return nested
            return data
        raise ClassroomClientError("平台响应缺少 result/data 字段")

    @staticmethod
    def _term_from_row(row: dict[str, Any]) -> Term:
        return Term(
            id=str(row.get("id") or ""),
            name=str(row.get("term_name") or row.get("name") or ""),
            year=str(row.get("year") or ""),
            season=str(row.get("season") or ""),
            begin_date=str(row.get("begin_date") or ""),
            end_date=str(row.get("end_date") or ""),
            current=bool(row.get("current")),
            raw=row,
        )

    @staticmethod
    def _course_from_row(row: dict[str, Any]) -> Course:
        return Course(
            id=str(row.get("Id") or row.get("id") or ""),
            title=str(row.get("Title") or row.get("title") or "未命名课程"),
            teacher=str(row.get("Teacher") or row.get("teacher") or ""),
            type=str(row.get("Type") or row.get("type") or ""),
            model=str(row.get("Model") or row.get("model") or ""),
            start_at=_format_time(row.get("StartAt") or row.get("start_at")),
            end_at=_format_time(row.get("EndAt") or row.get("end_at")),
            raw=row,
        )

    @staticmethod
    def _course_from_detail(row: dict[str, Any]) -> Course:
        course = ClassroomClient._course_from_row(row)
        return Course(
            id=course.id,
            title=course.title,
            teacher=str(row.get("teacher") or row.get("Teacher") or row.get("realname") or ""),
            type=course.type,
            model=course.model,
            start_at=course.start_at,
            end_at=course.end_at,
            lesson_count=len(row.get("subject", [])),
            raw=row,
        )

    @staticmethod
    def _lesson_from_row(row: dict[str, Any], course_id: str) -> Lesson:
        content = row.get("content") if isinstance(row.get("content"), dict) else {}
        return Lesson(
            id=str(row.get("id") or ""),
            title=str(row.get("title") or "未命名课次"),
            status_label=str(row.get("status_label") or ""),
            start_at=_format_time(row.get("start_at")),
            end_at=_format_time(row.get("end_at")),
            duration_seconds=_to_int(row.get("duration")),
            lecturer=str(row.get("lecturer_name") or ""),
            resources=ClassroomClient._resources_from_content(content, str(row.get("id") or ""), course_id),
            raw=row,
        )

    @staticmethod
    def _resources_from_content(content: dict[str, Any], lesson_id: str, course_id: str) -> list[LessonResource]:
        resources: list[LessonResource] = []
        for item in content.get("file_list", []) or []:
            if not isinstance(item, dict):
                continue
            url = item.get("file_name")
            file_type = str(item.get("file_type") or "")
            if not url:
                continue
            kind = _resource_kind(url, file_type)
            resources.append(
                LessonResource(
                    kind=kind,
                    title=_resource_title(kind, file_type),
                    url=str(url),
                    file_type=file_type,
                )
            )

        api_pass = content.get("api_pass") if isinstance(content.get("api_pass"), dict) else {}
        ppt_token = api_pass.get("ppt")
        if ppt_token:
            resources.append(
                LessonResource(
                    kind="ppt",
                    title="下载课件",
                    url=ClassroomClient._absolute_url(
                        PPT_EXPORT_PATH,
                        {"course_id": course_id, "sub_id": lesson_id},
                    ),
                    token=str(ppt_token),
                    status=str(api_pass.get("ppt_status") or ""),
                )
            )
        qlite_token = api_pass.get("qlite")
        if qlite_token:
            resources.append(
                LessonResource(
                    kind="transcript",
                    title="导出转写",
                    url=ClassroomClient._absolute_url(
                        TRANS_EXPORT_PATH,
                        {"course_id": course_id, "sub_id": lesson_id},
                    ),
                    token=str(qlite_token),
                    status=str(api_pass.get("qlite_status") or ""),
                )
            )
        return resources

    @staticmethod
    def _absolute_url(path: str, params: dict[str, str] | None = None) -> str:
        url = path if path.startswith("http") else f"{BASE_URL}{path}"
        if params:
            url = f"{url}?{urlencode(params)}"
        return url

    @staticmethod
    def _course_matches_term(row: dict[str, Any], term: Term) -> bool:
        term_start = _parse_date(term.begin_date)
        term_end = _parse_date(term.end_date)
        course_start = _date_from_timestamp(row.get("StartAt") or row.get("start_at"))
        course_end = _date_from_timestamp(row.get("EndAt") or row.get("end_at"))
        if term_start is None or term_end is None or course_start is None or course_end is None:
            return False
        return course_start <= term_end and course_end >= term_start


def _format_time(value: Any) -> str:
    seconds = _to_int(value)
    if seconds is None:
        return ""
    return datetime.fromtimestamp(seconds).strftime("%Y-%m-%d %H:%M")


def _to_int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _date_from_timestamp(value: Any) -> date | None:
    seconds = _to_int(value)
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds).date()


def _parse_date(value: str) -> date | None:
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except (TypeError, ValueError):
        return None


def _resource_kind(url: str, file_type: str) -> str:
    lowered = f"{url} {file_type}".lower()
    if ".mp4" in lowered or "mp4" in lowered:
        return "video"
    if any(ext in lowered for ext in (".jpg", ".jpeg", ".png", ".webp")):
        return "cover"
    return "file"


def _resource_title(kind: str, file_type: str) -> str:
    if kind == "video":
        return "课堂录像"
    if kind == "cover":
        return "封面/预览图"
    return file_type or "资源文件"


def _safe_filename(value: str) -> str:
    cleaned = "".join(char if char not in r'\/:*?"<>|' else "_" for char in value).strip()
    return cleaned[:80] or "未命名"


def _export_kind_info(export_kind: str) -> tuple[str, str, str]:
    if export_kind == "ppt":
        return (
            "课件",
            ".pptx",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        )
    if export_kind == "transcript":
        return (
            "转写",
            ".docx",
            "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
    raise ClassroomClientError("不支持的导出类型")


def _request_headers(accept: str) -> dict[str, str]:
    return {
        "Accept": accept,
        "Referer": f"{BASE_URL}/?tenant_code=21",
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
        ),
    }


def _http_error_message(exc: HTTPError) -> str:
    body = exc.read().decode("utf-8", errors="replace").strip()
    if body.startswith("{"):
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            payload = {}
        message = payload.get("message") or payload.get("msg")
        if message:
            return str(message)
    if "wengine-auth-failed" in body or "访问出错 - 403" in body:
        return "平台认证失败或登录会话已失效，请重新登录后再试"
    reason = exc.reason or "平台拒绝了请求"
    return f"平台接口请求失败：HTTP {exc.code} {reason}"
