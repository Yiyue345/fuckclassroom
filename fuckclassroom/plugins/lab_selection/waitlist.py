"""Persistent, explicitly requested local waitlist; no official queue is implied."""
from __future__ import annotations

import copy
import json
import threading
import time
from pathlib import Path
from uuid import uuid4

from ...core.atomic import atomic_write_text
from .client import LabRejected, LabSelectionError, LabSubmissionUncertain

ACTIVE = {"waiting", "waiting_session", "checking", "submitting"}
LABELS = {"waiting": "等待空位或开始", "waiting_session": "等待登录或网络恢复",
          "checking": "正在检查", "submitting": "正在提交", "selected": "已选上",
          "paused": "待人工核对", "cancelled": "已取消", "expired": "已结束"}
_OWNERS = {}
_OWNERS_LOCK = threading.Lock()


class WaitlistCancelled(Exception):
    pass


class LabWaitlist:
    def __init__(self, lab, path: Path, *, clock=time.time):
        self.lab, self.path, self.clock = lab, path, clock
        self._lock = threading.RLock()
        self._runner = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._jobs = self._load()

    def _load(self):
        if not self.path.exists():
            return {}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            jobs = payload["jobs"]
            if not isinstance(jobs, dict):
                raise ValueError("invalid jobs")
            for job in jobs.values():
                if job["status"] == "submitting":
                    job.update(status="paused", message="上次提交被中断，请先核对已选状态，勿重复提交")
                elif job["status"] == "checking":
                    job.update(status="waiting", next_check=self.clock())
            return jobs
        except (OSError, ValueError, KeyError, TypeError):
            # Preserve damaged data for diagnosis instead of silently erasing jobs.
            raise LabSelectionError("实验候补任务文件损坏，请备份并检查 waitlist.json")

    def _save(self):
        atomic_write_text(self.path, json.dumps({"version": 1, "jobs": self._jobs}, ensure_ascii=False, indent=2))

    def list_jobs(self):
        with self._lock:
            jobs = copy.deepcopy(list(self._jobs.values()))
        for job in jobs:
            job.pop("account_id", None)
            job["status_label"] = LABELS.get(job["status"], job["status"])
            job["is_active"] = job["status"] in ACTIVE
        return sorted(jobs, key=lambda j: j["created_at"], reverse=True)

    def add(self, *, task_id, group_id="", subject_id="", item_ids="", label="", interval=30):
        target = dict(task_id=task_id, group_id=group_id, subject_id=subject_id, item_ids=item_ids)
        state = self.lab.target_state(**target)
        if state["status"] not in {"ready", "waiting"}:
            raise LabSelectionError(state["message"])
        if not state.get("account_id"):
            raise LabSelectionError("无法确认教务账号，不能创建自动候补任务")
        with self._lock:
            for job in self._jobs.values():
                if job["target"] == target and job.get("account_id") == state["account_id"] and job["status"] in ACTIVE | {"paused"}:
                    return {"id": job["id"], "created": False}
            now = self.clock()
            job_id = uuid4().hex
            self._jobs[job_id] = {"id": job_id, "target": target, "label": label[:250],
                "status": "waiting", "message": state["message"], "account_id": state["account_id"], "interval": max(15, min(3600, int(interval))),
                "created_at": now, "updated_at": now, "next_check": now,
                "checks": 0, "attempts": 0, "cancel_requested": False}
            self._save()
        self._wake.set()
        return {"id": job_id, "created": True}

    def cancel(self, job_id):
        with self._lock:
            job = self._jobs.get(job_id)
            if job is None:
                raise LabSelectionError("候补任务不存在")
            if job["status"] == "submitting":
                job.update(cancel_requested=True, message="已取消后续尝试；已发出的请求仍需核验结果")
            elif job["status"] in ACTIVE or job["status"] == "paused":
                job.update(status="cancelled", message="已取消候补", next_check=None)
            job["updated_at"] = self.clock()
            self._save()
        self._wake.set()

    def start(self):
        with _OWNERS_LOCK:
            key = str(self.path.resolve())
            previous = _OWNERS.get(key)
            if previous is self and self._thread and self._thread.is_alive():
                return
            if previous is not None and previous is not self:
                previous.stop()
            with self._lock:
                self._jobs = self._load()
                self._stop.clear()
                self._thread = threading.Thread(target=self._worker, name="lab-waitlist", daemon=True)
                _OWNERS[key] = self
                self._thread.start()

    def stop(self):
        self._stop.set()
        self._wake.set()
        if self._thread:
            # Lifecycle awaits this in a thread; drain requests before releasing
            # shared session/proxy resources or starting the replacement runtime.
            self._thread.join()

    def _worker(self):
        while not self._stop.is_set():
            self._wake.clear()
            try:
                ran = self.run_due_once()
            except Exception:
                # Never loop at full speed or submit again after a persistence error.
                with self._lock:
                    for job in self._jobs.values():
                        if job["status"] in ACTIVE:
                            job.update(status="paused", message="候补后台已停止，请检查任务文件是否可写并核对已选状态")
                self._stop.set()
                return
            if not ran:
                self._wake.wait(1)

    def run_due_once(self):
        if self._stop.is_set() or not self._runner.acquire(blocking=False):
            return False
        try:
            with self._lock:
                job = next((j for j in self._jobs.values() if j["status"] in {"waiting", "waiting_session"}
                            and j["next_check"] <= self.clock()), None)
                if job is None:
                    return False
                job.update(status="checking", checks=job["checks"] + 1, updated_at=self.clock())
                self._save()
                job_id, target = job["id"], dict(job["target"])
            submitted = False

            def before_submit():
                nonlocal submitted
                with self._lock:
                    current = self._jobs[job_id]
                    if self._stop.is_set() or current["status"] == "cancelled":
                        raise WaitlistCancelled()
                    current.update(status="submitting", message="正在提交选课", attempts=current["attempts"] + 1)
                    self._save()  # Persist before any remote side effect.
                    submitted = True

            try:
                state = self.lab.target_state(**target)
                if not job.get("account_id") or state.get("account_id") != job["account_id"]:
                    raise LabSelectionError("教务账号已变化，候补已暂停，请核对后重新添加")
                state = {key: state[key] for key in ("status", "message")}
                if state["status"] == "ready":
                    result = self.lab.select(**target, before_submit=before_submit, expected_account=job["account_id"])
                    state = {"status": "selected" if result["verified"] else "paused", "message": result["message"]}
            except WaitlistCancelled:
                state = {"status": "waiting", "message": "应用停止，等待下次启动"}
            except LabSubmissionUncertain as exc:
                state = {"status": "paused", "message": str(exc)}
            except LabRejected as exc:
                state = {"status": "paused", "message": str(exc)}
                if any(word in str(exc).lower() for word in ("登录", "会话", "token", "expired")):
                    state["status"] = "waiting_session"
                if submitted:
                    try:
                        observed = self.lab.target_state(**target)
                        if observed.get("account_id") == job["account_id"] and observed["status"] in {"waiting", "expired", "selected"}:
                            state = {key: observed[key] for key in ("status", "message")}
                    except Exception:
                        pass
            except Exception as exc:
                message = str(exc)[:400]
                if submitted:
                    state = {"status": "paused", "message": "提交结果不明，请核对已选状态"}
                elif message in {"已满", "尚未开始", "实验时间冲突", "理论课时间冲突"}:
                    state = {"status": "waiting", "message": message}
                elif message == "已结束":
                    state = {"status": "expired", "message": message}
                elif any(word in message.lower() for word in ("会话", "登录", "未返回 json", "timeout", "timed out", "无法访问", "http 5")):
                    state = {"status": "waiting_session", "message": message}
                else:
                    state = {"status": "paused", "message": message}
            with self._lock:
                current = self._jobs[job_id]
                if current["status"] == "cancelled":
                    return True
                if current["cancel_requested"] and state["status"] not in {"selected", "paused"}:
                    state = {"status": "cancelled", "message": "已取消候补"}
                delay = max(current["interval"], 60) if state["status"] == "waiting_session" else current["interval"]
                current.update(**state, updated_at=self.clock(),
                               next_check=self.clock() + delay if state["status"] in {"waiting", "waiting_session"} else None)
                self._save()
            return True
        finally:
            self._runner.release()
