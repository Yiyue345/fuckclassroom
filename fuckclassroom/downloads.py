from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True)
class DownloadedFile:
    name: str
    relative_path: str
    size_bytes: int
    size_label: str
    updated_at: str


class DownloadLibrary:
    def __init__(self, root: Path) -> None:
        self.root = root

    def list_files(self) -> list[DownloadedFile]:
        if not self.root.exists():
            return []
        files: list[DownloadedFile] = []
        for path in sorted(self.root.rglob("*")):
            if not path.is_file():
                continue
            stat = path.stat()
            files.append(
                DownloadedFile(
                    name=path.name,
                    relative_path=path.relative_to(self.root).as_posix(),
                    size_bytes=stat.st_size,
                    size_label=_format_size(stat.st_size),
                    updated_at=datetime.fromtimestamp(stat.st_mtime).strftime("%Y-%m-%d %H:%M"),
                )
            )
        return files

    def delete(self, relative_path: str) -> None:
        path = self.resolve(relative_path)
        path.unlink()
        self._remove_empty_parents(path.parent)

    def resolve(self, relative_path: str) -> Path:
        root = self.root.resolve()
        path = (self.root / relative_path).resolve()
        if path != root and root not in path.parents:
            raise ValueError("文件路径非法")
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        return path

    def _remove_empty_parents(self, start: Path) -> None:
        root = self.root.resolve()
        current = start.resolve()
        while current != root and root in current.parents:
            try:
                current.rmdir()
            except OSError:
                break
            current = current.parent


def _format_size(size_bytes: int) -> str:
    value = float(size_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if value < 1024 or unit == "TB":
            if unit == "B":
                return f"{int(value)} B"
            return f"{value:.1f} {unit}"
        value /= 1024
    return f"{size_bytes} B"
