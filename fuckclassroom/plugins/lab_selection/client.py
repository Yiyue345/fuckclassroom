"""Protocol observed in GUET's guet-lab-web student frontend, 2026-09-22."""
from __future__ import annotations

import html
import json
import re
from contextlib import contextmanager
from urllib.parse import unquote, urlencode

from ...course_selection.models import CourseSelectionApiError

ENTRY = "/student/for-std/extra-system/newcapec-experiment/course/stu"
API = "/guet-lab-system"
PATHS = {
    "exchange": "/api/authentication/getAccessTokenByEduToken",
    "semesters": "/mesTeacherCalendar/mesTeacherCalendar/getTeachCalendarOptions",
    "courses": "/experiment/mesTeachTask/queryListByStuId",
    "subjects": "/experiment/mesTeachTask/getSubjectSelectionList",
    "slots": "/schedule/ScheduleItemBySubject/getItemListToSelect",
    "select_group": "/schedule/ScheduleItemBySubject/stuSelectGroup",
    "select_items": "/schedule/ScheduleItemBySubject/stuSelectItem",
}


class LabSelectionError(CourseSelectionApiError):
    pass


class LabSubmissionUncertain(LabSelectionError):
    """The server may have received a mutation; automatic retry is forbidden."""


class LabRejected(LabSelectionError):
    """The upstream returned an explicit business rejection, not a lost reply."""


def extract_edu_token(content: str) -> str:
    content = html.unescape(content).replace("\\/", "/")
    match = re.search(r"[?&]token=([^\s\"'<>&]+)", content)
    if not match:
        raise LabSelectionError("实验教学入口未返回登录令牌，请重新登录本科教务")
    token = unquote(match.group(1))
    if not re.fullmatch(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+", token):
        raise LabSelectionError("实验教学登录令牌格式无效")
    return token


def rows(value, label):
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise LabSelectionError(f"{label}接口格式发生变化，请重新检查教务接口")
    return value


class LabClient:
    def __init__(self, session, opener, base, token=""):
        self.session, self.opener, self.base, self.token = session, opener, base, token
        self.account_id = ""

    def request(self, operation, **params):
        # Upstream even uses GET for mutations. Only explicit local POST actions
        # may call select_group/select_items; never crawl or retry those URLs.
        url = self.base + API + PATHS[operation]
        if params:
            url += "?" + urlencode(params)
        headers = {"Referer": self.base + "/guet-lab-web/", "tenant_id": "0"}
        if self.token:
            headers["X-Access-Token"] = self.token
        content, _, _ = self.session._request_selection_bytes(
            self.opener, url, timeout_seconds=30, accept="application/json",
            extra_headers=headers,
        )
        try:
            payload = json.loads(content)
        except (ValueError, UnicodeDecodeError) as exc:
            raise LabSelectionError("实验教学未返回 JSON，请检查教务登录状态") from exc
        if not isinstance(payload, dict) or payload.get("success") is not True:
            message = str(payload.get("message") or "请求失败") if isinstance(payload, dict) else "返回格式错误"
            # Do not reflect an upstream URL/token in local UI errors.
            message = re.sub(r"eyJ[A-Za-z0-9_.-]+", "[令牌已隐藏]", message)
            error_type = LabRejected if isinstance(payload, dict) and payload.get("success") is False else LabSelectionError
            raise error_type("实验教学：" + message[:300])
        return payload.get("result")


@contextmanager
def connect(session):
    opener, _ = session._selection_http_client()
    home = session._selection_access().home_url
    if not home.endswith("/student/home"):
        raise LabSelectionError("无法确定本科教务入口")
    base = home[:-len("/student/home")]
    content, _, final_url = session._request_selection_bytes(
        opener, base + ENTRY, timeout_seconds=30, accept="text/html",
        extra_headers={"Referer": home},
    )
    token = extract_edu_token(final_url + "\n" + content.decode("utf-8", errors="replace"))
    client = LabClient(session, opener, base)
    result = client.request("exchange", token=token)
    if not isinstance(result, dict) or not isinstance(result.get("token"), str) or not result["token"]:
        raise LabSelectionError("实验教学未返回有效访问令牌")
    client.token = result["token"]
    user = result.get("userInfo") or {}
    client.account_id = str(user.get("username") or user.get("id") or "")
    try:
        yield client
    finally:
        client.token = ""
