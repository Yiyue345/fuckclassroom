from __future__ import annotations

import threading
from contextlib import contextmanager
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
    export_path: Path | None = None


_MODEL_CACHE: dict[tuple[object, ...], object] = {}
_MODEL_LOCK = threading.Lock()
_TRANSCRIPTION_CONDITION = threading.Condition()
_ACTIVE_TRANSCRIPTIONS = 0
_OPENCC_CONVERTER = None
_OPENCC_LOCK = threading.Lock()
_MODEL_ALLOW_PATTERNS = [
    "config.json",
    "preprocessor_config.json",
    "model.bin",
    "tokenizer.json",
    "vocabulary.*",
]


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
                normalized = self._normalize_text(cached_text)
                if normalized != cached_text:
                    output_path.write_text(normalized + "\n", encoding="utf-8")
                export_path = self._export_transcript(output_path, normalized)
                _clear_detail(progress)
                _report(progress, 100, "已复用本地语音转写")
                return LocalTranscriptionResult(
                    output_path,
                    normalized,
                    True,
                    export_path=export_path,
                )

        _report(progress, 4, "正在准备本地语音识别模型")
        model = self._get_model(progress)
        _clear_detail(progress)

        with _transcription_slot(self.config.asr_max_concurrent_tasks, progress):
            _report(
                progress,
                12,
                f"正在连接课堂录像并流式读取音频（并发上限 {self.config.asr_max_concurrent_tasks}）",
            )
            try:
                transcriber = model
                transcribe_kwargs = {
                    "language": self.config.asr_language or None,
                    "beam_size": 3,
                    "vad_filter": True,
                    "vad_parameters": {"min_silence_duration_ms": 500},
                    "condition_on_previous_text": True,
                }
                if self.config.asr_use_batched and self.config.asr_batch_size > 1:
                    from faster_whisper import BatchedInferencePipeline

                    # Pipeline instances keep a small amount of per-call state, so create one
                    # for each concurrent task while sharing the expensive WhisperModel.
                    transcriber = BatchedInferencePipeline(model=model)
                    transcribe_kwargs["batch_size"] = self.config.asr_batch_size

                segments, info = transcriber.transcribe(video_url, **transcribe_kwargs)
                duration = float(getattr(info, "duration", 0.0) or 0.0)
                lines: list[str] = []
                for segment in segments:
                    raw_text = str(getattr(segment, "text", "") or "").strip()
                    text = self._normalize_text(raw_text)
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
        export_path = self._export_transcript(output_path, text)
        _report(progress, 100, "本地语音转写完成，结果已保存到文件管理")
        return LocalTranscriptionResult(
            path=output_path,
            text=text,
            cached=False,
            duration_seconds=duration or None,
            export_path=export_path,
        )

    def _normalize_text(self, text: str) -> str:
        if not text or not self.config.asr_simplified_chinese:
            return text
        language = (self.config.asr_language or "").lower()
        if language and not language.startswith("zh"):
            return text
        return _to_simplified_chinese(text)

    def _export_transcript(self, output_path: Path, text: str) -> Path:
        try:
            relative_parent = output_path.resolve().parent.relative_to(
                self.config.outputs_dir.resolve()
            )
        except ValueError:
            relative_parent = Path()
        export_path = self.config.downloads_dir / relative_parent / "本地转写.txt"
        export_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = export_path.with_name(f"{export_path.name}.tmp")
        temporary.write_text(text.rstrip() + "\n", encoding="utf-8")
        temporary.replace(export_path)
        return export_path

    def _get_model(self, progress: ProgressCallback | None = None):
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
            self.config.asr_cpu_threads,
            self.config.asr_num_workers,
            str(self.config.asr_models_dir),
        )
        with _MODEL_LOCK:
            model = _MODEL_CACHE.get(key)
            if model is not None:
                return model
            try:
                self.config.asr_models_dir.mkdir(parents=True, exist_ok=True)
                model_path = self._ensure_model_files(progress)
                _report(progress, 8, "正在加载本地语音识别模型")
                model = WhisperModel(
                    model_path,
                    device=self.config.asr_device,
                    compute_type=self.config.asr_compute_type,
                    cpu_threads=self.config.asr_cpu_threads,
                    num_workers=self.config.asr_num_workers,
                    local_files_only=True,
                )
            except LocalTranscriptionError:
                raise
            except Exception as exc:
                raise LocalTranscriptionError(f"语音识别模型加载失败：{exc}") from exc
            _MODEL_CACHE[key] = model
            return model

    def _ensure_model_files(self, progress: ProgressCallback | None = None) -> str:
        configured = self.config.asr_model
        local_candidate = Path(configured).expanduser()
        if local_candidate.is_dir():
            return str(local_candidate)

        try:
            from faster_whisper.utils import _MODELS as model_repositories
            from huggingface_hub import hf_hub_download, snapshot_download
            from tqdm.auto import tqdm
        except ImportError as exc:
            raise LocalTranscriptionError(
                "缺少模型下载依赖，请重新安装 requirements.txt"
            ) from exc

        repo_id = configured if "/" in configured else model_repositories.get(configured)
        if not repo_id:
            raise LocalTranscriptionError(f"不支持的 faster-whisper 模型：{configured}")

        cache_dir = str(self.config.asr_models_dir)
        _report_detail(progress, 0, f"正在检查语音模型 {configured} 的本地缓存")
        try:
            files = snapshot_download(
                repo_id,
                cache_dir=cache_dir,
                allow_patterns=_MODEL_ALLOW_PATTERNS,
                dry_run=True,
            )
        except TypeError as exc:
            raise LocalTranscriptionError(
                "huggingface-hub 版本过旧，无法显示模型下载进度；请重新安装 requirements.txt"
            ) from exc
        except Exception as exc:
            raise LocalTranscriptionError(f"读取语音模型信息失败：{exc}") from exc

        pending = [item for item in files if getattr(item, "will_download", False)]
        model_total = sum(int(getattr(item, "file_size", 0) or 0) for item in files)
        download_total = sum(int(getattr(item, "file_size", 0) or 0) for item in pending)
        revision = next((str(getattr(item, "commit_hash", "") or "") for item in files), "") or None

        if not pending:
            if model_total:
                _report_detail(
                    progress,
                    100,
                    f"语音模型已缓存（{_format_bytes(model_total)}）",
                )
            try:
                return str(
                    snapshot_download(
                        repo_id,
                        cache_dir=cache_dir,
                        allow_patterns=_MODEL_ALLOW_PATTERNS,
                        revision=revision,
                        local_files_only=True,
                    )
                )
            except Exception as exc:
                raise LocalTranscriptionError(f"读取本地语音模型失败：{exc}") from exc

        size_label = _format_bytes(download_total) if download_total else f"{len(pending)} 个文件"
        _report_detail(progress, 0, f"首次使用：需要下载语音模型 {configured}，共 {size_label}")

        completed_bytes = 0
        completed_files = 0
        total_files = len(pending)

        for item in pending:
            filename = str(getattr(item, "filename", "") or "")
            expected_size = int(getattr(item, "file_size", 0) or 0)
            if not filename:
                continue

            base_bytes = completed_bytes
            file_index = completed_files

            class DownloadProgress(tqdm):
                def display(self, *args, **kwargs):  # noqa: ANN002, ANN003 - tqdm API compatibility.
                    return None

                def update(self, n=1):
                    result = super().update(n)
                    current = max(0, int(getattr(self, "n", 0) or 0))
                    if expected_size:
                        current = min(current, expected_size)
                    if download_total:
                        overall = min(download_total, base_bytes + current)
                        percent = int((overall / download_total) * 100)
                        message = (
                            f"正在下载语音模型 {configured}："
                            f"{_format_bytes(overall)} / {_format_bytes(download_total)}（{percent}%）"
                        )
                    else:
                        local_total = max(1, int(getattr(self, "total", 0) or 1))
                        fraction = min(1.0, current / local_total)
                        percent = int(((file_index + fraction) / max(1, total_files)) * 100)
                        message = f"正在下载语音模型 {configured}：{percent}%"
                    _report_detail(progress, percent, message)
                    return result

            try:
                hf_hub_download(
                    repo_id,
                    filename=filename,
                    cache_dir=cache_dir,
                    revision=revision,
                    tqdm_class=DownloadProgress,
                )
            except Exception as exc:
                raise LocalTranscriptionError(f"语音模型下载失败（{filename}）：{exc}") from exc

            completed_files += 1
            if expected_size:
                completed_bytes += expected_size
            if download_total:
                percent = int((min(download_total, completed_bytes) / download_total) * 100)
                _report_detail(
                    progress,
                    percent,
                    f"正在下载语音模型 {configured}："
                    f"{_format_bytes(min(download_total, completed_bytes))} / {_format_bytes(download_total)}（{percent}%）",
                )
            else:
                percent = int((completed_files / max(1, total_files)) * 100)
                _report_detail(progress, percent, f"正在下载语音模型 {configured}：{percent}%")

        _report_detail(
            progress,
            100,
            f"语音模型 {configured} 下载完成（{_format_bytes(model_total or download_total)}）",
        )
        try:
            return str(
                snapshot_download(
                    repo_id,
                    cache_dir=cache_dir,
                    allow_patterns=_MODEL_ALLOW_PATTERNS,
                    revision=revision,
                    local_files_only=True,
                )
            )
        except Exception as exc:
            raise LocalTranscriptionError(f"语音模型下载完成但本地快照不可用：{exc}") from exc


@contextmanager
def _transcription_slot(limit: int, progress: ProgressCallback | None = None):
    global _ACTIVE_TRANSCRIPTIONS
    limit = max(1, int(limit))
    with _TRANSCRIPTION_CONDITION:
        while _ACTIVE_TRANSCRIPTIONS >= limit:
            _report(
                progress,
                10,
                f"正在等待本地转写槽位：{_ACTIVE_TRANSCRIPTIONS}/{limit} 个任务运行中",
            )
            _TRANSCRIPTION_CONDITION.wait(timeout=1.0)
        _ACTIVE_TRANSCRIPTIONS += 1
        active = _ACTIVE_TRANSCRIPTIONS
    _report(progress, 11, f"已进入本地转写队列：{active}/{limit} 个任务运行中")
    try:
        yield
    finally:
        with _TRANSCRIPTION_CONDITION:
            _ACTIVE_TRANSCRIPTIONS = max(0, _ACTIVE_TRANSCRIPTIONS - 1)
            _TRANSCRIPTION_CONDITION.notify_all()


def _to_simplified_chinese(text: str) -> str:
    global _OPENCC_CONVERTER
    with _OPENCC_LOCK:
        if _OPENCC_CONVERTER is None:
            try:
                from opencc import OpenCC
            except ImportError as exc:
                raise LocalTranscriptionError(
                    "缺少 OpenCC 简繁转换依赖，请重新安装 requirements.txt"
                ) from exc
            _OPENCC_CONVERTER = OpenCC("t2s")
        converter = _OPENCC_CONVERTER
    return converter.convert(text)


def _timestamp(seconds: float) -> str:
    total = max(0, int(seconds))
    hours, remainder = divmod(total, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def _format_bytes(value: int) -> str:
    size = max(0, int(value))
    units = ("B", "KB", "MB", "GB")
    amount = float(size)
    for unit in units:
        if amount < 1024 or unit == units[-1]:
            if unit == "B":
                return f"{int(amount)} {unit}"
            return f"{amount:.1f} {unit}"
        amount /= 1024
    return f"{size} B"


def _report(progress: ProgressCallback | None, percent: int, message: str) -> None:
    if progress:
        progress(max(0, min(100, int(percent))), message)


def _report_detail(progress: ProgressCallback | None, percent: int, message: str) -> None:
    if progress is None:
        return
    detail = getattr(progress, "detail", None)
    if callable(detail):
        detail(max(0, min(100, int(percent))), message)
    else:
        progress(4, message)


def _clear_detail(progress: ProgressCallback | None) -> None:
    if progress is None:
        return
    clear_detail = getattr(progress, "clear_detail", None)
    if callable(clear_detail):
        clear_detail()
