from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable


@dataclass(frozen=True)
class Semester:
    id: str
    name: str
    start_time: str = ""
    end_time: str = ""


class SemesterSyncService:
    """Synchronize semester data with non-blocking cache fallback."""

    def __init__(self, cache_path: Path, expire_seconds: int = 24 * 3600) -> None:
        self.cache_path = cache_path
        self.expire_seconds = expire_seconds

    def sync(self, fetcher: Callable[[], Iterable[Semester]]) -> list[Semester]:
        try:
            semesters = list(fetcher())
            self._save(semesters)
            return semesters
        except Exception:
            return self.load_cache(ignore_expire=True)

    def load_cache(self, ignore_expire: bool = False) -> list[Semester]:
        if not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            updated_at = float(payload.get("updated_at", 0))
            if not ignore_expire and time.time() - updated_at > self.expire_seconds:
                return []
            return [Semester(**item) for item in payload.get("items", [])]
        except (OSError, ValueError, TypeError):
            return []

    def current_semester(self, semesters: list[Semester] | None = None) -> Semester | None:
        items = semesters if semesters is not None else self.load_cache()
        if not items:
            return None

        now = time.strftime("%Y-%m-%d")
        matched = [
            item
            for item in items
            if item.start_time <= now <= item.end_time
            and item.start_time
            and item.end_time
        ]
        if matched:
            return matched[-1]
        return items[-1]

    def refresh_current_semester(
        self,
        fetcher: Callable[[], Iterable[Semester]],
    ) -> Semester | None:
        return self.current_semester(self.sync(fetcher))

    def _save(self, semesters: list[Semester]) -> None:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.cache_path.write_text(
            json.dumps(
                {
                    "updated_at": time.time(),
                    "items": [item.__dict__ for item in semesters],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
