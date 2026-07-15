from __future__ import annotations

import json
import re
import time
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.cookiejar import Cookie, CookieJar
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse
from urllib.request import HTTPCookieProcessor, Request, build_opener

from .config import AppConfig


WEBVPN_URL = "https://v.guet.edu.cn/"
JW_WEBVPN_BASE_URL = (
    "https://v.guet.edu.cn/https/77726476706e69737468656265737421"
    "f2fc4b8b33357b44300f9ca98c1b2631e4350e37"
)
JW_WEBVPN_HOME_URL = f"{JW_WEBVPN_BASE_URL}/student/home"
JW_WEBVPN_COURSE_SELECT_URL = f"{JW_WEBVPN_BASE_URL}/student/for-std/course-select"
COURSE_SELECTION_API_BASE_URL = f"{JW_WEBVPN_BASE_URL}/course-selection-api/api/v1/student/course-select"
JW_INTERNAL_HOST = "bkjwtest.guet.edu.cn"
WEBVPN_HOST = "v.guet.edu.cn"
SENSITIVE_KEY_PARTS = ("token", "ticket", "session", "cookie", "auth", "password", "secret", "jwt")
CAPTURE_RESOURCE_TYPES = {"xhr", "fetch"}


class CourseSelectionApiError(RuntimeError):
    pass


@dataclass(frozen=True)
class SelectionTarget:
    course_name: str = ""
    course_code: str = ""
    teacher: str = ""

    @property
    def keywords(self) -> list[str]:
        return [item for item in (self.course_name, self.course_code, self.teacher) if item]


@dataclass(frozen=True)
class SelectionCandidate:
    index: int
    text: str


@dataclass(frozen=True)
class SelectionBatch:
    student_id: int
    turn_id: int
    label: str
    url: str
    is_open: bool = False


@dataclass(frozen=True)
class WebVpnSessionStatus:
    is_saved: bool
    message: str
    saved_at: str | None = None
    final_url: str | None = None
    is_valid: bool | None = None
    checked_at: str | None = None


@dataclass(frozen=True)
class CaptureFile:
    name: str
    json_path: Path
    markdown_path: Path | None
    updated_at: str
    request_count: int


@dataclass(frozen=True)
class SelectionInfoCache:
    updated_at: str | None
    title: str
    turn_id: int | None
    student_id: int | None
    courses: list[dict[str, Any]]
    sections: dict[str, list[dict[str, Any]]]
    active_view: str


SELECTION_INFO_VIEWS: dict[str, str] = {
    "major-plan": "培养方案",
    "all": "全部课程",
    "retake": "重修选课",
    "selected": "已选课程",
}


ProgressCallback = Callable[[int, str], None]


class CourseSelectionAssistant:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()
        self.session_dir = self.config.data_dir / "webvpn"
        self.storage_state_path = self.session_dir / "storage_state.json"
        self.session_meta_path = self.session_dir / "session_meta.json"
        self.capture_dir = self.session_dir / "captures"
        self.selection_info_path = self.session_dir / "selection_info.json"
        self.selection_action_path = self.session_dir / "selection_action.json"
        self.session_check_path = self.session_dir / "session_check.json"

    def get_session_status(self) -> WebVpnSessionStatus:
        if not self.storage_state_path.exists():
            return WebVpnSessionStatus(False, "尚未保存 WebVPN 会话")
        storage_state = self._read_json(self.storage_state_path, default={})
        cookies = storage_state.get("cookies", []) if isinstance(storage_state, dict) else []
        if not cookies:
            return WebVpnSessionStatus(False, "WebVPN 会话文件中没有 cookie，请重新登录")
        meta = self._read_json(self.session_meta_path, default={})
        check = self._read_json(self.session_check_path, default={})
        is_valid = check.get("is_valid") if isinstance(check, dict) else None
        checked_at = check.get("checked_at") if isinstance(check, dict) else None
        message = "已保存 WebVPN/教务会话"
        if is_valid is True:
            message = "WebVPN/教务会话有效"
        elif is_valid is False:
            reason = str(check.get("message") or "会话已失效，请重新登录")
            message = f"WebVPN/教务会话无效：{reason}"
        return WebVpnSessionStatus(
            True,
            message,
            saved_at=meta.get("saved_at"),
            final_url=meta.get("final_url"),
            is_valid=is_valid if isinstance(is_valid, bool) else None,
            checked_at=str(checked_at) if checked_at else None,
        )

    def verify_saved_session(self, timeout_ms: int = 15_000) -> WebVpnSessionStatus:
        if not self.storage_state_path.exists():
            return self.get_session_status()
        try:
            opener, jar = self._selection_http_client()
            content, _, final_url = self._request_selection_bytes(
                opener,
                JW_WEBVPN_HOME_URL,
                timeout_seconds=max(1.0, timeout_ms / 1000),
                accept="text/html,application/xhtml+xml,application/json",
            )
            preview = content[:5000].decode("utf-8", errors="ignore").lower()
            combined = f"{final_url} {preview}"
            if "/student/home" not in combined and JW_INTERNAL_HOST not in combined:
                raise CourseSelectionApiError("没有进入本科教务页面，请重新登录 WebVPN")
            self._persist_selection_cookies(jar)
            self._write_session_check(_session_check(True, "WebVPN/教务会话有效", final_url))
        except CourseSelectionApiError as exc:
            self._write_session_check(
                _session_check(False, str(exc), JW_WEBVPN_HOME_URL)
            )
        return self.get_session_status()

    def login(self, timeout_ms: int = 120_000) -> None:
        self._ensure_session_dir()
        with self._playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(WEBVPN_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            print("请在弹出的浏览器中完成 WebVPN 登录。")
            input("登录完成后按 Enter，脚本会打开本科教务系统并保存独立会话...")
            page.goto(JW_WEBVPN_HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url)
            self._write_session_check(_session_check(True, "WebVPN/教务会话有效", page.url))
            print(f"WebVPN/教务会话已保存：{self.storage_state_path}")
            input("按 Enter 关闭浏览器...")
            browser.close()

    def open_home(self, timeout_ms: int = 120_000) -> None:
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        page.goto(JW_WEBVPN_HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        print(f"已打开本科教务系统：{page.url}")
        input("操作完成后按 Enter 保存当前会话并关闭浏览器...")
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url)
        context.close()

    def login_for_web(self, progress: ProgressCallback | None = None, timeout_ms: int = 600_000) -> str:
        self._ensure_session_dir()
        _report(progress, 5, "正在打开 WebVPN 本科教务入口")
        try:
            with self._playwright() as playwright:
                browser = playwright.chromium.launch(headless=False)
                context = browser.new_context()
                page = context.new_page()
                page.goto(JW_WEBVPN_HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
                _report(progress, 20, "请在弹出的浏览器中完成 WebVPN/统一身份认证，认证成功后程序会自动保存并关闭窗口")
                deadline = datetime.now(timezone.utc).timestamp() + timeout_ms / 1000
                next_home_attempt_at = 0.0
                while datetime.now(timezone.utc).timestamp() < deadline:
                    if page.is_closed():
                        context.storage_state(path=str(self.storage_state_path))
                        self._write_session_meta("")
                        browser.close()
                        _report(progress, 100, "浏览器已关闭，会话已尽量保存")
                        return "/selection"
                    open_pages = [item for item in context.pages if not item.is_closed()]
                    if open_pages:
                        page = open_pages[-1]
                    if self._looks_like_jw_home(page):
                        _report(progress, 60, "WebVPN/教务登录成功，正在保存会话")
                        page.wait_for_timeout(1_000)
                        context.storage_state(path=str(self.storage_state_path))
                        self._write_session_meta(page.url)
                        self._write_session_check(_session_check(True, "WebVPN/教务会话有效", page.url))
                        browser.close()
                        _report(progress, 100, "WebVPN/教务会话已保存")
                        return "/selection"
                    now = datetime.now(timezone.utc).timestamp()
                    if self._looks_like_saved_webvpn_session(context, page) and now >= next_home_attempt_at:
                        _report(progress, 45, "WebVPN 登录成功，正在自动进入本科教务首页")
                        next_home_attempt_at = now + 8
                        try:
                            page.goto(
                                JW_WEBVPN_HOME_URL,
                                wait_until="domcontentloaded",
                                timeout=min(timeout_ms, 30_000),
                            )
                        except Exception:  # The navigation may continue after Playwright times out.
                            pass
                        continue
                    page.wait_for_timeout(2_000)
                context.storage_state(path=str(self.storage_state_path))
                self._write_session_meta(page.url if not page.is_closed() else "")
                browser.close()
                _report(progress, 100, "登录等待超时，已保存当前浏览器会话")
                return "/selection"
        except PermissionError as exc:
            raise PermissionError("Windows 拒绝访问：可能是浏览器进程启动、Python 缓存文件或 WebVPN 会话文件被占用/无权限") from exc

    def open_home_for_web(self, progress: ProgressCallback | None = None, timeout_ms: int = 1_800_000) -> str:
        _report(progress, 5, "正在验证本科教务系统")
        return self._open_saved_session_page(JW_WEBVPN_HOME_URL, "本科教务会话可用", progress, timeout_ms)

    def open_course_select_for_web(self, progress: ProgressCallback | None = None, timeout_ms: int = 1_800_000) -> str:
        _report(progress, 5, "正在验证选课入口")
        return self._open_saved_session_page(JW_WEBVPN_COURSE_SELECT_URL, "选课入口会话可用", progress, timeout_ms)

    def _open_saved_session_page(
        self,
        url: str,
        ready_message: str,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 1_800_000,
    ) -> str:
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
        _report(progress, 18, "正在验证 WebVPN/教务会话")
        check = self._verify_opened_session(context, page)
        self._write_session_check(check)
        if not check["is_valid"]:
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url if not page.is_closed() else "")
            context.close()
            message = str(check.get("message") or "会话已失效，请重新登录")
            _report(progress, 100, message)
            return f"/selection?{urlencode({'select_error': message})}"
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url if not page.is_closed() else "")
        context.close()
        _report(progress, 100, ready_message)
        return "/selection"

    def capture_interfaces_for_web(
        self,
        progress: ProgressCallback | None = None,
        duration_seconds: int = 180,
        timeout_ms: int = 1_800_000,
    ) -> str:
        _report(progress, 5, "正在打开本科教务系统并准备抓取接口")
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        captured: list[dict[str, Any]] = []

        def on_response(response: Any) -> None:
            try:
                request = response.request
                if request.resource_type not in CAPTURE_RESOURCE_TYPES:
                    return
                url = request.url
                if WEBVPN_HOST not in (urlparse(url).hostname or "") and JW_INTERNAL_HOST not in url:
                    return
                record = self._capture_response(response)
                captured.append(record)
            except Exception as exc:
                captured.append({"capture_error": str(exc)})

        page.on("response", on_response)
        page.goto(JW_WEBVPN_HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        _report(progress, 15, "浏览器已打开，请进入选课页面并执行查询操作")
        deadline = datetime.now(timezone.utc).timestamp() + max(10, duration_seconds)
        while datetime.now(timezone.utc).timestamp() < deadline:
            if page.is_closed():
                break
            left = int(deadline - datetime.now(timezone.utc).timestamp())
            percent = 15 + int((duration_seconds - max(0, left)) / max(1, duration_seconds) * 75)
            _report(progress, percent, f"正在抓取接口，剩余 {max(0, left)} 秒，已记录 {len(captured)} 条")
            page.wait_for_timeout(5_000)

        if not page.is_closed():
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url)
        context.close()
        capture_base = self._write_capture(captured)
        _report(progress, 100, f"接口抓取完成，已保存 {len(captured)} 条请求")
        return f"/selection/captures/{capture_base}"

    def highlight_for_web(
        self,
        target: SelectionTarget,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 900_000,
    ) -> str:
        if not target.keywords:
            raise ValueError("至少需要提供课程名、课程代码或教师中的一项")
        _report(progress, 5, "正在打开选课入口")
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        page = self._prepare_selection_search_page(context, page, target, progress, timeout_ms)
        candidates = self.find_candidates(page, target)
        if candidates:
            _report(progress, 70, f"已高亮 {len(candidates)} 个候选课程，请在浏览器中确认")
        else:
            _report(progress, 70, "当前页面未找到候选课程，可手动调整页面后重新发起定位")
        deadline = datetime.now(timezone.utc).timestamp() + timeout_ms / 1000
        while datetime.now(timezone.utc).timestamp() < deadline:
            if page.is_closed():
                context.storage_state(path=str(self.storage_state_path))
                self._write_session_meta("")
                context.close()
                _report(progress, 100, "浏览器已关闭，会话已保存")
                return "/selection"
            page.wait_for_timeout(5_000)
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url if not page.is_closed() else "")
        context.close()
        _report(progress, 100, "定位任务已结束，会话已保存")
        return "/selection"

    def select_once_for_web(
        self,
        target: SelectionTarget,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 900_000,
    ) -> str:
        if not target.keywords:
            raise ValueError("至少需要提供课程名、课程代码或教师中的一项")
        _report(progress, 5, "正在打开选课入口")
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        page = self._prepare_selection_search_page(context, page, target, progress, timeout_ms)
        candidates = self.find_candidates(page, target)
        if len(candidates) != 1:
            if candidates:
                _report(progress, 90, f"找到 {len(candidates)} 个候选，已高亮但不会自动点击")
            else:
                _report(progress, 90, "没有找到候选课程，未执行选课")
            self._wait_until_closed_or_timeout(context, page, timeout_ms)
            _report(progress, 100, "选课任务结束")
            return "/selection"

        clicked = self.click_candidate_action(page, candidates[0].index)
        if clicked:
            _report(progress, 80, f"已点击一次：{clicked}。如有弹窗或验证码，请手动完成")
        else:
            _report(progress, 80, "找到唯一候选，但未找到可点击的选课按钮")
        self._wait_until_closed_or_timeout(context, page, timeout_ms)
        _report(progress, 100, "选课任务结束")
        return "/selection"

    def _prepare_selection_search_page(
        self,
        context: Any,
        page: Any,
        target: SelectionTarget,
        progress: ProgressCallback | None,
        timeout_ms: int,
    ) -> Any:
        page.goto(JW_WEBVPN_COURSE_SELECT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        _report(progress, 20, "正在进入选课批次")
        page.wait_for_timeout(2_000)
        page = self._click_start_course_select(context, page)
        _report(progress, 35, "正在切换到全部课程并填写查询条件")
        page.wait_for_timeout(2_000)
        self._query_course_in_page(page, target)
        _report(progress, 55, "查询已提交，正在等待课程列表")
        page.wait_for_timeout(3_000)
        return page

    def _prepare_selected_lessons_page(
        self,
        context: Any,
        page: Any,
        progress: ProgressCallback | None,
        timeout_ms: int,
    ) -> Any:
        page.goto(JW_WEBVPN_COURSE_SELECT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        _report(progress, 20, "正在进入选课批次")
        page.wait_for_timeout(2_000)
        page = self._click_start_course_select(context, page)
        _report(progress, 35, "正在切换到已选课程")
        page.wait_for_timeout(2_000)
        _click_text_in_page(page, "已选课程")
        page.wait_for_timeout(3_000)
        return page

    @staticmethod
    def _click_start_course_select(context: Any, page: Any) -> Any:
        clicked = page.evaluate(
            """
            () => {
              const buttons = Array.from(document.querySelectorAll("button, a, [role=button], .el-button"));
              const start = buttons.find((item) => {
                const text = (item.innerText || item.textContent || "").replace(/\\s+/g, "");
                return text.includes("开始选课") || text.includes("进入预览");
              });
              if (!start) return false;
              start.click();
              return true;
            }
            """
        )
        if not clicked:
            return page
        page.wait_for_timeout(3_000)
        pages = context.pages
        return pages[-1] if pages else page

    @staticmethod
    def _query_course_in_page(page: Any, target: SelectionTarget) -> None:
        query_text = target.course_code or target.course_name
        _click_text_in_page(page, "全部课程")
        page.wait_for_timeout(800)
        page.evaluate(
            """
            ({ queryText }) => {
              const setNativeValue = (input, value) => {
                if (!input) return false;
                const descriptor = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, "value");
                descriptor.set.call(input, value);
                input.dispatchEvent(new Event("input", { bubbles: true }));
                input.dispatchEvent(new Event("change", { bubbles: true }));
                return true;
              };
              const inputs = Array.from(document.querySelectorAll("input"));
              const courseInput = inputs.find((input) => {
                const rect = input.getBoundingClientRect();
                return rect.width > 0 && rect.height > 0 && (input.placeholder || "").includes("课程名称");
              });
              if (queryText) setNativeValue(courseInput, queryText);
            }
            """,
            {"queryText": query_text},
        )
        page.wait_for_timeout(300)
        _click_text_in_page(page, "查询")

    def list_captures(self) -> list[CaptureFile]:
        if not self.capture_dir.exists():
            return []
        result: list[CaptureFile] = []
        for json_path in sorted(self.capture_dir.glob("*.json"), reverse=True):
            try:
                payload = json.loads(json_path.read_text(encoding="utf-8"))
                request_count = len(payload.get("requests", []))
            except (OSError, json.JSONDecodeError):
                request_count = 0
            markdown_path = json_path.with_suffix(".md")
            stat = json_path.stat()
            result.append(
                CaptureFile(
                    name=json_path.stem,
                    json_path=json_path,
                    markdown_path=markdown_path if markdown_path.exists() else None,
                    updated_at=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                    request_count=request_count,
                )
            )
        return result

    def read_capture_markdown(self, name: str) -> str:
        path = self._capture_path(name, ".md")
        if not path.exists():
            raise FileNotFoundError(name)
        return path.read_text(encoding="utf-8")

    def get_selection_info(self, view: str = "major-plan") -> SelectionInfoCache:
        payload = self._read_json(self.selection_info_path, default={})
        if not isinstance(payload, dict):
            payload = {}
        sections = payload.get("sections", {})
        if not isinstance(sections, dict):
            sections = {}
        if "major-plan" not in sections and isinstance(payload.get("courses"), list):
            sections["major-plan"] = payload["courses"]
        active_view = view if view in SELECTION_INFO_VIEWS else "major-plan"
        courses = sections.get(active_view, [])
        return SelectionInfoCache(
            updated_at=payload.get("updated_at"),
            title=str(payload.get("title") or ""),
            turn_id=_safe_int(payload.get("turn_id")),
            student_id=_safe_int(payload.get("student_id")),
            courses=courses if isinstance(courses, list) else [],
            sections={key: value for key, value in sections.items() if isinstance(value, list)},
            active_view=active_view,
        )

    def get_selection_action_result(self) -> dict[str, Any] | None:
        payload = self._read_json(self.selection_action_path, default=None)
        return payload if isinstance(payload, dict) else None

    def _fetch_selection_info_via_api(
        self,
        progress: ProgressCallback | None,
        timeout_seconds: float,
    ) -> tuple[dict[str, Any], SelectionBatch]:
        opener, jar = self._selection_http_client()
        _report(progress, 12, "正在通过接口获取选课批次")
        batches, token = self._fetch_selection_batches(opener, timeout_seconds)
        if not batches:
            raise CourseSelectionApiError("选课入口没有返回可用批次，请确认当前是否开放选课")

        errors: list[str] = []
        for index, batch in enumerate(batches):
            percent = 25 + min(20, int(index * 20 / max(1, len(batches))))
            _report(progress, percent, f"正在读取批次：{batch.label}")
            try:
                payload = self._fetch_selection_batch_payload(opener, token, batch, timeout_seconds)
            except CourseSelectionApiError as exc:
                errors.append(str(exc))
                continue
            payload["title"] = batch.label
            payload["batches"] = [asdict(item) for item in batches]
            self._persist_selection_cookies(jar)
            return payload, batch

        detail = errors[-1] if errors else "没有批次返回课程数据"
        raise CourseSelectionApiError(f"选课批次读取失败：{detail}")

    def _selection_http_client(self) -> tuple[Any, CookieJar]:
        if not self.storage_state_path.exists():
            raise CourseSelectionApiError("尚未保存 WebVPN 会话，请先登录")
        state = self._read_json(self.storage_state_path, default={})
        cookies = state.get("cookies", []) if isinstance(state, dict) else []
        if not isinstance(cookies, list) or not cookies:
            raise CourseSelectionApiError("WebVPN 会话中没有 Cookie，请重新登录")

        jar = CookieJar()
        for item in cookies:
            if not isinstance(item, dict) or not item.get("name"):
                continue
            domain = str(item.get("domain") or WEBVPN_HOST)
            expires = item.get("expires")
            expires_at = int(expires) if isinstance(expires, (int, float)) and expires > 0 else None
            jar.set_cookie(
                Cookie(
                    version=0,
                    name=str(item["name"]),
                    value=str(item.get("value") or ""),
                    port=None,
                    port_specified=False,
                    domain=domain,
                    domain_specified=bool(domain),
                    domain_initial_dot=domain.startswith("."),
                    path=str(item.get("path") or "/"),
                    path_specified=True,
                    secure=bool(item.get("secure")),
                    expires=expires_at,
                    discard=expires_at is None,
                    comment=None,
                    comment_url=None,
                    rest={},
                    rfc2109=False,
                )
            )
        return build_opener(HTTPCookieProcessor(jar)), jar

    def _fetch_selection_batches(
        self,
        opener: Any,
        timeout_seconds: float,
    ) -> tuple[list[SelectionBatch], str]:
        token = self._fetch_selection_token(opener, timeout_seconds)
        students_payload = self._request_selection_json(
            opener,
            token,
            "/students",
            timeout_seconds=timeout_seconds,
        )
        student_ids = _extract_selection_student_ids(students_payload)
        if not student_ids:
            raise CourseSelectionApiError("选课接口没有返回可用的学生记录")

        batches: list[SelectionBatch] = []
        for student_id in student_ids:
            turns_payload = self._request_selection_json(
                opener,
                token,
                f"/open-turns/{student_id}",
                timeout_seconds=timeout_seconds,
            )
            batches.extend(_selection_batches_from_api(turns_payload, student_id))
        return _sort_selection_batches(batches), token

    def _fetch_selection_token(self, opener: Any, timeout_seconds: float) -> str:
        content, _, _ = self._request_selection_bytes(
            opener,
            JW_WEBVPN_COURSE_SELECT_URL,
            timeout_seconds=timeout_seconds,
            accept="text/html,application/xhtml+xml,application/json",
        )
        text = content.decode("utf-8", errors="replace")
        token = _extract_selection_bootstrap_token(text)
        if not token:
            raise CourseSelectionApiError("教务入口没有下发选课授权，请重新登录 WebVPN")
        return token

    def _fetch_selection_batch_payload(
        self,
        opener: Any,
        token: str,
        batch: SelectionBatch,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        turn_id = batch.turn_id
        student_id = batch.student_id
        query_condition = self._request_selection_json(
            opener, token, f"/query-condition/{turn_id}", timeout_seconds=timeout_seconds
        )
        semester_id = _extract_semester_id(query_condition)
        selected_lessons = self._request_selection_json(
            opener, token, f"/selected-lessons/{turn_id}/{student_id}", timeout_seconds=timeout_seconds
        )
        major_plan = self._request_selection_json(
            opener, token, f"/major-plan/{turn_id}/{student_id}", timeout_seconds=timeout_seconds
        )
        repaired_courses = self._request_selection_json(
            opener, token, f"/repaired-courses/{turn_id}/{student_id}", timeout_seconds=timeout_seconds
        )
        request_body = _selection_query_body(turn_id, student_id, semester_id)
        query_lessons = self._request_selection_json(
            opener,
            token,
            f"/query-lesson/{student_id}/{turn_id}",
            method="POST",
            body=request_body,
            timeout_seconds=timeout_seconds,
        )
        lesson_ids = sorted(
            {
                lesson_id
                for lesson in _extract_lessons(query_lessons)
                if (lesson_id := _safe_int(lesson.get("id"))) is not None
            }
        )
        std_count: Any = {"data": {}}
        if lesson_ids:
            query = urlencode({"lessonIds": ",".join(str(item) for item in lesson_ids)})
            std_count = self._request_selection_json(
                opener,
                token,
                f"/std-count?{query}",
                timeout_seconds=timeout_seconds,
            )
        return {
            "turnId": turn_id,
            "studentId": student_id,
            "selectedLessons": selected_lessons,
            "queryCondition": query_condition,
            "majorPlan": major_plan,
            "repairedCourses": repaired_courses,
            "queryLessons": query_lessons,
            "stdCount": std_count,
        }

    def _request_selection_json(
        self,
        opener: Any,
        token: str,
        path: str,
        *,
        method: str = "GET",
        body: dict[str, Any] | None = None,
        timeout_seconds: float,
    ) -> Any:
        headers = {
            "Accept": "application/json, text/plain, */*",
            "Authorization": token,
            "Referer": JW_WEBVPN_COURSE_SELECT_URL,
        }
        data = None
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        content, content_type, _ = self._request_selection_bytes(
            opener,
            f"{COURSE_SELECTION_API_BASE_URL}{path}",
            timeout_seconds=timeout_seconds,
            accept=headers["Accept"],
            method=method,
            data=data,
            extra_headers=headers,
        )
        if "json" not in content_type.lower() and content.lstrip().startswith(b"<"):
            raise CourseSelectionApiError("选课接口返回了登录页面，请重新登录 WebVPN")
        try:
            payload = json.loads(content.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise CourseSelectionApiError("选课接口返回的数据不是有效 JSON") from exc
        if isinstance(payload, dict):
            status = _safe_int(payload.get("status") or payload.get("code"))
            if status in (401, 403):
                raise CourseSelectionApiError("选课 token 已失效，请重新登录 WebVPN")
        return payload

    @staticmethod
    def _request_selection_bytes(
        opener: Any,
        url: str,
        *,
        timeout_seconds: float,
        accept: str,
        method: str = "GET",
        data: bytes | None = None,
        extra_headers: dict[str, str] | None = None,
    ) -> tuple[bytes, str, str]:
        headers = {
            "Accept": accept,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/124 Safari/537.36",
        }
        if extra_headers:
            headers.update(extra_headers)
        request = Request(url, data=data, headers=headers, method=method)
        try:
            with opener.open(request, timeout=timeout_seconds) as response:
                content = response.read()
                content_type = str(response.headers.get("content-type") or "")
                final_url = str(response.geturl())
        except HTTPError as exc:
            if exc.code in (401, 403):
                raise CourseSelectionApiError(f"教务接口返回 {exc.code}，请重新登录 WebVPN") from exc
            raise CourseSelectionApiError(f"教务接口请求失败：HTTP {exc.code}") from exc
        except URLError as exc:
            raise CourseSelectionApiError(f"无法访问 WebVPN 教务接口：{exc.reason}") from exc

        preview = content[:5000].decode("utf-8", errors="ignore").lower()
        combined = f"{final_url} {preview}"
        if any(marker in combined for marker in ("authserver/login", "统一身份认证", "cas/login")):
            raise CourseSelectionApiError("WebVPN/教务会话已失效，请重新登录")
        if any(marker in combined for marker in ("wengine-auth-failed", "access forbidden", "访问出错 - 403")):
            raise CourseSelectionApiError("WebVPN 网关拒绝访问，请重新登录或稍后再试")
        return content, content_type, final_url

    def _persist_selection_cookies(self, jar: CookieJar) -> None:
        state = self._read_json(self.storage_state_path, default={})
        if not isinstance(state, dict):
            return
        rows = state.get("cookies", [])
        if not isinstance(rows, list):
            rows = []
        existing = {
            (str(item.get("name") or ""), str(item.get("domain") or ""), str(item.get("path") or "/")): item
            for item in rows
            if isinstance(item, dict)
        }
        for cookie in jar:
            key = (cookie.name, cookie.domain, cookie.path)
            item = existing.get(key)
            if item is None:
                item = {
                    "name": cookie.name,
                    "domain": cookie.domain,
                    "path": cookie.path,
                    "httpOnly": False,
                    "sameSite": "Lax",
                }
                rows.append(item)
                existing[key] = item
            item.update(
                {
                    "value": cookie.value,
                    "expires": cookie.expires if cookie.expires is not None else -1,
                    "secure": cookie.secure,
                }
            )
        state["cookies"] = rows
        temporary = self.storage_state_path.with_suffix(".tmp")
        try:
            temporary.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(self.storage_state_path)
        except OSError:
            temporary.unlink(missing_ok=True)

    def refresh_selection_info_for_web(
        self,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 300_000,
        result_url: str = "/selection/info",
    ) -> str:
        _report(progress, 5, "正在读取 WebVPN 会话")
        timeout_seconds = max(5.0, min(timeout_ms / 1000, 60.0))
        try:
            raw_info, batch = self._fetch_selection_info_via_api(progress, timeout_seconds)
        except CourseSelectionApiError as exc:
            message = str(exc)
            self._write_session_check(_session_check(False, message, JW_WEBVPN_COURSE_SELECT_URL))
            raise RuntimeError(message) from exc

        _report(progress, 70, "正在整理教师、时间和容量信息")
        cache_payload = self._normalize_selection_info(raw_info)
        cache_payload["batches"] = raw_info.get("batches", [])
        self._write_selection_info(cache_payload)
        self._write_session_meta(batch.url)
        self._write_session_check(_session_check(True, "WebVPN/选课接口会话有效", batch.url))
        total = sum(len(value) for value in cache_payload.get("sections", {}).values())
        _report(progress, 100, f"已通过接口缓存 {total} 条选课信息")
        return result_url

    def get_live_selection_counts(self, timeout_ms: int = 60_000) -> dict[str, Any]:
        info = self.get_selection_info("all")
        course_by_id: dict[int, dict[str, Any]] = {}
        for courses in info.sections.values():
            for course in courses:
                if not isinstance(course, dict):
                    continue
                lesson_id = _safe_int(course.get("lesson_id"))
                if lesson_id is not None and lesson_id not in course_by_id:
                    course_by_id[lesson_id] = course
        if not course_by_id:
            return {"updated_at": _local_now(), "courses": {}}

        timeout_seconds = max(5.0, min(timeout_ms / 1000, 60.0))
        opener, jar = self._selection_http_client()
        batches, token = self._fetch_selection_batches(opener, timeout_seconds)
        batch = self._match_cached_batch(batches, info)
        if batch is None:
            raise CourseSelectionApiError("当前缓存对应的选课批次已不可用，请刷新课程")

        counts: dict[int, int] = {}
        lesson_ids = sorted(course_by_id)
        for offset in range(0, len(lesson_ids), 100):
            chunk = lesson_ids[offset : offset + 100]
            query = urlencode({"lessonIds": ",".join(str(item) for item in chunk)})
            payload = self._request_selection_json(
                opener,
                token,
                f"/std-count?{query}",
                timeout_seconds=timeout_seconds,
            )
            counts.update(_extract_count_map(payload))

        self._persist_selection_cookies(jar)
        self._write_session_check(_session_check(True, "WebVPN/选课接口会话有效", batch.url))
        return {
            "updated_at": _local_now(),
            "courses": {
                str(lesson_id): {
                    "selected_count": counts.get(lesson_id),
                    "max_count": _safe_int(course_by_id[lesson_id].get("max_count")),
                }
                for lesson_id in lesson_ids
            },
        }

    def select_cached_lesson_for_web(
        self,
        lesson_id: int,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 900_000,
        result_url: str = "/selection/info",
    ) -> str:
        return self._run_cached_lesson_action(
            lesson_id=lesson_id,
            action="select",
            progress=progress,
            timeout_ms=timeout_ms,
            result_url=result_url,
        )

    def drop_cached_lesson_for_web(
        self,
        lesson_id: int,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 900_000,
        result_url: str = "/selection/info/selected",
    ) -> str:
        return self._run_cached_lesson_action(
            lesson_id=lesson_id,
            action="drop",
            progress=progress,
            timeout_ms=timeout_ms,
            result_url=result_url,
        )

    def _run_cached_lesson_action(
        self,
        lesson_id: int,
        action: str,
        progress: ProgressCallback | None,
        timeout_ms: int,
        result_url: str,
    ) -> str:
        course = self._find_cached_course(lesson_id)
        if course is None:
            raise ValueError("缓存中找不到这门课，请先刷新选课信息")
        if action == "select" and course.get("is_selected"):
            raise ValueError("这门课已经在已选课程中")
        if action not in {"select", "drop"}:
            raise ValueError("不支持的课程操作")

        action_label = "退课" if action == "drop" else "选课"
        timeout_seconds = max(5.0, min(timeout_ms / 1000, 60.0))
        responses: list[dict[str, Any]] = []
        _report(progress, 5, "正在读取 WebVPN 会话")
        try:
            opener, jar = self._selection_http_client()
            info = self.get_selection_info("all")
            _report(progress, 12, "正在获取选课授权")
            if info.student_id is not None and info.turn_id is not None:
                token = self._fetch_selection_token(opener, timeout_seconds)
                batch = SelectionBatch(
                    student_id=info.student_id,
                    turn_id=info.turn_id,
                    label=info.title or "当前选课批次",
                    url=_selection_batch_url(info.student_id, info.turn_id),
                    is_open=True,
                )
            else:
                batches, token = self._fetch_selection_batches(opener, timeout_seconds)
                batch = self._match_cached_batch(batches, info)
                if batch is None:
                    raise CourseSelectionApiError("当前缓存对应的选课批次已不可用，请刷新课程")

            _report(progress, 22, "正在读取目标课程状态")
            lesson = self._fetch_selection_action_lesson(
                opener, token, batch, lesson_id, action, timeout_seconds
            )

            _report(progress, 35, f"正在校验{action_label}条件")
            predicate_path, predicate_body = self._selection_predicate_request(action, lesson, batch)
            predicate_start = self._record_selection_request(
                opener, token, predicate_path, responses, method="POST", body=predicate_body,
                timeout_seconds=timeout_seconds,
            )
            predicate_id = _selection_request_id(predicate_start, f"{action_label}条件校验")
            predicate_response = self._poll_selection_result(
                opener, token, f"/predicate-response/{batch.student_id}/{predicate_id}", responses,
                timeout_seconds, progress, 42, 58, f"正在等待{action_label}条件校验",
            )
            predicate_message = _selection_predicate_message(predicate_response, lesson_id)
            if predicate_message:
                raise CourseSelectionApiError(f"平台要求额外确认：{predicate_message}")

            _report(progress, 62, f"正在提交{action_label}请求")
            action_path, action_body = self._selection_submit_request(action, lesson, batch)
            action_start = self._record_selection_request(
                opener, token, action_path, responses, method="POST", body=action_body,
                timeout_seconds=timeout_seconds,
            )
            action_id = _selection_request_id(action_start, action_label)
            final_response = self._poll_selection_result(
                opener, token, f"/add-drop-response/{batch.student_id}/{action_id}", responses,
                timeout_seconds, progress, 68, 82, f"正在等待平台返回{action_label}结果",
            )
            final_data = _selection_response_data(final_response)
            if final_data.get("success") is not True:
                raise CourseSelectionApiError(_selection_response_message(final_response) or f"平台未确认{action_label}成功")

            message = _selection_response_message(final_response) or f"{action_label}成功"
            _report(progress, 88, f"{action_label}成功，正在更新本地状态")
            try:
                self._update_cached_lesson_selection(lesson_id, action == "select")
            except OSError as exc:
                message = f"{message}，但本地课程状态更新失败：{exc}"
            self._persist_selection_cookies(jar)
            self._write_session_meta(batch.url)
            self._write_session_check(_session_check(True, "WebVPN/选课接口会话有效", batch.url))
            self._write_selection_action_result(
                self._build_action_result(action, course, "接口提交", responses, status="成功", fallback_message=message)
            )
            _report(progress, 100, f"{action_label}完成")
            return result_url
        except CourseSelectionApiError as exc:
            message = str(exc)
            self._write_selection_action_result(
                self._build_action_result(action, course, None, responses, status="失败", fallback_message=message)
            )
            if any(marker in message for marker in ("重新登录", "会话", "token", "授权")):
                self._write_session_check(_session_check(False, message, JW_WEBVPN_COURSE_SELECT_URL))
            raise RuntimeError(message) from exc

    @staticmethod
    def _match_cached_batch(batches: list[SelectionBatch], info: SelectionInfoCache) -> SelectionBatch | None:
        return next(
            (batch for batch in batches if batch.turn_id == info.turn_id and batch.student_id == info.student_id),
            batches[0] if batches and info.turn_id is None else None,
        )

    def _fetch_selection_action_lesson(
        self,
        opener: Any,
        token: str,
        batch: SelectionBatch,
        lesson_id: int,
        action: str,
        timeout_seconds: float,
    ) -> dict[str, Any]:
        if action == "drop":
            source = self._request_selection_json(
                opener,
                token,
                f"/selected-lessons/{batch.turn_id}/{batch.student_id}",
                timeout_seconds=timeout_seconds,
            )
        else:
            query_condition = self._request_selection_json(
                opener,
                token,
                f"/query-condition/{batch.turn_id}",
                timeout_seconds=timeout_seconds,
            )
            source = self._request_selection_json(
                opener,
                token,
                f"/query-lesson/{batch.student_id}/{batch.turn_id}",
                method="POST",
                body=_selection_query_body(
                    batch.turn_id,
                    batch.student_id,
                    _extract_semester_id(query_condition),
                ),
                timeout_seconds=timeout_seconds,
            )
        lesson = next(
            (item for item in _extract_lessons(source) if _safe_int(item.get("id")) == lesson_id),
            None,
        )
        if lesson is None:
            message = "课程不在当前已选列表中" if action == "drop" else "课程当前不可选或已被选中"
            raise CourseSelectionApiError(message)
        return lesson

    def _update_cached_lesson_selection(self, lesson_id: int, is_selected: bool) -> None:
        payload = self._read_json(self.selection_info_path, default={})
        if not isinstance(payload, dict):
            return
        sections = payload.get("sections")
        if not isinstance(sections, dict):
            return

        selected_course: dict[str, Any] | None = None
        for courses in sections.values():
            if not isinstance(courses, list):
                continue
            for course in courses:
                if not isinstance(course, dict) or _safe_int(course.get("lesson_id")) != lesson_id:
                    continue
                course["is_selected"] = is_selected
                if selected_course is None:
                    selected_course = dict(course)

        selected_courses = sections.get("selected")
        selected_courses = selected_courses if isinstance(selected_courses, list) else []
        selected_courses = [
            course
            for course in selected_courses
            if not isinstance(course, dict) or _safe_int(course.get("lesson_id")) != lesson_id
        ]
        if is_selected and selected_course is not None:
            selected_course["is_selected"] = True
            selected_courses.append(selected_course)
            selected_courses.sort(
                key=lambda item: (
                    str(item.get("course_name") or ""),
                    str(item.get("lesson_code") or ""),
                    str(item.get("teachers") or ""),
                )
            )
        sections["selected"] = selected_courses
        if isinstance(sections.get("major-plan"), list):
            payload["courses"] = sections["major-plan"]
        payload["updated_at"] = datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")
        self._write_selection_info(payload)

    @staticmethod
    def _selection_predicate_request(
        action: str, lesson: dict[str, Any], batch: SelectionBatch,
    ) -> tuple[str, dict[str, Any]]:
        lesson_id = _safe_int(lesson.get("id"))
        if lesson_id is None:
            raise CourseSelectionApiError("课程缺少教学班编号")
        if action == "drop":
            return "/drop-predicate", {
                "studentAssoc": batch.student_id,
                "courseSelectTurnAssoc": batch.turn_id,
                "lessonAssocSet": [lesson_id],
            }
        return "/add-predicate", {
            "studentAssoc": batch.student_id,
            "courseSelectTurnAssoc": batch.turn_id,
            "requestMiddleDtos": [_selection_request_middle(lesson)],
            "coursePackAssoc": lesson.get("coursePackAssoc"),
        }

    @staticmethod
    def _selection_submit_request(
        action: str, lesson: dict[str, Any], batch: SelectionBatch,
    ) -> tuple[str, dict[str, Any]]:
        lesson_id = _safe_int(lesson.get("id"))
        if lesson_id is None:
            raise CourseSelectionApiError("课程缺少教学班编号")
        if action == "drop":
            return "/drop-request", {
                "studentAssoc": batch.student_id,
                "courseSelectTurnAssoc": batch.turn_id,
                "lessonAssocs": [lesson_id],
                "coursePackAssoc": lesson.get("coursePackAssoc"),
                "confirmMidtermRetake": False,
            }
        return "/add-request", {
            "studentAssoc": batch.student_id,
            "courseSelectTurnAssoc": batch.turn_id,
            "requestMiddleDtos": [_selection_request_middle(lesson)],
            "coursePackAssoc": lesson.get("coursePackAssoc"),
        }

    def _record_selection_request(
        self, opener: Any, token: str, path: str, responses: list[dict[str, Any]], *,
        timeout_seconds: float, method: str = "GET", body: dict[str, Any] | None = None,
    ) -> Any:
        payload = self._request_selection_json(
            opener, token, path, method=method, body=body, timeout_seconds=timeout_seconds,
        )
        responses.append({
            "method": method,
            "url": f"{COURSE_SELECTION_API_BASE_URL}{path.split('?', 1)[0]}",
            "status": 200,
            "body": payload,
        })
        return payload

    def _poll_selection_result(
        self, opener: Any, token: str, path: str, responses: list[dict[str, Any]],
        timeout_seconds: float, progress: ProgressCallback | None, progress_start: int,
        progress_end: int, message: str,
    ) -> Any:
        retry_delays = (0.15, 0.25, 0.4, 0.6, 0.8, 1.0, 1.25, 1.5, 2.0)
        for attempt in range(len(retry_delays) + 1):
            payload = self._record_selection_request(
                opener, token, path, responses, timeout_seconds=timeout_seconds,
            )
            if "success" in _selection_response_data(payload):
                return payload
            percent = progress_start + int(
                (attempt + 1) * (progress_end - progress_start) / (len(retry_delays) + 1)
            )
            _report(progress, percent, message)
            if attempt < len(retry_delays):
                time.sleep(retry_delays[attempt])
        raise CourseSelectionApiError(f"{message}超时，请稍后刷新确认最终状态")

    def _find_cached_course(self, lesson_id: int) -> dict[str, Any] | None:
        info = self.get_selection_info()
        for courses in info.sections.values():
            for course in courses:
                if isinstance(course, dict) and _safe_int(course.get("lesson_id")) == lesson_id:
                    return course
        return None

    @staticmethod
    def _filter_candidates_by_cached_course(
        candidates: list[SelectionCandidate],
        course: dict[str, Any],
    ) -> list[SelectionCandidate]:
        required = [
            str(course.get("course_name") or "").strip(),
            str(course.get("lesson_code") or "").strip(),
        ]
        optional_teacher = _first_teacher_name(str(course.get("teachers") or ""))
        if optional_teacher:
            required.append(optional_teacher)
        required = [item for item in required if item and item != "未公布"]
        if not required:
            return candidates
        result = [candidate for candidate in candidates if all(item in candidate.text for item in required)]
        return result or candidates

    def assist_select(self, target: SelectionTarget, timeout_ms: int = 120_000) -> None:
        if not target.keywords:
            raise ValueError("至少需要提供课程名、课程代码或教师中的一项")
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        page.goto(JW_WEBVPN_COURSE_SELECT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
        print("脚本已打开选课入口，请确认进入了正确批次/标签。")
        input("页面准备好后按 Enter，脚本会在当前页面查找目标课程...")

        candidates = self.find_candidates(page, target)
        if not candidates:
            print("当前页面没有找到匹配课程。请手动调整筛选条件或换到正确选课页面后重试。")
            input("按 Enter 保存当前会话并关闭浏览器...")
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url)
            context.close()
            return

        print("找到以下候选行，浏览器中已用蓝色边框标出：")
        for candidate in candidates:
            preview = " ".join(candidate.text.split())
            print(f"[{candidate.index}] {preview[:220]}")

        raw_index = input("输入要操作的候选编号；直接回车则取消：").strip()
        if not raw_index:
            print("已取消。")
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url)
            context.close()
            return
        try:
            index = int(raw_index)
        except ValueError as exc:
            raise ValueError("候选编号必须是数字") from exc

        confirm = input("确认只点击一次该行里的选课/选择/提交按钮？输入 YES 继续：").strip()
        if confirm != "YES":
            print("未确认，已取消。")
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url)
            context.close()
            return

        clicked = self.click_candidate_action(page, index)
        if clicked:
            print(f"已点击：{clicked}")
            print("如果页面弹出确认框、验证码或二次确认，请在浏览器里手动完成。")
        else:
            print("没有在候选行中找到可点击的选课按钮，请在浏览器里手动处理已标出的课程。")
        input("处理完成后按 Enter 保存当前会话并关闭浏览器...")
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url)
        context.close()

    def _wait_for_course_selection_ready(self, context: Any, page: Any, timeout_ms: int) -> Any:
        deadline = time.monotonic() + max(1_000, timeout_ms) / 1000
        last_url = page.url
        while time.monotonic() < deadline:
            open_pages = [item for item in context.pages if not item.is_closed()]
            if open_pages:
                page = open_pages[-1]
            last_url = page.url
            if _selection_route_ids(last_url) and self._read_selection_token(context, page):
                return page
            page = self._click_start_course_select(context, page)
            page.wait_for_timeout(500)
        if _selection_route_ids(last_url):
            raise RuntimeError("未在选课页读取到课程选择 token：平台尚未生成选课凭证，请重新登录并进入一次选课批次")
        raise RuntimeError("未在选课页读取到课程选择 token：没有进入具体选课批次，请重新登录后打开选课入口")

    @staticmethod
    def _read_selection_token(context: Any, page: Any) -> str:
        try:
            token = page.evaluate(
                """
                () => {
                  const cookie = document.cookie.split(";").map((part) => part.trim())
                    .find((part) => part.startsWith("cs-course-select-student-token="));
                  if (cookie) return decodeURIComponent(cookie.slice(cookie.indexOf("=") + 1));
                  for (const storage of [localStorage, sessionStorage]) {
                    for (let index = 0; index < storage.length; index += 1) {
                      const key = storage.key(index) || "";
                      if (key.toLowerCase().includes("cs-course-select-student-token")) {
                        return storage.getItem(key) || "";
                      }
                    }
                  }
                  return "";
                }
                """
            )
            if token:
                return str(token)
        except Exception:
            pass
        try:
            for cookie in context.cookies():
                if cookie.get("name") == "cs-course-select-student-token" and cookie.get("value"):
                    return str(cookie["value"])
        except Exception:
            pass
        return ""

    def _fetch_selection_info_payload(self, context: Any, page: Any) -> dict[str, Any]:
        route_ids = _selection_route_ids(page.url)
        token = self._read_selection_token(context, page)
        if not token:
            raise RuntimeError("未在选课页读取到课程选择 token，请重新打开选课入口后刷新")
        if route_ids is None:
            raise RuntimeError("当前页面不是具体选课批次页面")
        student_id, turn_id = route_ids
        return page.evaluate(
            """
            async ({token, studentId, turnId}) => {
              const apiBase = "https://bkjwtest.guet.edu.cn/course-selection-api/api/v1/student/course-select";
              const api = async (path, options = {}) => {
                const headers = {
                  "Accept": "application/json, text/plain, */*",
                  "Authorization": token,
                  ...(options.body ? {"Content-Type": "application/json"} : {}),
                };
                const response = await fetch(`${apiBase}${path}`, {
                  method: options.method || "GET",
                  headers,
                  body: options.body ? JSON.stringify(options.body) : undefined,
                  credentials: "include",
                });
                if (!response.ok) {
                  throw new Error(`${options.method || "GET"} ${path} 返回 ${response.status}`);
                }
                return response.json();
              };
              const readJsonStorage = (namePart) => {
                const key = Object.keys(localStorage).find((item) => item.includes(namePart));
                if (!key) return null;
                try {
                  return JSON.parse(localStorage.getItem(key));
                } catch {
                  return null;
                }
              };
              const options = readJsonStorage("cs-course-select-options") || {};
              const semesterId = options?.turn?.semester?.id || options?.turn?.semesterId || options?.semester?.id || null;
              const title = document.title || "选课";
              const [selectedLessons, queryCondition, majorPlan, repairedCourses] = await Promise.all([
                api(`/selected-lessons/${turnId}/${studentId}`),
                api(`/query-condition/${turnId}`),
                api(`/major-plan/${turnId}/${studentId}`),
                api(`/repaired-courses/${turnId}/${studentId}`),
              ]);
              const requestBody = {
                turnId,
                studentId,
                semesterId,
                pageNo: 1,
                pageSize: 1000,
                courseNameOrCode: "",
                lessonNameOrCode: "",
                teacherNameOrCode: "",
                week: "",
                grade: "",
                departmentId: "",
                majorId: "",
                adminclassId: "",
                campusId: "",
                openDepartmentId: "",
                courseTypeId: "",
                coursePropertyId: "",
                canSelect: 1,
                _canSelect: "可选",
                creditGte: null,
                creditLte: null,
                hasCount: null,
                ids: null,
                substitutedCourseId: null,
                courseSubstitutePoolId: null,
                sortField: "course",
                sortType: "ASC",
              };
              const queryLessons = await api(`/query-lesson/${studentId}/${turnId}`, {method: "POST", body: requestBody});
              const lessonIds = ((queryLessons.data && queryLessons.data.lessons) || []).map((lesson) => lesson.id).filter(Boolean);
              const stdCount = lessonIds.length
                ? await api(`/std-count?lessonIds=${lessonIds.join(",")}`)
                : {data: {}};
              return {
                title,
                turnId,
                studentId,
                selectedLessons,
                queryCondition,
                majorPlan,
                repairedCourses,
                queryLessons,
                stdCount,
              };
            }
            """,
            {"token": token, "studentId": student_id, "turnId": turn_id},
        )

    def _normalize_selection_info(self, raw_info: dict[str, Any]) -> dict[str, Any]:
        selected_ids = _extract_selected_lesson_ids(raw_info.get("selectedLessons"))
        count_map = _extract_count_map(raw_info.get("stdCount"))
        plan_courses = _extract_plan_courses(raw_info.get("majorPlan"))
        queried_lessons = _extract_lessons(raw_info.get("queryLessons"))
        repaired_lessons = _extract_lessons(raw_info.get("repairedCourses"))
        selected_lessons = _extract_lessons(raw_info.get("selectedLessons"))
        major_lessons = _merge_lessons(plan_courses, queried_lessons)
        sections = {
            "major-plan": self._normalize_lesson_list(major_lessons, count_map, selected_ids),
            "all": self._normalize_lesson_list(queried_lessons, count_map, selected_ids),
            "retake": self._normalize_lesson_list(repaired_lessons, count_map, selected_ids),
            "selected": self._normalize_lesson_list(selected_lessons, count_map, selected_ids),
        }
        return {
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "title": str(raw_info.get("title") or "选课信息"),
            "turn_id": _safe_int(raw_info.get("turnId")),
            "student_id": _safe_int(raw_info.get("studentId")),
            "source": "course-selection-api",
            "courses": sections["major-plan"],
            "sections": sections,
        }

    @staticmethod
    def _normalize_lesson_list(
        lessons: list[dict[str, Any]],
        count_map: dict[int, int],
        selected_ids: set[int],
    ) -> list[dict[str, Any]]:
        courses = [_normalize_lesson(lesson, count_map, selected_ids) for lesson in lessons]
        courses = [course for course in courses if course["lesson_id"] is not None]
        courses.sort(key=lambda item: (item["course_name"], item["lesson_code"], item["teachers"]))
        return courses

    def _write_selection_info(self, payload: dict[str, Any]) -> None:
        self._ensure_session_dir()
        self.selection_info_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_selection_action_result(self, payload: dict[str, Any]) -> None:
        self._ensure_session_dir()
        self.selection_action_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _build_action_result(
        self,
        action: str,
        course: dict[str, Any],
        clicked: str | None,
        responses: list[dict[str, Any]],
        status: str | None = None,
        fallback_message: str | None = None,
    ) -> dict[str, Any]:
        summary = _summarize_action_responses(responses)
        action_label = "退课" if action == "drop" else "选课"
        inferred_status = status or summary["status"] or ("已提交" if clicked else "未执行")
        message = summary["message"] or fallback_message
        if not message:
            message = f"已点击{action_label}按钮，平台未返回明确提示" if clicked else f"未找到可点击的{action_label}按钮"
        return {
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "action": action,
            "action_label": action_label,
            "lesson_id": _safe_int(course.get("lesson_id")),
            "course_name": str(course.get("course_name") or ""),
            "lesson_code": str(course.get("lesson_code") or ""),
            "clicked": clicked,
            "status": inferred_status,
            "message": message,
            "responses": summary["responses"],
        }

    @staticmethod
    def _install_action_response_capture(page: Any) -> None:
        page.evaluate(
            """
            () => {
              const sensitiveParts = ["token", "ticket", "session", "cookie", "auth", "password", "secret", "jwt"];
              const isSensitiveKey = (key) => sensitiveParts.some((part) => String(key).toLowerCase().includes(part));
              const sanitize = (value, depth = 0) => {
                if (depth > 5) return "[truncated]";
                if (Array.isArray(value)) return value.slice(0, 20).map((item) => sanitize(item, depth + 1));
                if (value && typeof value === "object") {
                  const result = {};
                  for (const [key, item] of Object.entries(value)) {
                    result[key] = isSensitiveKey(key) ? "***" : sanitize(item, depth + 1);
                  }
                  return result;
                }
                if (typeof value === "string") return value.length > 1000 ? `${value.slice(0, 1000)}...` : value;
                return value;
              };
              const cleanUrl = (url) => {
                try {
                  const parsed = new URL(url, location.href);
                  for (const key of Array.from(parsed.searchParams.keys())) {
                    if (isSensitiveKey(key)) parsed.searchParams.set(key, "***");
                  }
                  return `${parsed.origin}${parsed.pathname}${parsed.search}`;
                } catch {
                  return String(url || "");
                }
              };
              const shouldCapture = (url) => {
                const text = String(url || "");
                return text.includes("course-selection-api") || text.includes("course-select");
              };
              const parseBody = (text) => {
                if (!text) return null;
                try {
                  return sanitize(JSON.parse(text));
                } catch {
                  return text.length > 1000 ? `${text.slice(0, 1000)}...` : text;
                }
              };
              const pushRecord = (record) => {
                window.__fcActionResponses.push(record);
                if (window.__fcActionResponses.length > 12) window.__fcActionResponses.shift();
              };
              window.__fcActionResponses = [];
              if (!window.__fcActionFetchPatched) {
                const originalFetch = window.fetch;
                window.fetch = async (...args) => {
                  const response = await originalFetch.apply(window, args);
                  const request = args[0];
                  const url = typeof request === "string" ? request : (request && request.url) || "";
                  const method = (args[1] && args[1].method) || (request && request.method) || "GET";
                  if (shouldCapture(url)) {
                    response.clone().text().then((text) => {
                      pushRecord({
                        type: "fetch",
                        method,
                        url: cleanUrl(url),
                        status: response.status,
                        body: parseBody(text),
                      });
                    }).catch(() => {});
                  }
                  return response;
                };
                window.__fcActionFetchPatched = true;
              }
              if (!window.__fcActionXhrPatched) {
                const originalOpen = XMLHttpRequest.prototype.open;
                const originalSend = XMLHttpRequest.prototype.send;
                XMLHttpRequest.prototype.open = function(method, url) {
                  this.__fcActionRequest = { method, url };
                  return originalOpen.apply(this, arguments);
                };
                XMLHttpRequest.prototype.send = function() {
                  this.addEventListener("loadend", () => {
                    const request = this.__fcActionRequest || {};
                    if (!shouldCapture(request.url)) return;
                    pushRecord({
                      type: "xhr",
                      method: request.method || "GET",
                      url: cleanUrl(request.url),
                      status: this.status,
                      body: parseBody(this.responseText),
                    });
                  });
                  return originalSend.apply(this, arguments);
                };
                window.__fcActionXhrPatched = true;
              }
            }
            """
        )

    @staticmethod
    def _read_action_responses(page: Any) -> list[dict[str, Any]]:
        responses = page.evaluate("() => window.__fcActionResponses || []")
        return responses if isinstance(responses, list) else []

    @staticmethod
    def find_candidates(page: Any, target: SelectionTarget) -> list[SelectionCandidate]:
        rows = page.evaluate(
            """
            (keywords) => {
              document.querySelectorAll("[data-fc-select-index]").forEach((node) => {
                node.style.outline = "";
                node.removeAttribute("data-fc-select-index");
              });
              const containers = Array.from(document.querySelectorAll(
                "tr, li, .el-table__row, .ant-table-row, [role=row], .course-item, .list-item"
              ));
              const normalizedKeywords = keywords.map((item) => String(item).trim()).filter(Boolean);
              const candidates = [];
              const seen = new Set();
              for (const node of containers) {
                const text = (node.innerText || "").replace(/\\s+/g, " ").trim();
                if (!text || text.length > 1200) continue;
                const matched = normalizedKeywords.every((keyword) => text.includes(keyword));
                if (!matched || seen.has(text)) continue;
                seen.add(text);
                const index = candidates.length + 1;
                node.setAttribute("data-fc-select-index", String(index));
                node.style.outline = "3px solid #2563eb";
                node.style.outlineOffset = "2px";
                candidates.push({ index, text });
                if (candidates.length >= 20) break;
              }
              return candidates;
            }
            """,
            target.keywords,
        )
        return [SelectionCandidate(index=int(item["index"]), text=str(item["text"])) for item in rows]

    @staticmethod
    def click_candidate_action(page: Any, index: int, action_words: list[str] | None = None) -> str | None:
        return page.evaluate(
            """
            ({ index, actionWords }) => {
              const row = document.querySelector(`[data-fc-select-index="${index}"]`);
              if (!row) return null;
              const controls = Array.from(row.querySelectorAll(
                "button, a, [role=button], .el-button, .ant-btn"
              ));
              for (const control of controls) {
                const text = (control.innerText || control.textContent || "").trim();
                const disabled = control.disabled || control.getAttribute("aria-disabled") === "true";
                if (disabled) continue;
                if (actionWords.some((word) => text.includes(word))) {
                  control.click();
                  return text || control.tagName;
                }
              }
              return null;
            }
            """,
            {"index": index, "actionWords": action_words or ["选课", "选择", "加入", "提交", "报名"]},
        )

    def _new_context_with_session(self, timeout_ms: int) -> Any:
        if not self.storage_state_path.exists():
            raise FileNotFoundError("尚未保存 WebVPN 会话，请先运行 login 命令")
        playwright = self._playwright()
        manager = playwright.__enter__()
        browser = manager.chromium.launch(headless=True)
        try:
            return _ManagedContext(
                playwright,
                browser,
                browser.new_context(storage_state=str(self.storage_state_path)),
                timeout_ms,
            )
        except Exception:
            browser.close()
            playwright.__exit__(None, None, None)
            raise

    def _wait_until_closed_or_timeout(self, context: Any, page: Any, timeout_ms: int) -> None:
        deadline = datetime.now(timezone.utc).timestamp() + timeout_ms / 1000
        while datetime.now(timezone.utc).timestamp() < deadline:
            if page.is_closed():
                context.storage_state(path=str(self.storage_state_path))
                self._write_session_meta("")
                context.close()
                return
            page.wait_for_timeout(5_000)
        context.storage_state(path=str(self.storage_state_path))
        self._write_session_meta(page.url if not page.is_closed() else "")
        context.close()

    def _verify_opened_session(self, context: Any, page: Any) -> dict[str, Any]:
        try:
            page.wait_for_timeout(1_500)
            result = page.evaluate(
                """
                async () => {
                  const pageUrl = location.href;
                  const title = document.title || "";
                  try {
                    const response = await fetch(location.href, {
                      credentials: "include",
                      headers: { Accept: "text/html,application/xhtml+xml,application/json,*/*" },
                    });
                    const text = await response.text();
                    return {
                      request_ok: true,
                      status: response.status,
                      response_url: response.url,
                      page_url: pageUrl,
                      title,
                      body_preview: text.slice(0, 2000),
                    };
                  } catch (error) {
                    return {
                      request_ok: false,
                      status: null,
                      response_url: "",
                      page_url: pageUrl,
                      title,
                      error: error && error.message ? error.message : String(error),
                    };
                  }
                }
                """
            )
        except Exception as exc:
            return _session_check(False, f"会话验证请求失败：{exc}")

        final_url = str(result.get("response_url") or result.get("page_url") or page.url)
        status = _safe_int(result.get("status"))
        title = str(result.get("title") or "")
        body_preview = str(result.get("body_preview") or "")
        combined = f"{final_url} {title} {body_preview}".lower()
        if not result.get("request_ok"):
            return _session_check(False, f"会话验证请求失败：{result.get('error') or '未知错误'}", final_url, status)
        if status is not None and status >= 400:
            return _session_check(False, f"教务访问返回 {status}，请重新登录或稍后再试", final_url, status)
        if any(marker in combined for marker in ("authserver", "统一身份认证", "/login", "cas/login")):
            return _session_check(False, "会话已跳转到登录页，请重新登录", final_url, status)
        if any(marker in combined for marker in ("wengine-auth-failed", "访问出错 - 403", "access forbidden")):
            return _session_check(False, "WebVPN 网关拒绝访问，请重新登录或稍后再试", final_url, status)
        if self._looks_like_jw_page(context, page, final_url, title):
            return _session_check(True, "WebVPN/教务会话有效", final_url, status)
        return _session_check(False, "没有进入本科教务页面，请重新登录", final_url, status)

    @staticmethod
    def _looks_like_jw_page(context: Any, page: Any, final_url: str, title: str) -> bool:
        try:
            cookies = context.cookies()
        except Exception:
            cookies = []
        lowered = f"{page.url} {final_url} {title}".lower()
        if any(marker in lowered for marker in ("login", "cas", "统一身份认证")):
            return False
        if WEBVPN_HOST not in (urlparse(page.url).hostname or "") and WEBVPN_HOST not in (urlparse(final_url).hostname or ""):
            return False
        if not any((cookie.get("domain") or "").endswith(WEBVPN_HOST) for cookie in cookies):
            return False
        return any(marker in lowered for marker in ("/student/home", "/student/for-std/course-select", JW_INTERNAL_HOST))

    def _write_session_check(self, payload: dict[str, Any]) -> None:
        self._ensure_session_dir()
        self.session_check_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_session_meta(self, final_url: str) -> None:
        self._ensure_session_dir()
        payload = {
            "saved_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "webvpn_url": WEBVPN_URL,
            "jw_webvpn_home_url": JW_WEBVPN_HOME_URL,
            "jw_internal_host": JW_INTERNAL_HOST,
            "final_url": final_url,
            "final_host": urlparse(final_url).hostname,
        }
        self.session_meta_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _write_capture(self, requests: list[dict[str, Any]]) -> str:
        self.capture_dir.mkdir(parents=True, exist_ok=True)
        name = datetime.now().strftime("selection_capture_%Y%m%d_%H%M%S")
        payload = {
            "captured_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "jw_webvpn_home_url": JW_WEBVPN_HOME_URL,
            "jw_internal_host": JW_INTERNAL_HOST,
            "requests": requests,
        }
        json_path = self._capture_path(name, ".json")
        json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        self._capture_path(name, ".md").write_text(_capture_markdown(payload), encoding="utf-8")
        return name

    def _capture_path(self, name: str, suffix: str) -> Path:
        safe_name = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
        return self.capture_dir / f"{safe_name}{suffix}"

    def _ensure_session_dir(self) -> None:
        self.session_dir.mkdir(parents=True, exist_ok=True)

    @staticmethod
    def _capture_response(response: Any) -> dict[str, Any]:
        request = response.request
        record: dict[str, Any] = {
            "method": request.method,
            "url": _sanitize_url(request.url),
            "resource_type": request.resource_type,
            "status": response.status,
            "request_headers": _sanitize_mapping(request.headers),
            "response_headers": _sanitize_mapping(response.headers),
            "post_data": _sanitize_body(request.post_data),
        }
        content_type = response.headers.get("content-type", "")
        if "json" in content_type:
            try:
                body = response.text()
            except Exception:
                body = ""
            record["response_body"] = _sanitize_body(body, max_length=6000)
        return record

    @staticmethod
    def _read_json(path: Path, default: Any = None) -> Any:
        if not path.exists():
            return default
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return default

    @staticmethod
    def _looks_like_jw_home(page: Any) -> bool:
        try:
            url = page.url
            title = page.title()
        except Exception:
            return False
        if WEBVPN_HOST not in (urlparse(url).hostname or ""):
            return False
        if "/student/home" not in url and JW_INTERNAL_HOST not in url:
            return False
        lowered = f"{url} {title}".lower()
        return "login" not in lowered and "cas" not in lowered

    @staticmethod
    def _looks_like_saved_webvpn_session(context: Any, page: Any) -> bool:
        try:
            url = page.url
            title = page.title()
            cookies = context.cookies()
        except Exception:
            return False
        if WEBVPN_HOST not in (urlparse(url).hostname or ""):
            return False
        lowered = f"{url} {title}".lower()
        if any(marker in lowered for marker in ("login", "cas", "统一身份认证")):
            return False
        return any(
            cookie.get("name") == "wengine_vpn_ticket"
            and (cookie.get("domain") or "").endswith(WEBVPN_HOST)
            for cookie in cookies
        )

    @staticmethod
    def _playwright() -> Any:
        from playwright.sync_api import sync_playwright

        return sync_playwright()


def _report(progress: ProgressCallback | None, percent: int, message: str) -> None:
    if progress:
        progress(percent, message)


def _session_check(is_valid: bool, message: str, final_url: str | None = None, status: int | None = None) -> dict[str, Any]:
    return {
        "is_valid": is_valid,
        "message": message,
        "checked_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
        "final_url": final_url,
        "status": status,
    }


def _extract_selection_bootstrap_token(content: str) -> str:
    normalized = content.replace("\\/", "/")
    match = re.search(
        r"https://bkjwtest\.guet\.edu\.cn/course-selection/\?token=([^'\"&<\s]+)",
        normalized,
    )
    return match.group(1) if match else ""


def _extract_selection_student_ids(payload: Any) -> list[int]:
    rows = _selection_api_rows(payload, "学生记录")
    student_ids: list[int] = []
    for item in rows:
        student_id = _safe_int(item.get("id")) if isinstance(item, dict) else _safe_int(item)
        if student_id is not None and student_id not in student_ids:
            student_ids.append(student_id)
    return student_ids


def _selection_batches_from_api(payload: Any, student_id: int) -> list[SelectionBatch]:
    batches: list[SelectionBatch] = []
    for item in _selection_api_rows(payload, "选课批次"):
        if not isinstance(item, dict):
            continue
        turn_id = _safe_int(item.get("id"))
        if turn_id is None:
            continue
        batches.append(
            SelectionBatch(
                student_id=student_id,
                turn_id=turn_id,
                label=str(item.get("name") or f"选课批次 {turn_id}"),
                url=_selection_batch_url(student_id, turn_id),
                is_open=_selection_api_bool(item.get("allowEnter")),
            )
        )
    return batches


def _selection_api_rows(payload: Any, label: str) -> list[Any]:
    if not isinstance(payload, dict):
        raise CourseSelectionApiError(f"{label}接口返回格式不正确")
    if _selection_api_bool(payload.get("result")):
        message = str(payload.get("message") or payload.get("data") or f"{label}接口请求失败")
        raise CourseSelectionApiError(message)
    rows = payload.get("data")
    return rows if isinstance(rows, list) else []


def _selection_api_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _sort_selection_batches(batches: list[SelectionBatch]) -> list[SelectionBatch]:
    deduped: dict[tuple[int, int], SelectionBatch] = {}
    for batch in batches:
        deduped[(batch.student_id, batch.turn_id)] = batch
    return sorted(
        deduped.values(),
        key=lambda item: (item.is_open, item.turn_id),
        reverse=True,
    )


def _selection_batch_url(student_id: int, turn_id: int) -> str:
    return f"{JW_WEBVPN_BASE_URL}/course-selection/course-select/{student_id}/turn/{turn_id}/select"


def _extract_semester_id(payload: Any) -> int | None:
    if isinstance(payload, list):
        for item in payload:
            semester_id = _extract_semester_id(item)
            if semester_id is not None:
                return semester_id
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("semesterId", "semester_id", "semesterAssoc"):
        semester_id = _safe_int(payload.get(key))
        if semester_id is not None:
            return semester_id
    semester = payload.get("semester")
    if isinstance(semester, dict):
        semester_id = _safe_int(semester.get("id"))
        if semester_id is not None:
            return semester_id
    for value in payload.values():
        if isinstance(value, (dict, list)):
            semester_id = _extract_semester_id(value)
            if semester_id is not None:
                return semester_id
    return None


def _selection_route_ids(url: str) -> tuple[int, int] | None:
    match = re.search(r"course-select/(\d+)/turn/(\d+)/select", url)
    if not match:
        return None
    return int(match.group(1)), int(match.group(2))


def _is_selection_token_error(exc: Exception) -> bool:
    return "未在选课页读取到课程选择 token" in str(exc)


def _safe_int(value: Any) -> int | None:
    try:
        if value is None or value == "":
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _extract_lessons(payload: Any) -> list[dict[str, Any]]:
    lessons: list[dict[str, Any]] = []
    seen: set[str] = set()

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        if _looks_like_lesson(value):
            lesson_id = str(value.get("id") or value.get("lessonId") or value.get("lessonAssoc") or "")
            if lesson_id and lesson_id not in seen:
                seen.add(lesson_id)
                lessons.append(value)
            return
        for key in ("lesson", "selectedLesson"):
            nested = value.get(key)
            if isinstance(nested, dict):
                walk(nested)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                walk(nested)

    walk(payload)
    return lessons


def _extract_plan_courses(payload: Any) -> list[dict[str, Any]]:
    courses: list[dict[str, Any]] = []
    seen: set[tuple[int | None, str]] = set()

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        plan_courses = value.get("planCourses")
        if isinstance(plan_courses, list):
            for item in plan_courses:
                if not isinstance(item, dict):
                    continue
                course_id = _safe_int(item.get("id"))
                course_code = str(item.get("code") or "").strip()
                key = (course_id, course_code)
                if key != (None, "") and key not in seen:
                    seen.add(key)
                    courses.append(item)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                walk(nested)

    walk(payload)
    return courses


def _merge_lessons(primary: list[dict[str, Any]], fallback: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not primary or not fallback:
        return []

    plan_ids: set[int] = set()
    plan_codes: set[str] = set()
    for item in primary:
        course = item.get("course") if isinstance(item.get("course"), dict) else item
        course_id = _safe_int(course.get("id"))
        course_code = str(course.get("code") or "").strip()
        if course_id is not None:
            plan_ids.add(course_id)
        if course_code:
            plan_codes.add(course_code)

    matched: list[dict[str, Any]] = []
    for lesson in fallback:
        course = lesson.get("course")
        if not isinstance(course, dict):
            continue
        course_id = _safe_int(course.get("id"))
        course_code = str(course.get("code") or "").strip()
        if course_id in plan_ids or (course_code and course_code in plan_codes):
            matched.append(lesson)
    return matched


def _looks_like_lesson(value: dict[str, Any]) -> bool:
    course = value.get("course")
    teachers = value.get("teachers")
    return bool(value.get("id")) and isinstance(course, dict) and isinstance(teachers, list)


def _extract_selected_lesson_ids(payload: Any) -> set[int]:
    selected: set[int] = set()

    def walk(value: Any) -> None:
        if isinstance(value, list):
            for item in value:
                walk(item)
            return
        if not isinstance(value, dict):
            return
        for key in ("lessonId", "lessonAssoc", "lesson_id"):
            lesson_id = _safe_int(value.get(key))
            if lesson_id is not None:
                selected.add(lesson_id)
        if _looks_like_lesson(value):
            lesson_id = _safe_int(value.get("id"))
            if lesson_id is not None:
                selected.add(lesson_id)
        for key in ("lesson", "selectedLesson"):
            nested = value.get(key)
            if isinstance(nested, dict):
                walk(nested)
        for nested in value.values():
            if isinstance(nested, (dict, list)):
                walk(nested)

    walk(payload)
    return selected


def _extract_count_map(payload: Any) -> dict[int, int]:
    result: dict[int, int] = {}
    data = payload.get("data") if isinstance(payload, dict) else payload

    def add_count(lesson_id: Any, count: Any) -> None:
        parsed_id = _safe_int(lesson_id)
        if isinstance(count, str):
            count = count.split("-", 1)[0].strip()
        parsed_count = _safe_int(count)
        if parsed_id is not None and parsed_count is not None:
            result[parsed_id] = parsed_count

    if isinstance(data, dict):
        for key, value in data.items():
            if isinstance(value, dict):
                add_count(key, value.get("count") or value.get("stdCount") or value.get("selectedCount") or value.get("num"))
                add_count(value.get("lessonId") or value.get("lessonAssoc"), value.get("count") or value.get("stdCount"))
            else:
                add_count(key, value)
    elif isinstance(data, list):
        for item in data:
            if isinstance(item, dict):
                add_count(
                    item.get("lessonId") or item.get("lessonAssoc") or item.get("id"),
                    item.get("count") or item.get("stdCount") or item.get("selectedCount") or item.get("num"),
                )
    return result


def _local_now() -> str:
    return datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds")


def _selection_query_body(turn_id: int, student_id: int, semester_id: int | None) -> dict[str, Any]:
    return {
        "turnId": turn_id,
        "studentId": student_id,
        "semesterId": semester_id,
        "pageNo": 1,
        "pageSize": 1000,
        "courseNameOrCode": "",
        "lessonNameOrCode": "",
        "teacherNameOrCode": "",
        "week": "",
        "grade": "",
        "departmentId": "",
        "majorId": "",
        "adminclassId": "",
        "campusId": "",
        "openDepartmentId": "",
        "courseTypeId": "",
        "coursePropertyId": "",
        "canSelect": 1,
        "_canSelect": "可选",
        "creditGte": None,
        "creditLte": None,
        "hasCount": None,
        "ids": None,
        "substitutedCourseId": None,
        "courseSubstitutePoolId": None,
        "sortField": "course",
        "sortType": "ASC",
    }


def _selection_request_middle(lesson: dict[str, Any]) -> dict[str, Any]:
    lesson_id = _safe_int(lesson.get("id"))
    if lesson_id is None:
        raise CourseSelectionApiError("课程缺少教学班编号")
    schedule_group_assoc = _safe_int(lesson.get("scheduleGroupAssoc"))
    groups = lesson.get("scheduleGroups")
    groups = groups if isinstance(groups, list) else []
    if schedule_group_assoc is None and groups:
        defaults = [group for group in groups if isinstance(group, dict) and group.get("default")]
        candidate_groups = defaults or groups
        if len(candidate_groups) != 1:
            raise CourseSelectionApiError("该教学班有多个上课分组，请先在教务系统中选择分组")
        schedule_group_assoc = _safe_int(candidate_groups[0].get("id"))
    return {
        "lessonAssoc": lesson_id,
        "virtualCost": 0,
        "scheduleGroupAssoc": schedule_group_assoc,
    }


def _selection_request_id(payload: Any, label: str) -> int | str:
    data = payload.get("data") if isinstance(payload, dict) else None
    if isinstance(data, (int, str)) and str(data).strip():
        return data
    raise CourseSelectionApiError(_selection_response_message(payload) or f"{label}接口没有返回请求编号")


def _selection_response_data(payload: Any) -> dict[str, Any]:
    data = payload.get("data") if isinstance(payload, dict) else None
    return data if isinstance(data, dict) else {}


def _selection_response_message(payload: Any) -> str:
    messages = _extract_action_messages(payload)
    return messages[0] if messages else ""


def _selection_predicate_message(payload: Any, lesson_id: int) -> str:
    data = _selection_response_data(payload)
    if data.get("success") is not True:
        return _selection_response_message(payload) or "平台条件校验未通过"
    result = data.get("result")
    if not isinstance(result, dict):
        return ""
    value = result.get(str(lesson_id), result.get(lesson_id))
    if not value:
        return ""
    if isinstance(value, str):
        aliases = {
            "ATTEND": "需要确认跟班听课",
            "CONFIRM_MIDTERM_RETAKE": "需要确认退课后的期中重修影响",
        }
        return aliases.get(value, value)
    return _selection_response_message(value) or str(value)


def _normalize_lesson(lesson: dict[str, Any], count_map: dict[int, int], selected_ids: set[int]) -> dict[str, Any]:
    course = lesson.get("course") if isinstance(lesson.get("course"), dict) else {}
    campus = lesson.get("campus") if isinstance(lesson.get("campus"), dict) else {}
    course_type = lesson.get("courseType") if isinstance(lesson.get("courseType"), dict) else {}
    course_property = lesson.get("courseProperty") if isinstance(lesson.get("courseProperty"), dict) else {}
    open_department = lesson.get("openDepartment") if isinstance(lesson.get("openDepartment"), dict) else {}
    lesson_id = _safe_int(lesson.get("id"))
    teachers = [
        str(item.get("nameZh") or item.get("nameEn") or "").strip()
        for item in lesson.get("teachers", [])
        if isinstance(item, dict) and (item.get("nameZh") or item.get("nameEn"))
    ]
    time_text = _first_text(
        lesson.get("dateTimePlace"),
        [group.get("dateTimePlace") for group in lesson.get("scheduleGroups", []) if isinstance(group, dict)],
    )
    max_count = _safe_int(lesson.get("limitCount")) or _schedule_limit_count(lesson)
    selected_count = count_map.get(lesson_id) if lesson_id is not None else None
    return {
        "lesson_id": lesson_id,
        "course_name": str(course.get("nameZh") or lesson.get("courseName") or "").strip(),
        "course_code": str(course.get("code") or "").strip(),
        "lesson_name": str(lesson.get("nameZh") or "").strip(),
        "lesson_code": str(lesson.get("code") or "").strip(),
        "teachers": "、".join(teachers) if teachers else "未公布",
        "time": time_text or "未公布",
        "campus": str(campus.get("nameZh") or "").strip(),
        "credits": course.get("credits"),
        "course_type": str(course_type.get("nameZh") or "").strip(),
        "course_property": str(course_property.get("nameZh") or "").strip(),
        "department": str(open_department.get("nameZh") or "").strip(),
        "selected_count": selected_count,
        "max_count": max_count,
        "is_selected": lesson_id in selected_ids if lesson_id is not None else False,
    }


def _first_text(primary: Any, alternatives: list[Any]) -> str:
    for item in [primary, *alternatives]:
        if not isinstance(item, dict):
            continue
        text = item.get("textZh") or item.get("text") or item.get("textEn")
        if text:
            return str(text).replace("\n", "；")
    return ""


def _schedule_limit_count(lesson: dict[str, Any]) -> int | None:
    groups = lesson.get("scheduleGroups", [])
    if not isinstance(groups, list):
        return None
    for group in groups:
        if isinstance(group, dict):
            count = _safe_int(group.get("limitCount"))
            if count is not None:
                return count
    return None


def _first_teacher_name(teachers: str) -> str:
    cleaned = teachers.strip()
    if not cleaned or cleaned == "未公布":
        return ""
    return cleaned.split("、", 1)[0].strip()


def _summarize_action_responses(responses: list[dict[str, Any]]) -> dict[str, Any]:
    details: list[dict[str, Any]] = []
    messages: list[str] = []
    success_values: list[bool] = []
    for item in responses[:8]:
        if not isinstance(item, dict):
            continue
        body = item.get("body")
        messages.extend(_extract_action_messages(body))
        success = _extract_action_success(body)
        if success is not None:
            success_values.append(success)
        status_code = _safe_int(item.get("status"))
        if status_code is not None and status_code >= 400:
            success_values.append(False)
        detail = {
            "method": str(item.get("method") or ""),
            "url": str(item.get("url") or ""),
            "status": status_code,
        }
        body_messages = _extract_action_messages(body)
        if body_messages:
            detail["message"] = body_messages[0]
        details.append(detail)
    message = next((item for item in messages if item), "")
    status = ""
    if False in success_values:
        status = "失败"
    elif True in success_values:
        status = "成功"
    return {"status": status, "message": message, "responses": details}


def _extract_action_messages(value: Any) -> list[str]:
    messages: list[str] = []
    message_keys = {"message", "msg", "error", "errorMessage", "reason", "detail"}

    def collect_strings(item: Any, depth: int = 0) -> None:
        if depth > 4 or len(messages) >= 8:
            return
        if isinstance(item, str):
            cleaned = " ".join(item.split())
            if cleaned and 2 <= len(cleaned) <= 300:
                messages.append(cleaned)
            return
        if isinstance(item, list):
            for child in item:
                collect_strings(child, depth + 1)
            return
        if isinstance(item, dict):
            for child in item.values():
                collect_strings(child, depth + 1)

    def walk(item: Any, depth: int = 0) -> None:
        if depth > 5 or len(messages) >= 8:
            return
        if isinstance(item, list):
            for child in item:
                walk(child, depth + 1)
            return
        if not isinstance(item, dict):
            return
        for key, child in item.items():
            if key in message_keys:
                collect_strings(child)
        for key, child in item.items():
            if key not in message_keys and isinstance(child, (dict, list)):
                walk(child, depth + 1)

    walk(value)
    deduped: list[str] = []
    for message in messages:
        if message not in deduped:
            deduped.append(message)
    return deduped


def _extract_action_success(value: Any) -> bool | None:
    if isinstance(value, list):
        for item in value:
            result = _extract_action_success(item)
            if result is not None:
                return result
        return None
    if not isinstance(value, dict):
        return None
    for key, item in value.items():
        lowered = str(key).lower()
        if lowered in {"success", "succeeded", "ok"} and isinstance(item, bool):
            return item
        if lowered in {"code", "status", "statuscode"}:
            if item in (0, "0", "success", "SUCCESS", "ok", "OK"):
                return True
            if isinstance(item, int) and item >= 400:
                return False
        if lowered in {"error", "errormessage"} and item:
            return False
    for item in value.values():
        result = _extract_action_success(item)
        if result is not None:
            return result
    return None


def _click_text_in_page(page: Any, text: str) -> bool:
    return bool(
        page.evaluate(
            """
            (text) => {
              const normalizedText = String(text).replace(/\\s+/g, "");
              const nodes = Array.from(document.querySelectorAll("button, a, [role=tab], [role=button], .el-button"));
              const node = nodes.find((item) => {
                const rect = item.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) return false;
                const content = (item.innerText || item.textContent || "").replace(/\\s+/g, "");
                return content.includes(normalizedText);
              });
              if (!node) return false;
              node.click();
              return true;
            }
            """,
            text,
        )
    )


def _sanitize_url(url: str) -> str:
    parsed = urlparse(url)
    query_items = []
    for key, value in parse_qsl(parsed.query, keep_blank_values=True):
        query_items.append((key, "***" if _is_sensitive_key(key) else value))
    return urlunparse(parsed._replace(query=urlencode(query_items)))


def _sanitize_mapping(mapping: dict[str, Any]) -> dict[str, str]:
    result: dict[str, str] = {}
    for key, value in mapping.items():
        result[str(key)] = "***" if _is_sensitive_key(str(key)) else str(value)
    return result


def _sanitize_body(body: str | None, max_length: int = 4000) -> Any:
    if not body:
        return None
    text = body[:max_length]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return _sanitize_form_or_text(text)
    return _sanitize_value(data)


def _sanitize_form_or_text(text: str) -> Any:
    pairs = parse_qsl(text, keep_blank_values=True)
    if pairs:
        return {key: "***" if _is_sensitive_key(key) else value for key, value in pairs}
    return text


def _sanitize_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {key: "***" if _is_sensitive_key(str(key)) else _sanitize_value(item) for key, item in value.items()}
    if isinstance(value, list):
        return [_sanitize_value(item) for item in value]
    return value


def _is_sensitive_key(key: str) -> bool:
    lowered = key.lower()
    return any(part in lowered for part in SENSITIVE_KEY_PARTS)


def _capture_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# 选课接口抓取记录",
        "",
        f"- 抓取时间：{payload.get('captured_at', '')}",
        f"- 请求数量：{len(payload.get('requests', []))}",
        f"- 内部域名：{payload.get('jw_internal_host', '')}",
        "",
    ]
    for index, item in enumerate(payload.get("requests", []), start=1):
        if "capture_error" in item:
            lines.extend([f"## {index}. 抓取错误", "", str(item["capture_error"]), ""])
            continue
        lines.extend(
            [
                f"## {index}. {item.get('method')} {item.get('status')}",
                "",
                f"- 类型：{item.get('resource_type')}",
                f"- URL：`{item.get('url')}`",
                "",
            ]
        )
        if item.get("post_data") is not None:
            lines.extend(["请求体：", "", "```json", json.dumps(item["post_data"], ensure_ascii=False, indent=2), "```", ""])
        if item.get("response_body") is not None:
            lines.extend(
                ["响应体节选：", "", "```json", json.dumps(item["response_body"], ensure_ascii=False, indent=2), "```", ""]
            )
    return "\n".join(lines).strip() + "\n"


class _ManagedContext:
    def __init__(self, playwright_context_manager: Any, browser: Any, browser_context: Any, timeout_ms: int) -> None:
        self._playwright_context_manager = playwright_context_manager
        self._browser = browser
        self._browser_context = browser_context
        self._browser_context.set_default_timeout(timeout_ms)

    def __getattr__(self, name: str) -> Any:
        return getattr(self._browser_context, name)

    @property
    def browser(self) -> Any:
        return self._browser

    def storage_state(self, *args: Any, **kwargs: Any) -> Any:
        return self._browser_context.storage_state(*args, **kwargs)

    def new_page(self) -> Any:
        return self._browser_context.new_page()

    def close(self) -> None:
        try:
            try:
                self._browser_context.close()
            finally:
                self._browser.close()
        finally:
            self._playwright_context_manager.__exit__(None, None, None)
