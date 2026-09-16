from __future__ import annotations

import asyncio
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from fuckclassroom.auth import LoginSessionService, SessionStatus
from fuckclassroom.classroom.semester_sync import SemesterSnapshot, SemesterSyncService
from fuckclassroom.config import AppConfig
from fuckclassroom.course_selection import CourseSelectionAssistant, WebVpnSessionStatus
from fuckclassroom.course_selection.automation import AutoSelectionService
from fuckclassroom.web import create_app


class StartupSessionRecoveryTests(unittest.TestCase):
    def test_startup_verification_enables_auto_relogin_for_both_systems(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            with (
                patch.object(
                    LoginSessionService,
                    "verify_saved_session",
                    return_value=SessionStatus(True, "课程录播会话已恢复"),
                ) as classroom_verify,
                patch.object(
                    CourseSelectionAssistant,
                    "verify_saved_session",
                    return_value=WebVpnSessionStatus(True, "教务会话已恢复", is_valid=True),
                ) as selection_verify,
                patch.object(
                    CourseSelectionAssistant,
                    "refresh_selection_info_for_web",
                    return_value="/selection/info",
                ) as selection_refresh,
                patch.object(
                    SemesterSyncService,
                    "sync",
                    return_value=SemesterSnapshot((), "", datetime.now(timezone.utc)),
                ) as semester_sync,
                patch.object(AutoSelectionService, "start"),
                patch.object(AutoSelectionService, "stop"),
            ):
                app = create_app(AppConfig(data_dir=Path(directory)))

                async def run_lifespan() -> None:
                    async with app.router.lifespan_context(app):
                        await asyncio.sleep(0.05)

                asyncio.run(run_lifespan())

        classroom_verify.assert_called_once_with(auto_relogin=True)
        selection_verify.assert_called_once_with(timeout_ms=60_000, auto_relogin=True)
        selection_refresh.assert_called_once_with(timeout_ms=60_000)
        semester_sync.assert_called_once_with(force=True)


if __name__ == "__main__":
    unittest.main()
