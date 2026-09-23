from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .errors import LocalTranscriptionError
from ...core.plugins import PluginContext
from ..process_runtime import ProcessPluginError, ProcessPluginHost
from ..rpc import PLUGIN_RPC_API_VERSION


@dataclass(frozen=True)
class RemoteTranscriptionResult:
    path: Path
    text: str
    cached: bool
    duration_seconds: float | None = None
    export_path: Path | None = None


class TranscriptionProcessProxy:
    def __init__(self, host: ProcessPluginHost) -> None:
        self.host = host

    def transcribe_url(
        self,
        video_url: str,
        output_path: Path,
        progress=None,
        *,
        force: bool = False,
        duration_hint_seconds: float | None = None,
    ) -> RemoteTranscriptionResult:
        try:
            payload = self.host.call_sync(
                "transcription.transcribe_url",
                {
                    "video_url": video_url,
                    "output_path": str(output_path),
                    "force": bool(force),
                    "duration_hint_seconds": duration_hint_seconds,
                },
                progress=progress,
                timeout=8 * 60 * 60,
            )
        except ProcessPluginError as exc:
            raise LocalTranscriptionError(str(exc)) from exc

        if not isinstance(payload, dict):
            raise LocalTranscriptionError("语音转写子进程返回格式错误")
        export_path = str(payload.get("export_path") or "")
        duration = payload.get("duration_seconds")
        return RemoteTranscriptionResult(
            path=Path(str(payload.get("path") or output_path)),
            text=str(payload.get("text") or ""),
            cached=bool(payload.get("cached")),
            duration_seconds=float(duration) if duration is not None else None,
            export_path=Path(export_path) if export_path else None,
        )


def setup_services(context: PluginContext) -> None:
    host = ProcessPluginHost(
        plugin_id="transcription",
        root=Path(__file__).resolve().parent,
        entry="worker.py",
        data_dir=Path(context.config.data_dir),
        rpc_registry=context.services.get("plugin_rpc"),
        rpc_api_version=PLUGIN_RPC_API_VERSION,
        rpc_permissions=(),
    )
    context.services.add("transcription_process_host", host)
    context.services.add("transcription_service", TranscriptionProcessProxy(host))


async def startup(context: PluginContext) -> None:
    await context.services.get("transcription_process_host").start()


async def shutdown(context: PluginContext) -> None:
    await context.services.get("transcription_process_host").stop()


__all__ = [
    "RemoteTranscriptionResult",
    "TranscriptionProcessProxy",
    "setup_services",
    "shutdown",
    "startup",
]
