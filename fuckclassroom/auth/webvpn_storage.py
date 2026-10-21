"""Cross-process, generation-fenced Playwright storage shared by GUET plugins.

Only trusted Python plugin services may read or write this module's raw state.
The public browser service never serializes state or generation to an HTTP/RPC
response. All modifications are serialized with an OS file lock.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Literal
from urllib.parse import urlsplit

from ..core.atomic import atomic_write_text
from ..core.plugins import PluginServiceError


class WebVpnSessionError(PluginServiceError):
    """The saved session was invalidated or the browser handoff is unsafe."""


_locks_guard = threading.Lock()
_locks: dict[str, threading.RLock] = {}
_context_guard = threading.Lock()
_context_snapshots: weakref.WeakKeyDictionary = weakref.WeakKeyDictionary()


def _lock(path: Path) -> threading.RLock:
    key = str(path.resolve())
    with _locks_guard:
        return _locks.setdefault(key, threading.RLock())


@contextmanager
def _locked_storage_file(path: Path, *, timeout_seconds: float | None = None):
    """Bounded cross-process file locking; no mutation on lock timeout."""
    deadline = None if timeout_seconds is None else time.monotonic() + max(timeout_seconds, 0.0)
    lock = _lock(path)
    if deadline is None:
        lock.acquire()
    elif not lock.acquire(timeout=max(0.0, deadline - time.monotonic())):
        raise TimeoutError("等待共享会话文件锁超时")
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.with_name(path.name + ".lock").open("a+b") as handle:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                if not handle.read(1):
                    handle.seek(0)
                    handle.write(b"0")
                    handle.flush()
                handle.seek(0)
            else:
                import fcntl

            while True:
                try:
                    if os.name == "nt":
                        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                    else:
                        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except (OSError, BlockingIOError):
                    if deadline is not None and time.monotonic() >= deadline:
                        raise TimeoutError("等待共享会话文件锁超时") from None
                    time.sleep(0.04)
            try:
                # Even if the file lock was just acquired, an operation already
                # past its deadline must not commit a late logout.
                if deadline is not None and time.monotonic() >= deadline:
                    raise TimeoutError("共享会话操作超时，尚未执行")
                yield
            finally:
                if os.name == "nt":
                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
    finally:
        lock.release()


def _generation_path(path: Path) -> Path:
    return path.with_name("session_generation.json")


def _read_generation_unlocked(path: Path) -> tuple[str, bool]:
    try:
        payload = json.loads(_generation_path(path).read_text(encoding="utf-8"))
        generation = payload.get("generation")
        if isinstance(generation, str) and generation:
            return generation, bool(payload.get("logged_out"))
    except (OSError, ValueError, AttributeError):
        pass
    # Backwards-compatible with existing storage_state.json installations.
    return "legacy", False


def session_generation(path: Path) -> tuple[str, bool]:
    """Read the durable generation without writing or touching browser cookies."""
    with _lock(path):
        return _read_generation_unlocked(path)


def _rotate_generation_unlocked(path: Path, *, logged_out: bool) -> str:
    token = uuid.uuid4().hex
    atomic_write_text(
        _generation_path(path),
        json.dumps({"generation": token, "logged_out": logged_out}),
    )
    return token


# Shared SSO authentication scope; other plugins own their own origin cookies.
# The parent-domain cookie is also shared by campus SSO and must not survive
# an explicit relogin. Do not use guet.edu.cn as a wildcard for relogin.
_DEFAULT_LOGIN_DOMAINS = ("v.guet.edu.cn", "cas.guet.edu.cn", "bkjwtest.guet.edu.cn")
SessionStartMode = Literal["renewal", "relogin", "account_switch"]


def _auth_scope(domain: str, *, mode: SessionStartMode, domains: tuple[str, ...]) -> bool:
    host = domain.lower().lstrip(".").rstrip(".")
    if mode == "account_switch":
        return host == "guet.edu.cn" or host.endswith(".guet.edu.cn")
    return host == "guet.edu.cn" or any(
        host == owned or host.endswith("." + owned) for owned in domains
    )


def _purge_login_scope(state: dict[str, Any], *, mode: SessionStartMode,
                       domains: tuple[str, ...]) -> dict[str, Any]:
    """Drop owned login state, preserving unrelated plugin cookies/origins."""
    cookies = [
        item for item in state.get("cookies", [])
        if not (isinstance(item, dict) and
                _auth_scope(str(item.get("domain") or ""), mode=mode, domains=domains))
    ]
    origins = [
        item for item in state.get("origins", [])
        if not (isinstance(item, dict) and
                _auth_scope(urlsplit(str(item.get("origin") or "")).hostname or "",
                            mode=mode, domains=domains))
    ]
    return {"cookies": cookies, "origins": origins}


def begin_session(
    path: Path, *, mode: SessionStartMode = "relogin",
    cookie_domains: tuple[str, ...] = (),
) -> str:
    """Enter a session using an explicit cookie ownership policy.

    renewal: same account, no generation change or cookie removal; never
        overrides a prior explicit logout.
    relogin (default): rotate generation and remove gateway/CAS/undergrad
        authentication state plus caller-owned domains.
    account_switch: rotate generation and drop all campus .guet.edu.cn
        state, preserving truly unrelated external plugin origins.

    Both destructive policies run inside the same cross-process file lock as
    generation fencing. Existing BrowserContexts and HTTP snapshots are stale.
    """
    if mode not in ("renewal", "relogin", "account_switch"):
        raise ValueError(f"不支持的会话开始模式：{mode}")
    domains = (*_DEFAULT_LOGIN_DOMAINS, *(str(d).lower().lstrip(".") for d in cookie_domains))
    if any(not d or d == "guet.edu.cn" or not d.endswith(".guet.edu.cn")
           or any(c not in "abcdefghijklmnopqrstuvwxyz0123456789-." for c in d)
           for d in domains):
        raise WebVpnSessionError("登录 Cookie 所属域必须是明确的桂电子域名")
    with _locked_storage_file(path):
        current, logged_out = _read_generation_unlocked(path)
        if mode == "renewal":
            if logged_out:
                raise WebVpnSessionError("WebVPN 已主动退出，必须由用户明确重新登录")
            return current
        state = _purge_login_scope(_read_unlocked(path), mode=mode, domains=domains)
        # Rotate generation BEFORE storing the filtered state. The storage
        # lock prevents stale writes between rotation and removal.
        generation = _rotate_generation_unlocked(path, logged_out=False)
        if state["cookies"] or state["origins"]:
            atomic_write_text(path, json.dumps(state, ensure_ascii=False))
        else:
            path.unlink(missing_ok=True)
        return generation


def invalidate_session(
    path: Path, *, timeout_seconds: float | None = None,
    account_switch: bool = False,
) -> None:
    """Log out with generation fencing; account switch keeps external plugins.

    Full logout removes the entire shared state. A credential/account switch
    revokes every campus-domain Cookie/Origin while preserving unrelated
    external plugin state. Both remain logged_out until explicit authorization.
    """
    with _locked_storage_file(path, timeout_seconds=timeout_seconds):
        retained = (
            _purge_login_scope(_read_unlocked(path), mode="account_switch",
                               domains=_DEFAULT_LOGIN_DOMAINS)
            if account_switch else {"cookies": [], "origins": []}
        )
        _rotate_generation_unlocked(path, logged_out=True)
        if retained["cookies"] or retained["origins"]:
            atomic_write_text(path, json.dumps(retained, ensure_ascii=False))
        else:
            path.unlink(missing_ok=True)


def _read_unlocked(path: Path) -> dict[str, Any]:
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(state, dict) and isinstance(state.get("cookies"), list):
            return state
    except (OSError, ValueError, TypeError):
        pass
    return {"cookies": [], "origins": []}


def read_storage_state(path: Path) -> dict[str, Any]:
    """Side-effect-free; os.replace makes unguarded cross-process reads atomic."""
    with _lock(path):
        return _read_unlocked(path)


def read_session_snapshot(path: Path) -> tuple[dict[str, Any], str]:
    """Capture both state and generation under the same cross-process lock."""
    with _locked_storage_file(path):
        generation, logged_out = _read_generation_unlocked(path)
        if logged_out:
            raise WebVpnSessionError("WebVPN 会话已主动退出，请先重新连接")
        return _read_unlocked(path), generation


def _cookie_map(state: dict[str, Any]) -> dict[tuple[str, str, str], dict[str, Any]]:
    return {
        (c["name"], c["domain"].lower(), c.get("path", "/")): c
        for c in state.get("cookies", [])
        if isinstance(c, dict) and isinstance(c.get("name"), str)
        and isinstance(c.get("domain"), str)
    }


def _expired(cookie: dict[str, Any], now: float) -> bool:
    expires = cookie.get("expires", -1)
    return isinstance(expires, (float, int)) and expires > 0 and expires <= now


def _origins_map(state: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        item["origin"]: item
        for item in state.get("origins", [])
        if isinstance(item, dict) and isinstance(item.get("origin"), str)
    }


def _local_storage(item: dict[str, Any] | None) -> dict[str, str]:
    result: dict[str, str] = {}
    for entry in (item or {}).get("localStorage", []):
        if isinstance(entry, dict) and isinstance(entry.get("name"), str):
            result[entry["name"]] = str(entry.get("value", ""))
    return result


def save_storage_state(
    path: Path, state: dict[str, Any], *,
    expected_generation: str | None = None,
    previous_state: dict[str, Any] | None = None,
) -> None:
    """Compare-and-swap generation plus a three-way delta (browser/base/disk).

    If previous_state is omitted, the incoming snapshot is a patch; absent
    cookies are not deletions. Browser adapters should always pass the snapshot
    captured when the context was created to propagate removals safely.
    """
    if not isinstance(state, dict) or not isinstance(state.get("cookies"), list):
        raise WebVpnSessionError("无效的浏览器会话结构")
    with _locked_storage_file(path):
        current_generation, logged_out = _read_generation_unlocked(path)
        if logged_out or (
            expected_generation is None and current_generation != "legacy"
        ) or (
            expected_generation is not None and expected_generation != current_generation
        ):
            raise WebVpnSessionError("会话已变化，请重新加载登录态后操作")
        disk = _read_unlocked(path)
        current = _cookie_map(disk)
        original = _cookie_map(previous_state or {})
        incoming = _cookie_map(state)
        now = time.time()

        if previous_state is not None:
            for key, old in original.items():
                if key not in incoming and current.get(key) == old:
                    current.pop(key, None)
        for key, new in incoming.items():
            if previous_state is None or original.get(key) != new:
                if _expired(new, now):
                    current.pop(key, None)
                else:
                    current[key] = new
        current = {key: item for key, item in current.items() if not _expired(item, now)}

        existing_origins = _origins_map(disk)
        previous_origins = _origins_map(previous_state or {})
        new_origins = _origins_map(state)
        for origin in set(previous_origins) | set(new_origins):
            if previous_state is not None and origin in previous_origins:
                original_local = _local_storage(previous_origins[origin])
                updated_local = _local_storage(new_origins.get(origin))
                disk_local = _local_storage(existing_origins.get(origin))
                for key in original_local.keys() - updated_local.keys():
                    if disk_local.get(key) == original_local[key]:
                        disk_local.pop(key, None)
                for key, value in updated_local.items():
                    if original_local.get(key) != value:
                        disk_local[key] = value
                if disk_local:
                    existing_origins[origin] = {
                        "origin": origin,
                        "localStorage": [{"name": key, "value": value} for key, value in disk_local.items()],
                    }
                else:
                    existing_origins.pop(origin, None)
            elif origin in new_origins:
                # No base: update only keys provided; never delete another plugin's.
                disk_local = _local_storage(existing_origins.get(origin))
                disk_local.update(_local_storage(new_origins[origin]))
                existing_origins[origin] = {
                    "origin": origin,
                    "localStorage": [{"name": key, "value": value} for key, value in disk_local.items()],
                }

        merged = {
            "cookies": list(current.values()),
            "origins": list(existing_origins.values()),
        }
        atomic_write_text(path, json.dumps(merged, ensure_ascii=False))


def new_browser_context(browser: Any, path: Path, *, reuse_saved_session: bool = True) -> Any:
    """Create a context and bind its generation + immutable initial snapshot."""
    state, generation = read_session_snapshot(path)
    context = (
        browser.new_context(storage_state=state)
        if reuse_saved_session and (state.get("cookies") or state.get("origins"))
        else browser.new_context()
    )
    # A fresh login intentionally starts with an empty browser. Track only
    # the former SSO-owned state as the deletion baseline; unrelated plugin
    # state must survive the subsequent three-way merge.
    before = state if reuse_saved_session else {
        "cookies": [c for c in state.get("cookies", [])
                    if isinstance(c, dict) and _auth_scope(
                        str(c.get("domain") or ""), mode="relogin",
                        domains=_DEFAULT_LOGIN_DOMAINS)],
        "origins": [o for o in state.get("origins", [])
                    if isinstance(o, dict) and _auth_scope(
                        urlsplit(str(o.get("origin") or "")).hostname or "",
                        mode="relogin", domains=_DEFAULT_LOGIN_DOMAINS)],
    }
    with _context_guard:
        _context_snapshots[context] = (path.resolve(), generation, before)
    return context


def save_browser_context(path: Path, context: Any) -> None:
    """Reject any browser context created before logout/account switching."""
    inner = getattr(context, "_browser_context", context)
    with _context_guard:
        snapshot = _context_snapshots.get(inner)
    if snapshot is None or snapshot[0] != path.resolve():
        raise WebVpnSessionError("浏览器 Context 未通过主仓会话适配器创建")
    _, generation, before = snapshot
    new_state = inner.storage_state()
    save_storage_state(
        path, new_state,
        expected_generation=generation,
        previous_state=before,
    )
    # A second save must propagate changes since the first save, not replay them.
    with _context_guard:
        _context_snapshots[inner] = (path.resolve(), generation, new_state)


__all__ = [
    "WebVpnSessionError", "SessionStartMode", "begin_session", "invalidate_session",
    "session_generation", "read_storage_state", "read_session_snapshot",
    "save_storage_state", "new_browser_context", "save_browser_context",
]
