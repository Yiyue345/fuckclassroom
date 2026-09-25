from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from fuckclassroom.core.config import AppConfig
from fuckclassroom.web.reloadable import create_reloadable_app


class ReloadableTaskTests(unittest.TestCase):
    def test_generations_share_the_same_task_manager(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            shell = create_reloadable_app(
                AppConfig(data_dir=Path(directory)),
                enabled_plugins=("core_ui",),
            )

            first = shell._app_factory(lambda: True, lambda: {})
            second = shell._app_factory(lambda: True, lambda: {})
            first_manager = first.state.plugin_runtime.services.get("task_manager")
            second_manager = second.state.plugin_runtime.services.get("task_manager")

            self.assertIs(first_manager, second_manager)
            task = first_manager.create_external("跨代任务")
            self.assertIs(second_manager.get(task.id), task)


if __name__ == "__main__":
    unittest.main()
