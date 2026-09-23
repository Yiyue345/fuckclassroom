import threading
from datetime import datetime, timedelta, timezone

from ...auth.academic import get_academic_session
from .client import LabRejected, LabSelectionError, LabSubmissionUncertain, connect, rows


def identifier(value):
    value = str(value or "").strip()
    if not value or len(value) > 100:
        raise LabSelectionError("课程或实验编号无效")
    return value


def flag(value):
    return value is True or value == 1 or value == "1" or value == "true"


def group_reason(group):
    if flag(group.get("hasSelect")):
        return "已选"
    if "hasSelect" not in group:
        return "当前分组不可选"
    info = group.get("groupInfo") or {}
    now = datetime.now(timezone(timedelta(hours=8)))
    try:
        begin = datetime.fromisoformat(info["selectBeginTime"])
        end = datetime.fromisoformat(info["selectEndTime"])
        begin = begin.replace(tzinfo=now.tzinfo) if begin.tzinfo is None else begin
        end = end.replace(tzinfo=now.tzinfo) if end.tzinfo is None else end
    except (KeyError, TypeError, ValueError):
        return "选课时间信息不完整"
    if now > end:
        return "已结束"
    if now < begin:
        return "尚未开始"
    try:
        if int(group["selectCount"]) >= int(group["maxCount"]):
            return "已满"
    except (KeyError, TypeError, ValueError):
        return "名额信息不完整"
    return ""


def slot_ids(slot):
    return ",".join(identifier(item.get("id")) for item in rows(slot.get("list"), "实验时间"))


def slot_reason(slot):
    for key, label in (("isEnding", "已结束"), ("notBegin", "尚未开始"),
                       ("isFull", "已满"), ("isConflict", "实验时间冲突"),
                       ("isTheoryConflict", "理论课时间冲突")):
        if flag(slot.get(key)):
            return label
    return "" if slot.get("list") else "没有可选时间"


def find_subject(groups, subject_id):
    for group in groups:
        for subject in rows(group.get("list", []), "实验项目"):
            if str(subject.get("id")) == subject_id:
                if subject.get("selectItemString"):
                    raise LabSelectionError("该实验已经选课，请刷新查看")
                try:
                    eligible = int(group["needComplete"]) > int(group["selectCount"])
                except (KeyError, TypeError, ValueError):
                    eligible = False
                if not eligible:
                    raise LabSelectionError("该类实验已达到要求或选课条件不完整")
                return subject
    raise LabSelectionError("实验项目已不可用，请刷新")


class LabSelectionService:
    """Independent feature boundary; academic authentication is shared."""

    def __init__(self, session):
        self.session = session
        self._action_lock = threading.Lock()

    def login(self, progress):
        self.session.login_for_web(progress=progress, force_interactive=True)
        return "/lab-selection"

    def courses(self, semester="", page=1):
        with connect(self.session) as client:
            semesters = rows(client.request("semesters"), "学期")
            if not semesters:
                return {"semesters": [], "semester": "", "courses": [], "total": 0, "page": page}
            semester = semester or str(semesters[0]["value"])
            if semester not in {str(item.get("value")) for item in semesters}:
                raise LabSelectionError("所选学期已不可用，请刷新")
            result = client.request("courses", teacherCalendarId=semester, pageNo=page, pageSize=30)
            if not isinstance(result, dict):
                raise LabSelectionError("实验课程列表格式不正确")
            courses = rows(result.get("records"), "课程")
            return {"semesters": semesters, "semester": semester, "courses": courses,
                    "total": int(result.get("total") or 0), "page": page}

    def subjects(self, task_id):
        with connect(self.session) as client:
            groups = rows(client.request("subjects", taskId=identifier(task_id)), "实验项目")
            for group in groups:
                if "groupInfo" in group:
                    group["unavailable_reason"] = group_reason(group)
            return groups

    def slots(self, task_id, subject_id):
        with connect(self.session) as client:
            groups = rows(client.request("subjects", taskId=identifier(task_id)), "实验项目")
            find_subject(groups, identifier(subject_id))
            return self._slots(client, task_id, subject_id)

    @staticmethod
    def _slots(client, task_id, subject_id):
        slots = rows(client.request("slots", taskId=task_id, subjectId=subject_id, theoryFlag="true"), "实验时间")
        for slot in slots:
            slot["item_ids"] = slot_ids(slot)
            slot["unavailable_reason"] = slot_reason(slot)
        return slots

    def target_state(self, task_id, *, group_id="", subject_id="", item_ids=""):
        """Read-only eligibility check for durable waitlist jobs."""
        if bool(group_id) == bool(subject_id):
            raise LabSelectionError("请选择一个分组或一个实验时间段")
        with connect(self.session) as client:
            def state(status, message):
                return {"status": status, "message": message, "account_id": client.account_id}

            groups = rows(client.request("subjects", taskId=identifier(task_id)), "实验项目")
            if group_id:
                group = next((g for g in groups if str((g.get("groupInfo") or {}).get("id")) == group_id), None)
                if group is None:
                    raise LabSelectionError("实验分组已不可用，请刷新")
                if flag(group.get("hasSelect")):
                    return state("selected", "该分组已选上")
                if any(flag(g.get("hasSelect")) for g in groups):
                    return state("paused", "该课程已有其他已选分组，请核对后处理")
                reason = group_reason(group)
            else:
                existing = next((s for g in groups for s in g.get("list", []) if str(s.get("id")) == subject_id), None)
                if existing and existing.get("selectItemString"):
                    return state("paused", "该实验已有选课记录，请核对时间，候补已停止")
                find_subject(groups, identifier(subject_id))
                slots = self._slots(client, task_id, subject_id)
                slot = next((s for s in slots if s["item_ids"] == item_ids), None)
                if slot is None:
                    raise LabSelectionError("原实验时间组合已变化，请重新添加候补")
                reason = slot["unavailable_reason"]
            if not reason:
                return state("ready", "可以尝试选课")
            if reason == "已结束":
                return state("expired", reason)
            if reason in {"已满", "尚未开始", "实验时间冲突", "理论课时间冲突"}:
                return state("waiting", reason)
            return state("paused", reason)

    def select(self, task_id, *, group_id="", subject_id="", item_ids="", before_submit=None, expected_account=""):
        task_id = identifier(task_id)
        if bool(group_id) == bool(subject_id):
            raise LabSelectionError("请选择一个分组或一个实验时间段")
        with self._action_lock, connect(self.session) as client:
            if expected_account and client.account_id != expected_account:
                raise LabSelectionError("教务账号已变化，请核对候补任务")
            groups = rows(client.request("subjects", taskId=task_id), "实验项目")
            if group_id:
                group = next((g for g in groups if str((g.get("groupInfo") or {}).get("id")) == group_id), None)
                if group is None:
                    raise LabSelectionError("实验分组已不可用，请刷新")
                reason = group_reason(group)
                if reason:
                    raise LabSelectionError(reason)
                operation, params = "select_group", {"groupId": group_id}
            else:
                subject = find_subject(groups, identifier(subject_id))
                slots = self._slots(client, task_id, subject_id)
                slot = next((s for s in slots if s["item_ids"] == item_ids), None)
                if slot is None:
                    raise LabSelectionError("所选时间段已变化，请重新查询")
                if slot["unavailable_reason"]:
                    raise LabSelectionError(slot["unavailable_reason"])
                operation, params = "select_items", {"itemIds": item_ids, "stuId": identifier(subject.get("stuId"))}
            if before_submit is not None:
                before_submit()
            try:
                client.request(operation, taskId=task_id, selectWey=1, **params)
            except LabRejected:
                raise
            except Exception as exc:
                # A GET mutation may have reached the server even if its response
                # is lost. Never automatically retry an enrollment request.
                raise LabSubmissionUncertain("提交未确认，请先刷新已选状态，再决定是否重试。" + str(exc)[:300]) from exc
            try:
                after = rows(client.request("subjects", taskId=task_id), "实验项目")
                if group_id:
                    verified = any(str((g.get("groupInfo") or {}).get("id")) == group_id and flag(g.get("hasSelect")) for g in after)
                else:
                    verified = any(str(s.get("id")) == subject_id and bool(s.get("selectItemString")) for g in after for s in g.get("list", []))
            except Exception:
                verified = False
            return {"verified": verified, "message": "选课成功，已核验已选状态" if verified else "教务已接受提交，但尚未核验已选状态，请刷新确认，勿重复提交"}


def setup_services(context):
    from .waitlist import LabWaitlist

    lab = LabSelectionService(get_academic_session(context))
    context.services.add("lab_selection", lab)
    context.services.add("lab_waitlist", LabWaitlist(lab, context.config.data_dir / "lab_selection" / "waitlist.json"))
