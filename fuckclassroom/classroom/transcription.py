from __future__ import annotations

import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from ..core.config import AppConfig


class LocalTranscriptionError(RuntimeError):
    pass


ProgressCallback = Callable[[int, str], None]


@dataclass(frozen=True)
class LocalTranscriptionResult:
    path: Path
    text: str
    cached: bool
    duration_seconds: float | None = None


_MODEL_CACHE: dict[tuple[str, str, str, str], object] = {}
_MODEL_LOCK = threading.Lock()
_TRANSCRIPTION_LOCK = threading.Lock()


class LocalTranscriptionService:
    """Stream lesson media into faster-whisper without saving the full video locally."""

    def __init__(self, config: AppConfig | None = None) -> None:
        self.config = config or AppConfig()

    def transcribe_url(
        self,
        video_url: str,
        output_path: Path,
        progress: ProgressCallback | None = None,
        *,
        force: bool = False,
    ) -> LocalTranscriptionResult:
        if not video_url:
            raise LocalTranscriptionError("本课次没有可用的课堂录像")

        if output_path.exists() and not force:
            cached_text = output_path.read_text(encoding="utf-8").strip()
            if cached_text:
                _report(progress, 100, "已复用本地语音转写")
                return LocalTranscriptionResult(output_path, cached_text, True)

        _report(progress, 4, "正在加载本地语音识别模型；首次使用可能需要下载模型")
        model = self._get_model()

        # Running multiple Whisper jobs in parallel can easily exhaust RAM/VRAM on a laptop.
        # Serialize ASR while letting the rest of the application's background tasks remain concurrent.
        with _TRANSCRIPTION_LOCK:
            _report(progress, 12, "正在连接课堂录像并流式读取音频")
            try:
                segments, info = model.transcribe(
                    video_url,
                    language=self.config.asr_language or None,
                    beam_size=3,
                    vad_filter=True,
                    vad_parameters={"min_silence_duration_ms": 500},
                    condition_on_previous_text=True,
                )

                duration = float(getattr(info, "duration", 0.0) or 0.0)
                lines: list[str] = []
                for segment in segments:
                    text = str(getattr(segment, "text", "") or "").strip()
                    if not text:
                        continue
                    start = float(getattr(segment, "start", 0.0) or 0.0)
                    end = float(getattr(segment, "end", start) or start)
                    lines.append(f"[{_timestamp(start)} - {_timestamp(end)}] {text}")
                    if duration > 0:
                        percent = 15 + min(80, int((end / duration) * 80))
                        _report(progress, percent, f"正在本地转写：{_timestamp(end)} / {_timestamp(duration)}")
                    else:
                        _report(progress, 55, f"正在本地转写：已处理到 {_timestamp(end)}")
            except Exception as exc:  # faster-whisper/PyAV can raise several backend-specific errors.
                raise LocalTranscriptionError(f"本地语音转写失败：{exc}") from exc

        if not lines:
            raise LocalTranscriptionError("录像中没有识别到可用语音")

        text = "\n".join(lines).strip()
        output_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = output_path.with_name(f"{output_path.name}.tmp")
        temporary.write_text(text + "\n", encoding="utf-8")
        temporary.replace(output_path)
        _report(progress, 100, "本地语音转写完成")
        return LocalTranscriptionResult(
            path=output_path,
            text=text,
            cached=False,
            duration_seconds=duration or None,
        )

    def _get_model(self):
        try:
            from faster_whisper import WhisperModel
        except ImportError as exc:
            raise LocalTranscriptionError(
                "缺少 faster-whisper，请先重新安装 requirements.txt 中的依赖"
            ) from exc

        key = (
            self.config.asr_model,
            self.config.asr_device,
            self.config.asr_compute_type,
            str(self.config.asr_models_dir),
        )
        with _MODEL_LOCK:
            model = _MODEL_CACHE.get(key)
            if model is not None:
                return model
            try:
                self.config.asr_models_dir.mkdir(parents=True, exist_ok=True)
                model = WhisperModel(
                    self.config.asr_model,
                    device=self.config.asr_device,
                    compute_type=self.config.asr_compute_type,
                    download_root=str(self.config.asr_models_dir),
                )
            except Exception as exc:
                raise LocalTranscriptionError(f"语音识别模型加载失败：{exc}") from exc
            _MODEL_CACHE[key] = model
            return model


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _report(progress: ProgressCallback | None, percent: int, message: str) -> None:
    if progress:
        progress(max(0, min(100, int(percent))), message)
