"""Persistent, application-wide Registry channel preference."""

from __future__ import annotations

import json
import tempfile
from pathlib import Path


class RegistryPreferences:
    """Saved in the application's data directory, not in browser storage."""

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / "plugin_registry_preferences.json"

    def include_beta(self) -> bool:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return False
        return isinstance(payload, dict) and payload.get("include_beta") is True

    def set_include_beta(self, enabled: bool) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                prefix=".plugin_registry_preferences-",
                suffix=".tmp",
                dir=self.path.parent,
                delete=False,
            ) as output:
                temporary = Path(output.name)
                json.dump({"include_beta": bool(enabled)}, output, ensure_ascii=False)
            temporary.replace(self.path)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
