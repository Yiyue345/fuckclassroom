from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

from .config import AppConfig


WEBVPN_URL = "https://v.guet.edu.cn/"
JW_WEBVPN_HOME_URL = (
    "https://v.guet.edu.cn/https/77726476706e69737468656265737421"
    "f2fc4b8b33357b44300f9ca98c1b2631e4350e37/student/home"
)
JW_WEBVPN_COURSE_SELECT_URL = (
    "https://v.guet.edu.cn/https/77726476706e69737468656265737421"
    "f2fc4b8b33357b44300f9ca98c1b2631e4350e37/student/for-std/course-select"
)
JW_INTERNAL_HOST = "bkjwtest.guet.edu.cn"
WEBVPN_HOST = "v.guet.edu.cn"
SENSITIVE_KEY_PARTS = ("token", "ticket", "session", "cookie", "auth", "password", "secret", "jwt")
CAPTURE_RESOURCE_TYPES = {"xhr", "fetch"}


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
class WebVpnSessionStatus:
    is_saved: bool
    message: str
    saved_at: str | None = None
    final_url: str | None = None


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


ProgressCallback = Callable[[int, str], None]


class CourseSelectionAssistant:
    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()
        self.session_dir = self.config.data_dir / "webvpn"
        self.storage_state_path = self.session_dir / "storage_state.json"
        self.session_meta_path = self.session_dir / "session_meta.json"
        self.capture_dir = self.session_dir / "captures"
        self.selection_info_path = self.session_dir / "selection_info.json"

    def get_session_status(self) -> WebVpnSessionStatus:
        if not self.storage_state_path.exists():
            return WebVpnSessionStatus(False, "尚未保存 WebVPN 会话")
        storage_state = self._read_json(self.storage_state_path, default={})
        cookies = storage_state.get("cookies", []) if isinstance(storage_state, dict) else []
        if not cookies:
            return WebVpnSessionStatus(False, "WebVPN 会话文件中没有 cookie，请重新登录")
        meta = self._read_json(self.session_meta_path, default={})
        return WebVpnSessionStatus(
            True,
            "已保存 WebVPN/教务会话",
            saved_at=meta.get("saved_at"),
            final_url=meta.get("final_url"),
        )

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
        with self._playwright() as playwright:
            browser = playwright.chromium.launch(headless=False)
            context = browser.new_context()
            page = context.new_page()
            page.goto(JW_WEBVPN_HOME_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            _report(progress, 20, "请在弹出的浏览器中完成 WebVPN/统一身份认证，登录后进入任意 WebVPN 内页即可保存")
            deadline = datetime.now(timezone.utc).timestamp() + timeout_ms / 1000
            while datetime.now(timezone.utc).timestamp() < deadline:
                if page.is_closed():
                    context.storage_state(path=str(self.storage_state_path))
                    self._write_session_meta("")
                    browser.close()
                    _report(progress, 100, "浏览器已关闭，会话已尽量保存")
                    return "/selection"
                if self._looks_like_jw_home(page) or self._looks_like_saved_webvpn_session(context, page):
                    page.wait_for_timeout(3_000)
                    context.storage_state(path=str(self.storage_state_path))
                    self._write_session_meta(page.url)
                    browser.close()
                    _report(progress, 100, "WebVPN/教务会话已保存")
                    return "/selection"
                page.wait_for_timeout(2_000)
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url if not page.is_closed() else "")
            browser.close()
            _report(progress, 100, "登录等待超时，已保存当前浏览器会话")
            return "/selection"

    def open_home_for_web(self, progress: ProgressCallback | None = None, timeout_ms: int = 1_800_000) -> str:
        _report(progress, 5, "正在打开本科教务系统")
        return self._open_saved_session_page(JW_WEBVPN_HOME_URL, "本科教务系统已打开，可在浏览器中操作", progress, timeout_ms)

    def open_course_select_for_web(self, progress: ProgressCallback | None = None, timeout_ms: int = 1_800_000) -> str:
        _report(progress, 5, "正在打开选课入口")
        return self._open_saved_session_page(JW_WEBVPN_COURSE_SELECT_URL, "选课入口已打开，可进入批次并执行查询", progress, timeout_ms)

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
        _report(progress, 25, ready_message)
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
        _report(progress, 100, "打开教务任务已结束，会话已保存")
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

    def get_selection_info(self) -> SelectionInfoCache:
        payload = self._read_json(self.selection_info_path, default={})
        if not isinstance(payload, dict):
            payload = {}
        courses = payload.get("courses", [])
        return SelectionInfoCache(
            updated_at=payload.get("updated_at"),
            title=str(payload.get("title") or ""),
            turn_id=_safe_int(payload.get("turn_id")),
            student_id=_safe_int(payload.get("student_id")),
            courses=courses if isinstance(courses, list) else [],
        )

    def refresh_selection_info_for_web(
        self,
        progress: ProgressCallback | None = None,
        timeout_ms: int = 300_000,
    ) -> str:
        _report(progress, 5, "正在打开选课入口")
        context = self._new_context_with_session(timeout_ms)
        page = context.new_page()
        try:
            page.goto(JW_WEBVPN_COURSE_SELECT_URL, wait_until="domcontentloaded", timeout=timeout_ms)
            _report(progress, 20, "正在进入选课批次")
            page.wait_for_timeout(2_000)
            page = self._click_start_course_select(context, page)
            page.wait_for_timeout(3_000)
            _report(progress, 40, "正在读取培养方案和可选课程")
            raw_info = self._fetch_selection_info_payload(page)
            _report(progress, 70, "正在整理教师、时间和容量信息")
            cache_payload = self._normalize_selection_info(raw_info)
            self._write_selection_info(cache_payload)
            context.storage_state(path=str(self.storage_state_path))
            self._write_session_meta(page.url if not page.is_closed() else "")
        finally:
            context.close()
        _report(progress, 100, f"已缓存 {len(cache_payload.get('courses', []))} 条可选课")
        return "/selection/info"

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

    @staticmethod
    def _fetch_selection_info_payload(page: Any) -> dict[str, Any]:
        return page.evaluate(
            """
            async () => {
              const cookieValue = (name) => {
                const item = document.cookie.split(";").map((part) => part.trim()).find((part) => part.startsWith(`${name}=`));
                return item ? decodeURIComponent(item.slice(name.length + 1)) : "";
              };
              const token = cookieValue("cs-course-select-student-token");
              if (!token) {
                throw new Error("未在选课页读取到课程选择 token，请重新打开选课入口后刷新");
              }
              const match = location.href.match(/course-select\\/(\\d+)\\/turn\\/(\\d+)\\/select/);
              if (!match) {
                throw new Error("当前页面不是具体选课批次页面");
              }
              const studentId = Number(match[1]);
              const turnId = Number(match[2]);
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
            """
        )

    def _normalize_selection_info(self, raw_info: dict[str, Any]) -> dict[str, Any]:
        major_lessons = _extract_lessons(raw_info.get("majorPlan"))
        queried_lessons = _extract_lessons(raw_info.get("queryLessons"))
        lessons = major_lessons or queried_lessons
        if major_lessons and queried_lessons:
            queried_by_id = {str(item.get("id")): item for item in queried_lessons if isinstance(item, dict)}
            merged: list[dict[str, Any]] = []
            for lesson in major_lessons:
                merged.append(queried_by_id.get(str(lesson.get("id")), lesson))
            lessons = merged
        selected_ids = _extract_selected_lesson_ids(raw_info.get("selectedLessons"))
        count_map = _extract_count_map(raw_info.get("stdCount"))
        courses = [_normalize_lesson(lesson, count_map, selected_ids) for lesson in lessons]
        courses = [course for course in courses if course["lesson_id"] is not None]
        courses.sort(key=lambda item: (item["course_name"], item["lesson_code"], item["teachers"]))
        return {
            "updated_at": datetime.now(timezone.utc).astimezone().isoformat(timespec="seconds"),
            "title": str(raw_info.get("title") or "选课信息"),
            "turn_id": _safe_int(raw_info.get("turnId")),
            "student_id": _safe_int(raw_info.get("studentId")),
            "source": "major-plan" if major_lessons else "query-lesson",
            "courses": courses,
        }

    def _write_selection_info(self, payload: dict[str, Any]) -> None:
        self._ensure_session_dir()
        self.selection_info_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

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
    def click_candidate_action(page: Any, index: int) -> str | None:
        return page.evaluate(
            """
            (index) => {
              const row = document.querySelector(`[data-fc-select-index="${index}"]`);
              if (!row) return null;
              const actionWords = ["选课", "选择", "加入", "提交", "报名"];
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
            index,
        )

    def _new_context_with_session(self, timeout_ms: int) -> Any:
        if not self.storage_state_path.exists():
            raise FileNotFoundError("尚未保存 WebVPN 会话，请先运行 login 命令")
        playwright = self._playwright()
        manager = playwright.__enter__()
        browser = manager.chromium.launch(headless=False)
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
        return any((cookie.get("domain") or "").endswith(WEBVPN_HOST) for cookie in cookies)

    @staticmethod
    def _playwright() -> Any:
        from playwright.sync_api import sync_playwright

        return sync_playwright()


def _report(progress: ProgressCallback | None, percent: int, message: str) -> None:
    if progress:
        progress(percent, message)


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
