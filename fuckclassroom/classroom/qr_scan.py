from __future__ import annotations

from dataclasses import dataclass, field
import importlib
import json
from pathlib import Path
import threading
from typing import Any
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

from ..core.config import AppConfig


DETECT_MODEL_URL = "https://raw.githubusercontent.com/wilinz/wxscan-weights/main/models/detect.onnx"
SR_MODEL_URL = "https://raw.githubusercontent.com/wilinz/wxscan-weights/main/models/sr.onnx"
_MODEL_SPECS = {
    "detect.onnx": (DETECT_MODEL_URL, 900_000),
    "sr.onnx": (SR_MODEL_URL, 20_000),
}

_CHANGKE_ESC_BANG = chr(30)
_CHANGKE_ESC_TILDE = chr(31)
_CHANGKE_TYPED = chr(26)
_CHANGKE_NUMBER = chr(16)
_CHANGKE_TRUE = _CHANGKE_TYPED + "1"
_CHANGKE_FALSE = _CHANGKE_TYPED + "0"
_CHANGKE_KEYS = [
    "courseId",
    "activityId",
    "activityType",
    "data",
    "rollcallId",
    "groupSetId",
    "accessCode",
    "action",
    "enableGroupRollcall",
    "createUser",
    "joinCourse",
]


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value < 0:
        return "-" + _base36(-value)
    if value < 36:
        return alphabet[value]
    result = ""
    while value:
        value, remainder = divmod(value, 36)
        result = alphabet[remainder] + result
    return result or "0"


_CHANGKE_KEY_BY_CODE = {_base36(index): key for index, key in enumerate(_CHANGKE_KEYS)}
_CHANGKE_ENUM_BY_CODE = {
    _CHANGKE_TYPED + _base36(index + 2): value
    for index, value in enumerate(("classroom-exam", "feedback", "vote"))
}


class QRScannerError(RuntimeError):
    pass


class QRScannerUnavailable(QRScannerError):
    pass


@dataclass(frozen=True)
class QRPayload:
    platform: str
    label: str
    kind: str
    raw: str
    action_url: str = ""
    fields: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "label": self.label,
            "kind": self.kind,
            "raw": self.raw,
            "action_url": self.action_url,
            "fields": self.fields,
        }


def parse_changke_sign_payload(payload: str) -> dict[str, Any]:
    """Decode the compact `p` payload used by Changke scanner-jumper links."""
    result: dict[str, Any] = {}
    for part in payload.split("!"):
        if not part or "~" not in part:
            continue
        raw_key, raw_value = part.split("~", 1)
        key = _CHANGKE_KEY_BY_CODE.get(raw_key, raw_key)
        value: Any = raw_value
        if raw_value.startswith(_CHANGKE_TYPED):
            if raw_value == _CHANGKE_TRUE:
                value = True
            elif raw_value == _CHANGKE_FALSE:
                value = False
            else:
                value = _CHANGKE_ENUM_BY_CODE.get(raw_value, raw_value)
        elif raw_value.startswith(_CHANGKE_NUMBER):
            pieces = raw_value[1:].split(".")
            try:
                numbers = [int(piece, 36) for piece in pieces]
            except ValueError:
                numbers = []
            if len(numbers) > 1:
                value = float(f"{numbers[0]}.{numbers[1]}")
            elif numbers:
                value = numbers[0]
        else:
            value = raw_value.replace(_CHANGKE_ESC_TILDE, "~").replace(_CHANGKE_ESC_BANG, "!")
        result[key] = value
    return result


def parse_changke_scan_url(raw: str) -> dict[str, Any] | None:
    value = raw.strip()
    if not value:
        return None
    parsed = urlparse(value)
    if parsed.path not in {"/j", "/scanner-jumper"}:
        return None
    params = parse_qs(parsed.query, keep_blank_values=True)
    packed_json = (params.get("_p") or [""])[0]
    if packed_json:
        try:
            decoded = json.loads(packed_json)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict) and decoded:
            return decoded
    compact = (params.get("p") or [""])[0]
    decoded = parse_changke_sign_payload(compact)
    return decoded or None


def analyze_qr_payload(raw: str) -> QRPayload:
    value = raw.strip()
    fields = parse_changke_scan_url(value)
    if fields is not None:
        parsed = urlparse(value)
        action_url = value if parsed.scheme in {"http", "https"} else ""
        is_rollcall = any(key in fields for key in ("rollcallId", "activityId", "accessCode"))
        return QRPayload(
            platform="changke",
            label="畅课",
            kind="rollcall" if is_rollcall else "link",
            raw=value,
            action_url=action_url,
            fields=fields,
        )

    parsed = urlparse(value)
    hostname = (parsed.hostname or "").lower()
    if hostname == "ketangpai.com" or hostname.endswith(".ketangpai.com"):
        return QRPayload(
            platform="ketangpai",
            label="课堂派",
            kind="rollcall-link",
            raw=value,
            action_url=value if parsed.scheme in {"http", "https"} else "",
        )
    if parsed.scheme in {"http", "https"}:
        return QRPayload(platform="web", label="网页二维码", kind="link", raw=value, action_url=value)
    return QRPayload(platform="unknown", label="二维码", kind="text", raw=value)


class WxScanService:
    """Lazy, process-local wrapper around the PyO3 wxscan extension."""

    def __init__(self, config: AppConfig) -> None:
        self.model_dir = config.data_dir / "models" / "wxscan"
        self._scanner: Any | None = None
        self._init_lock = threading.Lock()

    def scan_image(self, image_bytes: bytes) -> list[str]:
        scanner = self._get_scanner()
        try:
            return [str(item) for item in scanner.scan(image_bytes) if str(item)]
        except Exception as exc:  # noqa: BLE001 - native errors need a stable Python boundary.
            raise QRScannerError(f"二维码识别失败：{exc}") from exc

    def _get_scanner(self):
        if self._scanner is not None:
            return self._scanner
        with self._init_lock:
            if self._scanner is not None:
                return self._scanner
            try:
                native = importlib.import_module("fuckclassroom_wxscan")
            except ImportError as exc:
                raise QRScannerUnavailable(
                    "未安装 wxscan PyO3 扩展，请在 rust_module/wxscan_py 中执行 "
                    "`python -m pip install maturin && maturin develop --release`"
                ) from exc
            detect_path, sr_path = self._ensure_models()
            try:
                self._scanner = native.Scanner(detect_path.read_bytes(), sr_path.read_bytes())
            except Exception as exc:  # noqa: BLE001
                raise QRScannerUnavailable(f"wxscan 模型加载失败：{exc}") from exc
            return self._scanner

    def _ensure_models(self) -> tuple[Path, Path]:
        self.model_dir.mkdir(parents=True, exist_ok=True)
        paths: dict[str, Path] = {}
        for filename, (url, minimum_size) in _MODEL_SPECS.items():
            target = self.model_dir / filename
            if not target.exists() or target.stat().st_size < minimum_size:
                self._download_model(url, target, minimum_size)
            paths[filename] = target
        return paths["detect.onnx"], paths["sr.onnx"]

    @staticmethod
    def _download_model(url: str, target: Path, minimum_size: int) -> None:
        temporary = target.with_suffix(target.suffix + ".part")
        request = Request(url, headers={"User-Agent": "fuckclassroom-wxscan/0.1"})
        try:
            with urlopen(request, timeout=30) as response, temporary.open("wb") as output:
                while chunk := response.read(256 * 1024):
                    output.write(chunk)
            if temporary.stat().st_size < minimum_size:
                raise QRScannerUnavailable(f"下载的模型文件异常：{target.name}")
            temporary.replace(target)
        except Exception as exc:  # noqa: BLE001 - network/filesystem failures share one UI error.
            temporary.unlink(missing_ok=True)
            if isinstance(exc, QRScannerUnavailable):
                raise
            raise QRScannerUnavailable(f"下载 wxscan 模型失败：{exc}") from exc
