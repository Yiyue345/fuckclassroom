from __future__ import annotations

import asyncio
import json
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace

from fuckclassroom.plugins.process_runtime import ProcessPluginHost
from fuckclassroom.plugins.process_worker import _CallProgress
from fuckclassroom.plugins.rpc import PLUGIN_RPC_API_VERSION, PluginRpcRegistry


class _Progress:
    def __init__(self) -> None:
        self.events: list[tuple[object, ...]] = []
        self.cancel_requested = False

    def __call__(self, progress: int, message: str) -> None:
        self.events.append(("progress", progress, message))

    def detail(self, progress: int, message: str) -> None:
        self.events.append(("detail", progress, message))

    def clear_detail(self) -> None:
        self.events.append(("clear_detail",))

    def log(self, message: str, level: str = "info") -> None:
        self.events.append(("log", level, message))

    def raise_if_cancelled(self) -> None:
        if self.cancel_requested:
            raise RuntimeError("cancelled")


class CallProgressConcurrencyTests(unittest.TestCase):
    def test_event_and_acknowledgement_are_one_transaction(self) -> None:
        first_read = threading.Event()
        second_started = threading.Event()
        second_write = threading.Event()
        release_ack = threading.Event()
        writes: list[dict[str, object]] = []
        errors: list[BaseException] = []

        def write(payload):
            writes.append(payload)
            if len(writes) == 2:
                second_write.set()

        def readline(limit):
            first_read.set()
            if not release_ack.wait(3):
                raise TimeoutError("test acknowledgement not released")
            return json.dumps({"ok": True, "cancel_requested": False}).encode() + b"\n"

        progress = _CallProgress(SimpleNamespace(
            _write=write, rfile=SimpleNamespace(readline=readline),
        ))

        def report(second=False):
            try:
                if second:
                    second_started.set()
                progress.raise_if_cancelled()
            except BaseException as exc:
                errors.append(exc)

        first = threading.Thread(target=report)
        second = threading.Thread(target=report, args=(True,))
        first.start()
        try:
            self.assertTrue(first_read.wait(1))
            second.start()
            self.assertTrue(second_started.wait(1))
            self.assertFalse(
                second_write.wait(0.2),
                "another thread sent an event before the first acknowledgement was read",
            )
        finally:
            release_ack.set()
            first.join(3)
            if second.ident is not None:
                second.join(3)
        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(writes), 2)


class ProcessServiceCallTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_progress_and_decode_failure_finish_over_real_ipc(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "worker.py").write_text(
                '''
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from fuckclassroom.plugins.transcription import engine

def handle_call(method, params, context, progress):
    if method == "demo.concurrent":
        def report(index):
            for step in range(20):
                progress.log(f"{index}:{step}")
                progress.raise_if_cancelled()
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(report, range(3)))
        return "all acknowledged"

    other_started = threading.Event()
    def decode(url, start, end, rate, cancel_check):
        if start == 0:
            if not other_started.wait(1):
                raise RuntimeError("second decoder did not start")
            raise EOFError("media connection ended")
        other_started.set()
        while True:
            cancel_check()
            time.sleep(0.001)

    engine._decode_audio_chunk = decode
    service = engine.LocalTranscriptionService(SimpleNamespace(
        audio_prefetch_queue_size=1, asr_use_batched=False, asr_batch_size=1,
        asr_language="zh", asr_simplified_chinese=False,
        asr_max_concurrent_tasks=1,
    ))
    service._can_pipeline_remote_audio = lambda *args: True
    class Model:
        def transcribe(self, audio, **kwargs):
            return iter([SimpleNamespace(start=0, end=1, text="fallback result")]), SimpleNamespace(duration=1200)
    lines, duration = service._transcribe_with_model(
        Model(), "https://example.test/video", 1200, progress,
    )
    return {"lines": lines, "duration": duration}
'''.lstrip(),
                encoding="utf-8",
            )
            host = ProcessPluginHost(
                plugin_id="concurrent-service-test", root=root, entry="worker.py",
                data_dir=root / "data", rpc_registry=PluginRpcRegistry(),
                rpc_api_version=PLUGIN_RPC_API_VERSION, rpc_permissions=(),
            )
            await host.start()
            try:
                progress = _Progress()
                result = await asyncio.to_thread(
                    host.call_sync, "demo.concurrent", progress=progress, timeout=3,
                )
                self.assertEqual(result, "all acknowledged")
                self.assertEqual(len(progress.events), 60)

                class CancelProgress(_Progress):
                    def log(self, message, level="info"):
                        super().log(message, level)
                        self.cancel_requested = True

                with self.assertRaisesRegex(RuntimeError, "cancelled"):
                    await asyncio.to_thread(
                        host.call_sync, "demo.concurrent", progress=CancelProgress(), timeout=3,
                    )

                result = await asyncio.to_thread(
                    host.call_sync, "demo.decode_failure", progress=progress, timeout=3,
                )
                self.assertEqual(result["lines"], ["[00:00:00 - 00:00:01] fallback result"])
                self.assertTrue(any("自动回退" in str(event) for event in progress.events))
            finally:
                await host.stop()

    async def test_service_invoke_streams_progress_and_handles_dash_prefixed_tokens(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "worker.py").write_text(
                """
def handle_call(method, params, context, progress):
    if method != "demo.echo":
        raise ValueError(method)
    progress(10, "started")
    progress.detail(50, "halfway")
    progress.log("worker log", "debug")
    progress.clear_detail()
    progress.raise_if_cancelled()
    return {
        "value": params.get("value"),
        "plugin_id": context.plugin_id,
    }
""".lstrip(),
                encoding="utf-8",
            )

            host = ProcessPluginHost(
                plugin_id="service-test",
                root=root,
                entry="worker.py",
                data_dir=root / "data",
                rpc_registry=PluginRpcRegistry(),
                rpc_api_version=PLUGIN_RPC_API_VERSION,
                rpc_permissions=(),
            )
            # Regression: argparse used to treat a random token beginning with "-"
            # as another command-line option.
            host._token = "-child-token"  # type: ignore[attr-defined]
            host._parent_rpc_token = "-parent-token"  # type: ignore[attr-defined]
            progress = _Progress()

            await host.start()
            try:
                result = await asyncio.to_thread(
                    host.call_sync,
                    "demo.echo",
                    {"value": 42},
                    progress=progress,
                )
                self.assertEqual(
                    result,
                    {"value": 42, "plugin_id": "service-test"},
                )
                self.assertEqual(
                    progress.events,
                    [
                        ("progress", 10, "started"),
                        ("detail", 50, "halfway"),
                        ("log", "debug", "worker log"),
                        ("clear_detail",),
                    ],
                )
            finally:
                await host.stop()

            self.assertFalse(host.running)


if __name__ == "__main__":
    unittest.main()
