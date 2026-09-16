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


if __name__ == "__main__":
    unittest.main()
