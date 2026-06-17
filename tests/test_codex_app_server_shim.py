"""End-to-end tests for the Design-A UDS<->stdio bridge (#791).

Spawns the real ``codex_app_server_shim`` as a subprocess, but points its child
at ``tests/_fake_app_server`` via PINKY_CODEX_APP_SERVER_CMD so no ``codex``
binary is needed. Exercises the byte-transparent bridge, single-client policy,
and terminal teardown (child reaped + socket unlinked + shim exits).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile

import pytest

_REPO_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
_FAKE = os.path.join(os.path.dirname(__file__), "_fake_app_server.py")
_STREAM_LIMIT = 10 * 1024 * 1024


@pytest.fixture
def sock_path():
    """A short UDS path. pytest's tmp_path blows past macOS's ~104-char
    AF_UNIX limit, so anchor the socket under a short /tmp dir instead."""
    d = tempfile.mkdtemp(dir="/tmp", prefix="kzas")
    try:
        yield os.path.join(d, "app.sock")
    finally:
        shutil.rmtree(d, ignore_errors=True)


class _Shim:
    """A running shim subprocess. Spawned via plain ``subprocess.Popen`` — NOT
    ``asyncio.create_subprocess_exec`` — because pytest-asyncio's per-test event
    loop breaks the asyncio child watcher (the shim's own socket I/O still runs
    on asyncio fine; only the process management sidesteps it). Its stdout/stderr
    go to a real file, not a pipe, so pytest's stdio capture can't interfere."""

    def __init__(self, proc: subprocess.Popen, child_pid: int) -> None:
        self.proc = proc
        self.child_pid = child_pid

    async def wait(self, timeout: float = 10.0) -> int:
        return await asyncio.wait_for(asyncio.to_thread(self.proc.wait), timeout)

    def kill(self) -> None:
        try:
            self.proc.kill()
            self.proc.wait(timeout=5)
        except Exception:  # noqa: BLE001
            pass


async def _spawn_shim(sock_path: str, *, exit_after_init: bool = False) -> _Shim:
    env = {
        **os.environ,
        "PYTHONPATH": _REPO_SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
        "PINKY_CODEX_APP_SERVER_CMD": f"{sys.executable} {_FAKE}",
    }
    if exit_after_init:
        env["FAKE_AS_EXIT_AFTER_INIT"] = "1"
    log_path = sock_path + ".log"
    log = open(log_path, "w+")
    proc = subprocess.Popen(
        [sys.executable, "-m", "pinky_daemon.codex_app_server_shim", sock_path],
        stdout=log,
        stderr=subprocess.STDOUT,
        env=env,
    )

    # Poll the log file for the "...child_pid=<pid>" line (off the event loop).
    def _read_pid() -> int:
        deadline = 150  # ~15s at 0.1s
        with open(log_path) as fh:
            for _ in range(deadline):
                for line in fh:
                    if "child_pid=" in line:
                        return int(line.split("child_pid=")[1])
                if proc.poll() is not None:
                    fh.seek(0)
                    raise RuntimeError(f"shim exited rc={proc.returncode}; log:\n{fh.read()}")
                import time

                time.sleep(0.1)
        raise RuntimeError("shim never reported child_pid")

    child_pid = await asyncio.wait_for(asyncio.to_thread(_read_pid), timeout=20)
    return _Shim(proc, child_pid)


async def _request(reader, writer, mid, method, params=None):
    writer.write((json.dumps({"id": mid, "method": method, "params": params or {}}) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), timeout=10)
    return json.loads(line)


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


@pytest.mark.asyncio
async def test_initialize_round_trips_through_bridge(sock_path):
    sock = sock_path
    shim = await _spawn_shim(sock)
    try:
        reader, writer = await asyncio.open_unix_connection(sock, limit=_STREAM_LIMIT)
        resp = await _request(reader, writer, 1, "initialize", {"clientInfo": {"name": "t"}})
        assert resp["id"] == 1
        assert resp["result"]["userAgent"] == "fake/1"
        writer.close()
    finally:
        shim.kill()


@pytest.mark.asyncio
async def test_large_frame_is_byte_transparent(sock_path):
    """A >64 KiB frame must round-trip — the bridge reads fixed chunks and never
    parses lines, so it can't trip the client reader's frame limit."""
    sock = sock_path
    shim = await _spawn_shim(sock)
    try:
        reader, writer = await asyncio.open_unix_connection(sock, limit=_STREAM_LIMIT)
        big = "x" * (256 * 1024)
        resp = await _request(reader, writer, 7, "thread/start", {"blob": big})
        assert resp["id"] == 7
        assert resp["result"]["echo"]["blob"] == big
        writer.close()
    finally:
        shim.kill()


@pytest.mark.asyncio
async def test_second_connection_is_rejected(sock_path):
    sock = sock_path
    shim = await _spawn_shim(sock)
    try:
        r1, w1 = await asyncio.open_unix_connection(sock, limit=_STREAM_LIMIT)
        await _request(r1, w1, 1, "initialize")
        # Second connection: shim closes it immediately (EOF), child stays alive.
        r2, w2 = await asyncio.open_unix_connection(sock, limit=_STREAM_LIMIT)
        assert await asyncio.wait_for(r2.read(1), timeout=5) == b""
        w2.close()
        await asyncio.sleep(0.2)
        assert _pid_alive(shim.child_pid)
        # Primary connection still works.
        resp = await _request(r1, w1, 2, "ping")
        assert resp["id"] == 2
        w1.close()
    finally:
        shim.kill()


@pytest.mark.asyncio
async def test_client_disconnect_kills_child_and_unlinks_sock(sock_path):
    sock = sock_path
    shim = await _spawn_shim(sock)
    reader, writer = await asyncio.open_unix_connection(sock, limit=_STREAM_LIMIT)
    await _request(reader, writer, 1, "initialize")
    assert _pid_alive(shim.child_pid)
    writer.close()
    await writer.wait_closed()
    # Shim should reap the child, unlink the socket, and exit.
    await shim.wait(timeout=10)
    for _ in range(50):
        if not _pid_alive(shim.child_pid):
            break
        await asyncio.sleep(0.1)
    assert not _pid_alive(shim.child_pid)
    assert not os.path.exists(sock)


@pytest.mark.asyncio
async def test_child_death_tears_down_the_bridge(sock_path):
    """If the child exits (item F at the bridge layer), the shim drops the
    client connection and exits rather than wedging."""
    sock = sock_path
    shim = await _spawn_shim(sock, exit_after_init=True)
    reader, writer = await asyncio.open_unix_connection(sock, limit=_STREAM_LIMIT)
    # initialize triggers the fake child to exit right after replying.
    resp = await _request(reader, writer, 1, "initialize")
    assert resp["result"]["userAgent"] == "fake/1"
    # Child EOF -> shim closes our connection and exits.
    assert await asyncio.wait_for(reader.read(), timeout=10) == b""
    await shim.wait(timeout=10)
    assert not os.path.exists(sock)


@pytest.mark.asyncio
async def test_stale_socket_is_unlinked_before_bind(sock_path):
    sock = sock_path
    # Pre-create a stale regular file at the socket path (its dir already exists).
    with open(sock, "w") as f:
        f.write("stale")
    shim = await _spawn_shim(sock)
    try:
        reader, writer = await asyncio.open_unix_connection(sock, limit=_STREAM_LIMIT)
        resp = await _request(reader, writer, 1, "initialize")
        assert resp["id"] == 1
        writer.close()
    finally:
        shim.kill()
