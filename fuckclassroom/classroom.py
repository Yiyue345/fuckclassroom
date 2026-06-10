from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .config import AppConfig


BASE_URL = "https://classroom.guet.edu.cn"
COURSE_LIST_PATH = "/personal/courseapi/vlabpassportapi/v1/account-profile/course"
COURSE_DETAIL_PATH = "/personal/courseapi/vlabpassportapi/v1/projects/{course_id}"
PPT_EXPORT_PATH = "/personal/courseapi/vlabpassportapi/v1/account-profile/rcourse/export/download-sub-ppt"
TRANS_EXPORT_PATH = "/personal/courseapi/vlabpassportapi/v1/account-profile/rcourse/export/download-sub-trans"


class ClassroomClientError(RuntimeError):
    pass


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


class ClassroomClient:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()

    def list_courses(self, page: int = 1, page_size: int = 50) -> list[Course]:
        payload = self._get_json(
            COURSE_LIST_PATH,
            {"nowpage": str(page), "per-page": str(page_size)},
        )
        result = self._result(payload)
        rows = result.get("data", [])
        return [self._course_from_row(row) for row in rows]

    def get_course_detail(self, course_id: str) -> CourseDetail:
        payload = self._get_json(COURSE_DETAIL_PATH.format(course_id=course_id))
        row = self._result(payload)
        course = self._course_from_detail(row)
        lessons = [self._lesson_from_row(item, course.id) for item in row.get("subject", [])]
        return CourseDetail(course=course, lessons=lessons)

    def build_ppt_export_url(self, lesson_id: str) -> str:
        return self._absolute_url(PPT_EXPORT_PATH, {"sub_id": lesson_id})

    def build_trans_export_url(self, lesson_id: str) -> str:
        return self._absolute_url(TRANS_EXPORT_PATH, {"sub_id": lesson_id})

    def _get_json(self, path: str, params: dict[str, str] | None = None) -> dict[str, Any]:
        url = self._absolute_url(path, params)
        request = Request(url, headers={"Accept": "application/json"})
        opener = build_opener(HTTPCookieProcessor(self._cookie_jar()))
        with opener.open(request, timeout=30) as response:
            content_type = response.headers.get("content-type", "")
            body = response.read().decode("utf-8", errors="replace")

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
                    title="课件处理标识",
                    url=None,
                    token=str(ppt_token),
                    status=str(api_pass.get("ppt_status") or ""),
                )
            )
        qlite_token = api_pass.get("qlite")
        if qlite_token:
            resources.append(
                LessonResource(
                    kind="transcript",
                    title="平台转写标识",
                    url=None,
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
