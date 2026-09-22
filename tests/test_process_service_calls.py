from __future__ import annotations

import asyncio
import tempfile
import unittest
from pathlib import Path

from fuckclassroom.plugins.process_runtime import ProcessPluginHost
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


class ProcessServiceCallTests(unittest.IsolatedAsyncioTestCase):
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
