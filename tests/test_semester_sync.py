from __future__ import annotations

from pathlib import Path

from fuckclassroom.classroom.semester_sync import Semester, SemesterSyncService


def test_sync_success_and_current_semester(tmp_path: Path):
    service = SemesterSyncService(tmp_path / "semester.json")

    semesters = service.sync(
        lambda: [
            Semester("1", "2025 春季", "2025-02-01", "2025-07-01"),
            Semester("2", "2025 秋季", "2025-09-01", "2026-01-20"),
        ]
    )

    assert len(semesters) == 2
    assert service.current_semester(semesters) is not None


def test_sync_failure_falls_back_to_cache(tmp_path: Path):
    service = SemesterSyncService(tmp_path / "semester.json")
    service.sync(lambda: [Semester("1", "缓存学期")])

    result = service.sync(lambda: (_ for _ in ()).throw(RuntimeError("network")))

    assert result[0].id == "1"


def test_expired_cache_is_ignored(tmp_path: Path):
    service = SemesterSyncService(tmp_path / "semester.json", expire_seconds=0)
    service.sync(lambda: [Semester("1", "old")])

    assert service.load_cache() == []
