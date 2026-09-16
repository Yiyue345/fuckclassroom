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
    """Manage semester synchronization with local cache fallback.

    The network layer is injected so the service can be used by both web
    routes and background startup tasks without coupling to ClassroomClient.
    """

    def __init__(self, cache_path: Path, expire_seconds: int = 24 * 3600) -> None:
        self.cache_path = cache_path
        self.expire_seconds = expire_seconds

    def sync(
        self,
        fetcher: Callable[[], Iterable[Semester]],
    ) -> list[Semester]:
        try:
            semesters = list(fetcher())
            self._save(semesters)
            return semesters
        except Exception:
            return self.load_cache()

    def load_cache(self) -> list[Semester]:
        if not self.cache_path.exists():
            return []
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
            if time.time() - float(payload.get("updated_at", 0)) > self.expire_seconds:
                return []
            return [Semester(**item) for item in payload.get("items", [])]
        except (OSError, ValueError, TypeError):
            return []

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
