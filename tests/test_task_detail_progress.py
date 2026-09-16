from __future__ import annotations

import threading
import time
import unittest

from fuckclassroom.core.tasks import TaskManager


class TaskDetailProgressTests(unittest.TestCase):
    def test_worker_can_publish_and_clear_detail_progress(self) -> None:
        manager = TaskManager()
        published = threading.Event()
        release = threading.Event()

        def worker(progress):
            progress(10, "正在准备")
            progress.detail(37, "正在下载语音模型：180 MB / 486 MB（37%）")
            published.set()
            if not release.wait(2):
                raise RuntimeError("test timed out")
            progress.clear_detail()
            progress(90, "继续处理")
            return "/done"

        task = manager.start("测试任务", worker)
        self.assertTrue(published.wait(2))
        state = manager.as_dict(task.id)
        self.assertIsNotNone(state)
        self.assertEqual(state["detail_progress"], 37)
        self.assertIn("37%", str(state["detail_message"]))
        self.assertTrue(any("正在准备" in entry["message"] for entry in state["logs"]))
        self.assertTrue(any("正在下载语音模型" in entry["message"] for entry in state["logs"]))

        release.set()
        deadline = time.time() + 2
        while time.time() < deadline:
            state = manager.as_dict(task.id)
            if state and state["status"] == "succeeded":
                break
            time.sleep(0.01)

        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "succeeded")
        self.assertIsNone(state["detail_progress"])
        self.assertIsNone(state["detail_message"])
        self.assertTrue(any("任务执行完成" in entry["message"] for entry in state["logs"]))

    def test_running_task_can_be_cancelled_at_progress_checkpoint(self) -> None:
        manager = TaskManager()
        started = threading.Event()

        def worker(progress):
            progress(10, "开始处理")
            started.set()
            while True:
                time.sleep(0.01)
                progress(20, "等待下一步")

        task = manager.start("可取消任务", worker)
        self.assertTrue(started.wait(2))
        cancelled = manager.cancel(task.id)
        self.assertTrue(cancelled["cancel_requested"])

        deadline = time.time() + 2
        state = None
        while time.time() < deadline:
            state = manager.as_dict(task.id)
            if state and state["status"] == "cancelled":
                break
            time.sleep(0.01)

        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "cancelled")
        self.assertTrue(state["cancel_requested"])
        self.assertIsNotNone(state["cancelled_at"])
        messages = [entry["message"] for entry in state["logs"]]
        self.assertTrue(any("取消请求" in message for message in messages))
        self.assertTrue(any("安全取消点" in message for message in messages))

    def test_failed_task_keeps_error_and_traceback_in_logs(self) -> None:
        manager = TaskManager()

        def worker(progress):
            progress.log("准备触发测试异常", "debug")
            raise ValueError("boom")

        task = manager.start("失败任务", worker)
        deadline = time.time() + 2
        state = None
        while time.time() < deadline:
            state = manager.as_dict(task.id)
            if state and state["status"] == "failed":
                break
            time.sleep(0.01)

        self.assertIsNotNone(state)
        self.assertEqual(state["status"], "failed")
        self.assertEqual(state["error"], "boom")
        logs = state["logs"]
        self.assertTrue(any(entry["level"] == "error" and "boom" in entry["message"] for entry in logs))
        self.assertTrue(any(entry["level"] == "debug" and "Traceback" in entry["message"] for entry in logs))


if __name__ == "__main__":
    unittest.main()
