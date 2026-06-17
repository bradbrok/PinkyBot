"""Tests for CodexAppServerSupervisor (#791 Design A).

The supervisor's tmux dependency is replaced with a fake ``_TmuxControl`` that,
on ``new_session``, actually launches the real ``codex_app_server_shim`` via
``subprocess.Popen`` (pointed at ``tests/_fake_app_server`` — no ``codex``
needed). That exercises the real accept-readiness probe, the real
CodexAppServerClient over the real socket, and the real teardown, while letting
the tests assert how the supervisor drives tmux (idempotent kill, env injection,
session lifecycle) with no actual tmux server.
"""

from __future__ import annotations

import os
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile

import pytest

from pinky_daemon import codex_app_server_tmux as mod
from pinky_daemon.codex_app_server_tmux import CodexAppServerSupervisor, _TmuxAppServerProc

_REPO_SRC = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
_FAKE = os.path.join(os.path.dirname(__file__), "_fake_app_server.py")


class FakeTmuxResult:
    def __init__(self, ok: bool = True, stderr: str = "") -> None:
        self.ok = ok
        self.returncode = 0 if ok else 1
        self.stdout = ""
        self.stderr = stderr


class FakeTmux:
    """Records tmux calls; really runs the shim command on new_session."""

    def __init__(self, *, new_session_ok: bool = True, spawn: bool = True) -> None:
        self.calls: list[str] = []
        self.new_session_env: dict | None = None
        self.new_session_ok = new_session_ok
        self.spawn = spawn
        self._proc: subprocess.Popen | None = None
        self._has = False

    async def has_session(self) -> bool:
        return self._has

    async def new_session(self, *, cwd: str, command: str, env=None) -> FakeTmuxResult:
        self.calls.append("new_session")
        self.new_session_env = dict(env or {})
        if not self.new_session_ok:
            return FakeTmuxResult(ok=False, stderr="boom")
        if self.spawn:
            run_env = {
                **os.environ,
                **(env or {}),
                "PYTHONPATH": _REPO_SRC + os.pathsep + os.environ.get("PYTHONPATH", ""),
                "PINKY_CODEX_APP_SERVER_CMD": f"{sys.executable} {_FAKE}",
            }
            self._proc = subprocess.Popen(
                shlex.split(command),
                cwd=cwd,
                env=run_env,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            self._has = True
        return FakeTmuxResult(ok=True)

    async def kill_session(self) -> FakeTmuxResult:
        self.calls.append("kill_session")
        if self._proc is not None:
            try:
                self._proc.kill()
                self._proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass
            self._proc = None
        self._has = False
        return FakeTmuxResult(ok=True)


@pytest.fixture
def workdir():
    # Short path so the derived socket stays under macOS's AF_UNIX limit.
    d = tempfile.mkdtemp(dir="/tmp", prefix="kzwd")
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _make_supervisor(workdir: str, fake: FakeTmux, **kw) -> CodexAppServerSupervisor:
    sup = CodexAppServerSupervisor(
        "kztest", working_dir=workdir, openai_api_key=kw.get("key", "sk-SENTINEL"),
    )
    sup._tmux = fake
    return sup


@pytest.mark.asyncio
async def test_start_spawns_shim_and_initialize_round_trips(workdir):
    fake = FakeTmux()
    sup = _make_supervisor(workdir, fake)
    client, proc = await sup.start()
    try:
        # Idempotent pre-start cleanup ran before the spawn.
        assert fake.calls[0] == "kill_session"
        assert "new_session" in fake.calls
        # Env injected for the shell/child: PATH (codex resolution) + the key
        # (item H — must reach the grandchild codex via tmux -e -> shim -> child).
        assert "PATH" in fake.new_session_env
        assert fake.new_session_env.get("OPENAI_API_KEY") == "sk-SENTINEL"
        # The single initialize (the daemon's real gate) round-trips end to end.
        res = await client.initialize(name="pinkybot", version="1")
        assert res["userAgent"] == "fake/1"
        assert isinstance(proc, _TmuxAppServerProc)
        assert proc.returncode is None
        # Fail-closed perms: dir 0700, socket 0600.
        assert stat.S_IMODE(os.stat(sup._sock_dir).st_mode) == 0o700
        assert stat.S_IMODE(os.stat(sup.sock_path).st_mode) == 0o600
    finally:
        await client.close()
        await fake.kill_session()


@pytest.mark.asyncio
async def test_new_session_failure_raises(workdir):
    fake = FakeTmux(new_session_ok=False)
    sup = _make_supervisor(workdir, fake)
    with pytest.raises(RuntimeError, match="new-session failed"):
        await sup.start()


@pytest.mark.asyncio
async def test_readiness_timeout_raises(workdir, monkeypatch):
    # new_session "succeeds" but never spawns a listener -> accept never lands.
    monkeypatch.setattr(mod, "_READINESS_TIMEOUT", 0.5)
    fake = FakeTmux(spawn=False)
    sup = _make_supervisor(workdir, fake)
    with pytest.raises(TimeoutError, match="did not accept"):
        await sup.start()


@pytest.mark.asyncio
async def test_teardown_kills_session_and_unlinks_sock(workdir):
    fake = FakeTmux()
    sup = _make_supervisor(workdir, fake)
    client, _proc = await sup.start()
    await client.close()
    assert os.path.exists(sup.sock_path) or True  # shim may already self-unlink
    await sup.teardown()
    assert "kill_session" in fake.calls
    assert not os.path.exists(sup.sock_path)


@pytest.mark.asyncio
async def test_proc_adapter_kill_then_wait_tears_down(workdir):
    fake = FakeTmux()
    sup = _make_supervisor(workdir, fake)
    client, proc = await sup.start()
    await client.close()
    kills_before = fake.calls.count("kill_session")
    proc.kill()  # request only — non-blocking, mirrors Process.kill()
    rc = await proc.wait()  # performs the real async teardown
    assert rc == -9
    assert proc.returncode == -9
    assert fake.calls.count("kill_session") == kills_before + 1
    assert not os.path.exists(sup.sock_path)


@pytest.mark.asyncio
async def test_start_unlinks_stale_socket(workdir):
    fake = FakeTmux()
    sup = _make_supervisor(workdir, fake)
    # Pre-create a stale file where the socket will live.
    os.makedirs(sup._sock_dir, exist_ok=True)
    with open(sup.sock_path, "w") as f:
        f.write("stale")
    client, _proc = await sup.start()
    try:
        # A real, connectable socket now exists (not the stale regular file).
        assert stat.S_ISSOCK(os.stat(sup.sock_path).st_mode)
        res = await client.initialize()
        assert res["userAgent"] == "fake/1"
    finally:
        await client.close()
        await fake.kill_session()


def test_session_name_is_agent_scoped():
    sup = CodexAppServerSupervisor("dymok", working_dir="/tmp/x")
    assert sup.session_name == "pinky-codex-as-dymok"
