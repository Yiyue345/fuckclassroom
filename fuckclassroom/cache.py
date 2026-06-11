from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class CacheInfo:
    file_count: int
    total_bytes: int


class JsonCache:
    def __init__(self, root: Path) -> None:
        self.root = root

    def get(self, name: str) -> dict[str, Any] | None:
        path = self._path(name)
        if not path.exists():
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        data = payload.get("data")
        return data if isinstance(data, dict) else None

    def set(self, name: str, data: dict[str, Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        payload = {
            "updated_at": datetime.now().isoformat(timespec="seconds"),
            "data": data,
        }
        self._path(name).write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def clear(self) -> None:
        if self.root.exists():
            shutil.rmtree(self.root)

    def info(self) -> CacheInfo:
        if not self.root.exists():
            return CacheInfo(0, 0)
        files = [path for path in self.root.rglob("*.json") if path.is_file()]
        return CacheInfo(
            file_count=len(files),
            total_bytes=sum(path.stat().st_size for path in files),
        )

    def _path(self, name: str) -> Path:
        safe_name = "".join(char if char.isalnum() or char in "._-" else "_" for char in name)
        return self.root / f"{safe_name}.json"
