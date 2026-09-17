from __future__ import annotations

import json
import os
import socket
import socketserver
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler

from fuckclassroom.config import AppConfig
from fuckclassroom.course_selection import CourseSelectionAssistant, _selection_access
from fuckclassroom.course_selection.proxy import (
    Hy2ProxyError,
    Hy2ProxyManager,
    _ConnectProxyServer,
    _SocksEndpoint,
    _parse_authority,
)


class _FakeSocksServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True


class _FakeSocksHandler(socketserver.BaseRequestHandler):
    def handle(self) -> None:
        self.assertEqual(_recv_exact(self.request, 3), b"\x05\x01\x02")
        self.request.sendall(b"\x05\x02")
        version, user_length = _recv_exact(self.request, 2)
        self.assertEqual(version, 1)
        username = _recv_exact(self.request, user_length)
        password_length = _recv_exact(self.request, 1)[0]
        password = _recv_exact(self.request, password_length)
        self.assertEqual((username, password), (b"user", b"pass"))
        self.request.sendall(b"\x01\x00")

        request = _recv_exact(self.request, 4)
        self.assertEqual(request[:3], b"\x05\x01\x00")
        if request[3] == 1:
            _recv_exact(self.request, 4)
        elif request[3] == 4:
            _recv_exact(self.request, 16)
        else:
            _recv_exact(self.request, _recv_exact(self.request, 1)[0])
        _recv_exact(self.request, 2)
        self.request.sendall(b"\x05\x00\x00\x01\x00\x00\x00\x00\x00\x00")
        while True:
            data = self.request.recv(4096)
            if not data:
                return
            self.request.sendall(data)

    def assertEqual(self, left: object, right: object) -> None:
        if left != right:
            raise AssertionError(f"{left!r} != {right!r}")


class _FakeBuildStdout:
    def __init__(self, process: "_FakeBuildProcess", lines: list[str]) -> None:
        self.process = process
        self.lines = iter(lines)

    def __iter__(self):
        return self

    def __next__(self) -> str:
        try:
            return next(self.lines)
        except StopIteration:
            self.process.done = True
            raise


class _FakeBuildProcess:
    def __init__(self, lines: list[str] | None = None, returncode: int = 0) -> None:
        self.done = False
        self.returncode = returncode
        self.stdout = _FakeBuildStdout(
            self,
            lines or ["Compiling test-crate v0.1.0\n", "Finished release profile\n"],
        )

    def poll(self):
        return self.returncode if self.done else None

    def wait(self, timeout=None):
        del timeout
        self.done = True
        return self.returncode

    def kill(self) -> None:
        self.done = True


class _FailingOpener:
    def open(self, *_args, **_kwargs):
        raise URLError("offline")


class _ErrorStatusOpener:
    def __init__(self, status: int) -> None:
        self.status = status

    def open(self, request, *_args, **_kwargs):
        raise HTTPError(request.full_url, self.status, "unavailable", {}, None)


class _Response:
    status = 200

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None


class _SuccessfulOpener:
    def open(self, *_args, **_kwargs):
        return _Response()


class _FakeHy2Proxy:
    def __init__(self, error: Exception | None = None) -> None:
        self.error = error
        self.starts = 0

    def ensure_started(self) -> str:
        self.starts += 1
        if self.error:
            raise self.error
        return "http://127.0.0.1:19090"


class Hy2ProxyTests(unittest.TestCase):
    def test_config_uses_bundled_rust_source(self) -> None:
        config = AppConfig()

        self.assertEqual(
            config.hy2_source_dir,
            Path(__file__).resolve().parents[1] / "rust_module" / "hy2_proxy",
        )
        self.assertTrue((config.hy2_source_dir / "Cargo.toml").is_file())

    def test_build_runner_compiles_bundled_binary(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = AppConfig(data_dir=Path(directory))
            manager = Hy2ProxyManager(config)
            built_name = "hy2_serve.exe" if config.hy2_runner_path.suffix == ".exe" else "hy2_serve"
            built_runner = config.data_dir / "hy2" / "build" / "release" / built_name
            built_runner.parent.mkdir(parents=True)
            built_runner.write_bytes(b"runner")

            with (
                patch("fuckclassroom.course_selection.proxy.shutil.which", return_value="cargo"),
                patch(
                    "fuckclassroom.course_selection.proxy.subprocess.Popen",
                    return_value=_FakeBuildProcess(),
                ) as popen,
            ):
                manager.build_runner()

            command = popen.call_args.args[0]
            self.assertIn("--bin", command)
            self.assertIn("hy2_serve", command)
            self.assertNotIn("--example", command)
            self.assertEqual(Path(command[command.index("--manifest-path") + 1]), config.hy2_source_dir / "Cargo.toml")
            self.assertEqual(config.hy2_runner_path.read_bytes(), b"runner")

    def test_enabled_hy2_auto_builds_missing_runner_on_startup(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = _hy2_config(Path(directory))
            built_name = "hy2_serve.exe" if os.name == "nt" else "hy2_serve"
            built_runner = config.data_dir / "hy2" / "build" / "release" / built_name
            built_runner.parent.mkdir(parents=True)
            built_runner.write_bytes(b"auto-runner")

            with (
                patch(
                    "fuckclassroom.course_selection.hy2_prebuilt.install_prebuilt_runner",
                    return_value=False,
                ),
                patch("fuckclassroom.course_selection.proxy.shutil.which", return_value="cargo"),
                patch(
                    "fuckclassroom.course_selection.proxy.subprocess.Popen",
                    return_value=_FakeBuildProcess(),
                ) as popen,
            ):
                manager = Hy2ProxyManager(config)
                self.assertTrue(manager.wait_for_auto_build(2.0))

            self.assertTrue(popen.called)
            self.assertEqual(config.hy2_runner_path.read_bytes(), b"auto-runner")
            self.assertIn("已就绪", manager.get_status().message)

    def test_connect_bridge_tunnels_data(self) -> None:
        socks = _FakeSocksServer(("127.0.0.1", 0), _FakeSocksHandler)
        socks_thread = threading.Thread(target=socks.serve_forever, daemon=True)
        socks_thread.start()
        bridge = _ConnectProxyServer(
            _SocksEndpoint(port=socks.server_address[1], username="user", password="pass")
        )
        bridge_thread = threading.Thread(target=bridge.serve_forever, daemon=True)
        bridge_thread.start()
        try:
            with socket.create_connection(bridge.server_address, timeout=3) as client:
                client.sendall(b"CONNECT example.com:443 HTTP/1.1\r\nHost: example.com:443\r\n\r\n")
                response = _recv_until(client, b"\r\n\r\n")
                self.assertIn(b"200 Connection Established", response)
                client.sendall(b"hy2-bridge-ok")
                self.assertEqual(_recv_exact(client, 13), b"hy2-bridge-ok")
        finally:
            bridge.shutdown()
            bridge.server_close()
            socks.shutdown()
            socks.server_close()

    def test_authority_parser_supports_ipv4_and_ipv6(self) -> None:
        self.assertEqual(_parse_authority("example.com:443"), ("example.com", 443))
        self.assertEqual(_parse_authority("[::1]:8443"), ("::1", 8443))

    def test_hy2_access_uses_direct_academic_urls(self) -> None:
        access = _selection_access("hy2")
        self.assertEqual(access.mode, "hy2")
        self.assertEqual(access.label, "Hy2 代理")
        self.assertIn("bkjwtest.guet.edu.cn", access.home_url)
        self.assertTrue(access.login_url.endswith("/student/sso/login"))

    def test_public_access_uses_ldap_login_without_webvpn(self) -> None:
        access = _selection_access("public")
        self.assertEqual(access.mode, "public")
        self.assertEqual(access.label, "公网直连")
        self.assertTrue(access.login_url.endswith("/student/ldap/login"))
        self.assertIn("bkjwtest.guet.edu.cn", access.api_base_url)

    def test_public_playwright_bypasses_system_proxy(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            assistant = CourseSelectionAssistant(AppConfig(data_dir=Path(directory)))

            self.assertEqual(
                assistant._playwright_proxy_options(_selection_access("public")),
                {
                    "args": ["--proxy-server=direct://", "--proxy-bypass-list=*"],
                },
            )
            self.assertEqual(
                assistant._playwright_proxy_options(_selection_access("campus")),
                {},
            )

    def test_hy2_playwright_uses_managed_proxy(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            proxy = _FakeHy2Proxy()
            assistant = CourseSelectionAssistant(
                AppConfig(data_dir=Path(directory)), hy2_proxy=proxy
            )

            self.assertEqual(
                assistant._playwright_proxy_options(_selection_access("hy2")),
                {"proxy": {"server": "http://127.0.0.1:19090"}},
            )
            self.assertEqual(proxy.starts, 1)

    def test_detection_uses_hy2_after_direct_probe_fails(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = _hy2_config(Path(directory))
            assistant = CourseSelectionAssistant(config, hy2_proxy=_FakeHy2Proxy())
            with patch(
                "fuckclassroom.course_selection.browser.build_opener",
                side_effect=[_FailingOpener(), _ErrorStatusOpener(483), _SuccessfulOpener()],
            ):
                self.assertEqual(assistant._detect_access_mode(force=True), "hy2")

    def test_detection_prefers_hy2_before_public_ldap(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = _hy2_config(Path(directory))
            proxy = _FakeHy2Proxy()
            assistant = CourseSelectionAssistant(config, hy2_proxy=proxy)
            with patch(
                "fuckclassroom.course_selection.browser.build_opener",
                side_effect=[_FailingOpener(), _SuccessfulOpener()],
            ):
                self.assertEqual(assistant._detect_access_mode(force=True), "hy2")
            self.assertEqual(proxy.starts, 1)

    def test_detection_uses_public_ldap_when_hy2_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = _hy2_config(Path(directory))
            assistant = CourseSelectionAssistant(
                config,
                hy2_proxy=_FakeHy2Proxy(Hy2ProxyError("unavailable")),
            )
            with patch(
                "fuckclassroom.course_selection.browser.build_opener",
                side_effect=[_FailingOpener(), _SuccessfulOpener()],
            ):
                self.assertEqual(assistant._detect_access_mode(force=True), "public")

    def test_detection_falls_back_when_hy2_cannot_start(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = _hy2_config(Path(directory))
            assistant = CourseSelectionAssistant(
                config,
                hy2_proxy=_FakeHy2Proxy(Hy2ProxyError("unavailable")),
            )
            with patch(
                "fuckclassroom.course_selection.browser.build_opener",
                return_value=_FailingOpener(),
            ):
                self.assertEqual(assistant._detect_access_mode(force=True), "webvpn")

    def test_selection_http_client_installs_proxy_handler(self) -> None:
        with tempfile.TemporaryDirectory(dir=Path.cwd()) as directory:
            config = _hy2_config(Path(directory))
            assistant = CourseSelectionAssistant(config, hy2_proxy=_FakeHy2Proxy())
            assistant.session_dir.mkdir(parents=True)
            assistant.storage_state_path.write_text(
                json.dumps(
                    {
                        "cookies": [
                            {
                                "name": "session",
                                "value": "test",
                                "domain": "bkjwtest.guet.edu.cn",
                                "path": "/",
                                "secure": True,
                            }
                        ]
                    }
                ),
                encoding="utf-8",
            )
            assistant.access_mode_path.write_text(json.dumps({"mode": "hy2"}), encoding="utf-8")
            assistant._access_mode = "hy2"
            assistant._access_mode_checked_at = time.time()
            sentinel = object()
            with patch(
                "fuckclassroom.course_selection.api.build_opener",
                return_value=sentinel,
            ) as build:
                opener, _ = assistant._selection_http_client()
            self.assertIs(opener, sentinel)
            self.assertTrue(any(isinstance(handler, ProxyHandler) for handler in build.call_args.args))


def _hy2_config(data_dir: Path) -> AppConfig:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "settings.json").write_text(json.dumps({"hy2_enabled": True}), encoding="utf-8")
    return AppConfig(data_dir=data_dir)


def _recv_exact(stream: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = stream.recv(size - len(data))
        if not chunk:
            raise ConnectionError("socket closed")
        data.extend(chunk)
    return bytes(data)


def _recv_until(stream: socket.socket, marker: bytes) -> bytes:
    data = bytearray()
    while marker not in data:
        data.extend(stream.recv(4096))
    return bytes(data)


if __name__ == "__main__":
    unittest.main()
