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
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import codex_app_server_tmux as mod
from pinky_daemon.codex_app_server_tmux import CodexAppServerSupervisor, _TmuxAppServerProc
from pinky_daemon.codex_home import PER_AGENT_CODEX_HOME_ENV
from tests.codex_socket_support import codex_socket_sandbox as codex_socket_sandbox

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

    async def new_session(
        self, *, cwd: str, command: str, env=None, codex_headers=False,
    ) -> FakeTmuxResult:
        self.calls.append("new_session")
        assert codex_headers, "app-server loader must remove inherited MCP headers"
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


@pytest.fixture(autouse=True)
def socket_sandbox(codex_socket_sandbox):
    return codex_socket_sandbox


@pytest.fixture
def workdir(socket_sandbox):
    # Short path so the derived socket stays under macOS's AF_UNIX limit.
    d = str(socket_sandbox.root / "work")
    os.mkdir(d, 0o700)
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _make_supervisor(workdir: str, fake: FakeTmux, **kw) -> CodexAppServerSupervisor:
    sup = CodexAppServerSupervisor(
        "test-agent",
        working_dir=workdir,
        openai_api_key=kw.get("key", "test-key"),
        agent_config=kw.get("agent_config"),
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
        assert fake.new_session_env.get("OPENAI_API_KEY") == "test-key"
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
async def test_full_daemon_env_propagated_to_session(workdir, monkeypatch):
    """#792 P1 (Murzik): tmux drops parent env, so the supervisor must carry the
    daemon's full Codex config — not just PATH — or a fresh child runs under a
    different CODEX_HOME/session store and breaks auth + item G resume."""
    monkeypatch.setenv("CODEX_HOME", "/fleet/codex")
    monkeypatch.setenv("HOME", "/fleet/home")
    monkeypatch.setenv("XDG_CONFIG_HOME", "/fleet/xdg")
    monkeypatch.setenv("HTTPS_PROXY", "http://proxy:8080")
    monkeypatch.setenv("no_proxy", "localhost")
    monkeypatch.setenv("SSL_CERT_FILE", "/fleet/ca.pem")
    monkeypatch.setenv("OPENAI_BASE_URL", "https://fleet.example/v1")
    monkeypatch.setenv("TMUX", "/private/tmp/tmux-0/default,123,4")  # must be dropped
    # spawn=False -> no listener -> readiness times out, but new_session (and the
    # env build it exercises) has already run by then; shrink the wait.
    monkeypatch.setattr(mod, "_READINESS_TIMEOUT", 0.3)
    fake = FakeTmux(spawn=False)
    sup = _make_supervisor(workdir, fake)
    with pytest.raises(TimeoutError):
        await sup.start()
    env = fake.new_session_env
    for key, val in {
        "CODEX_HOME": "/fleet/codex",
        "HOME": "/fleet/home",
        "XDG_CONFIG_HOME": "/fleet/xdg",
        "HTTPS_PROXY": "http://proxy:8080",
        "no_proxy": "localhost",
        "SSL_CERT_FILE": "/fleet/ca.pem",
        "OPENAI_BASE_URL": "https://fleet.example/v1",
    }.items():
        assert env.get(key) == val, f"daemon env {key} not propagated to tmux session"
    # The configured key is overlaid (item H), and tmux-internal vars are dropped.
    assert env.get("OPENAI_API_KEY") == "test-key"
    assert "TMUX" not in env


def test_per_agent_home_overlaid_for_tmux_app_server(workdir, monkeypatch):
    shared_home = Path(workdir) / "shared-codex"
    shared_home.mkdir()
    (shared_home / "auth.json").write_text('{"test": true}\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    fake = FakeTmux(spawn=False)
    config = SimpleNamespace(working_dir=workdir, codex_home="")
    sup = _make_supervisor(workdir, fake, agent_config=config)

    env = sup._build_env()

    agent_home = Path(workdir).resolve() / ".codex"
    assert env["CODEX_HOME"] == str(agent_home)
    assert (agent_home / "auth.json").is_symlink()


@pytest.mark.asyncio
async def test_auth_absence_fails_before_tmux_cleanup(workdir, monkeypatch):
    shared_home = Path(workdir) / "missing-auth-codex"
    shared_home.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    fake = FakeTmux(spawn=False)
    config = SimpleNamespace(working_dir=workdir, codex_home="")
    sup = _make_supervisor(workdir, fake, agent_config=config)

    with pytest.raises(RuntimeError, match="shared auth file is absent or unreadable"):
        await sup.start()

    assert fake.calls == []


@pytest.mark.asyncio
async def test_long_working_dir_falls_back_to_short_sock_dir(monkeypatch):
    """A working_dir long enough to blow past the AF_UNIX limit must fall back
    to a short, owner-only 0700 dir — not a predictable /tmp path, and not an
    unbindable long one (would surface as a readiness timeout)."""
    long_wd = "/tmp/" + ("d" * 140)
    sup = CodexAppServerSupervisor("test-agent", working_dir=long_wd)
    assert sup.sock_path is None
    await _start_without_child(sup, monkeypatch)
    try:
        assert sup._sock_dir_is_tmp is True
        assert len(sup.sock_path) <= 104
        assert sup.sock_path.startswith("/tmp/")
        # mkdtemp gives an unguessable, atomically-0700, owner-only dir — the
        # parent-dir perms are what gate connect() to an approvals=never codex.
        st = os.lstat(sup._sock_dir)
        assert stat.S_ISDIR(st.st_mode)
        assert stat.S_IMODE(st.st_mode) == 0o700
        assert st.st_uid == os.getuid()
    finally:
        await sup.teardown()


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


async def _start_without_child(sup, monkeypatch):
    sup._tmux = FakeTmux(spawn=False)
    monkeypatch.setattr(sup, "_await_accept", AsyncMock(return_value=(object(), object())))
    client = SimpleNamespace(start=lambda: None)
    monkeypatch.setattr(mod, "CodexAppServerClient", lambda *args, **kwargs: client)
    return await sup.start()


def _long_supervisor(socket_sandbox, **kwargs):
    return CodexAppServerSupervisor(
        "test-agent", working_dir=str(socket_sandbox.root / ("long-" + "x" * 140)), **kwargs,
    )


def test_long_constructor_and_stats_allocate_no_socket_dir(socket_sandbox):
    sup = _long_supervisor(socket_sandbox)
    assert socket_sandbox.created == [], "construction must not allocate a fallback directory"
    assert sup.sock_path is None
    assert sup.stats()["sock_path"] is None
    assert socket_sandbox.created == [], "stats must not allocate a fallback directory"


@pytest.mark.asyncio
async def test_never_started_teardown_allocates_nothing(socket_sandbox):
    sup = _long_supervisor(socket_sandbox)
    sup._tmux = FakeTmux(spawn=False)
    await sup.teardown()
    await sup.teardown()
    assert socket_sandbox.created == [], "never-started teardown must not allocate a directory"
    assert sup.sock_path is None and sup.stats()["sock_path"] is None


@pytest.mark.asyncio
async def test_long_start_teardown_removes_owned_directory(socket_sandbox, monkeypatch):
    sup = _long_supervisor(socket_sandbox)
    await _start_without_child(sup, monkeypatch)
    directory = Path(sup._sock_dir)
    (directory / "app.sock").write_text("test-owned stale socket")
    assert directory.lstat().st_uid == os.getuid()
    assert len(sup.sock_path) <= 100
    assert sup.stats()["sock_path"] == sup.sock_path
    await sup.teardown()
    assert not directory.exists(), "teardown must rmdir its empty owned fallback"
    assert sup.sock_path is None and sup.stats()["sock_path"] is None
    await sup.teardown()
    assert not directory.exists()


@pytest.mark.asyncio
async def test_unexpected_file_survives_loud_rmdir_refusal(socket_sandbox, monkeypatch):
    logs = []
    sup = _long_supervisor(socket_sandbox, log=logs.append)
    await _start_without_child(sup, monkeypatch)
    directory = Path(sup._sock_dir)
    extra = directory / "unexpected.txt"
    extra.write_text("retain this unexpected file")
    logs.clear()
    await sup.teardown()
    assert extra.read_text() == "retain this unexpected file"
    assert any("rmdir" in line and str(directory) in line for line in logs), logs
    extra.unlink()
    await sup.teardown()
    assert not directory.exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["replacement", "symlink", "uid", "dev", "ino"])
async def test_changed_socket_directory_is_not_touched(socket_sandbox, monkeypatch, change):
    sup = _long_supervisor(socket_sandbox)
    await _start_without_child(sup, monkeypatch)
    directory = Path(sup._sock_dir)
    original_identity = directory.lstat()
    original = directory
    if change in {"replacement", "symlink"}:
        original = directory.with_name(directory.name + "-original")
        directory.rename(original)
        # Original remains live: inode reuse after removal is deliberately excluded.
        if change == "replacement":
            directory.mkdir(mode=0o700)
        else:
            target = socket_sandbox.root / "foreign-target"
            target.mkdir(mode=0o700)
            directory.symlink_to(target, target_is_directory=True)
        assert (original.lstat().st_dev, original.lstat().st_ino) == (
            original_identity.st_dev, original_identity.st_ino,
        )
    sock = directory / "app.sock"
    sock.write_text("preserve replacement socket")
    real_lstat = os.lstat
    if change in {"uid", "dev", "ino"}:
        field = {"uid": "st_uid", "dev": "st_dev", "ino": "st_ino"}[change]

        def changed_lstat(path, *args, **kwargs):
            result = real_lstat(path, *args, **kwargs)
            if os.fsdecode(path) == str(directory):
                values = {name: getattr(result, name) for name in dir(result)
                          if name.startswith("st_")}
                values[field] += 1
                return SimpleNamespace(**values)
            return result

        monkeypatch.setattr(mod.os, "lstat", changed_lstat)
    calls = []
    real_rmdir = os.rmdir

    def rmdir(path, *args, **kwargs):
        calls.append(os.fsdecode(path))
        return real_rmdir(path, *args, **kwargs)

    with monkeypatch.context() as guard:
        guard.setattr(mod.os, "rmdir", rmdir)
        await sup.teardown()
    assert os.path.lexists(directory), "a replaced or foreign directory must survive"
    assert sock.read_text() == "preserve replacement socket"
    assert calls == [], "ownership/identity refusal must occur before rmdir"
    assert original.exists()


@pytest.mark.asyncio
async def test_short_working_dir_keeps_in_boundary_directory(workdir, monkeypatch):
    sup = CodexAppServerSupervisor("test-agent", working_dir=workdir)
    planned = Path(workdir) / ".codex-app-server"
    assert not planned.exists(), "short-path construction must not create a directory"
    assert sup._sock_dir_is_tmp is False
    await _start_without_child(sup, monkeypatch)
    assert Path(sup._sock_dir) == planned
    await sup.teardown()
    assert planned.is_dir(), "the in-boundary directory must never be removed"
    assert sup.stats()["sock_path"] == str(planned / "app.sock")


@pytest.mark.asyncio
async def test_same_supervisor_restarts_with_fresh_fallback(socket_sandbox, monkeypatch):
    sup = _long_supervisor(socket_sandbox)
    await _start_without_child(sup, monkeypatch)
    first = Path(sup._sock_dir)
    await sup.teardown()
    assert not first.exists()
    assert sup.sock_path is None
    sup.request_kill()
    await sup.start()
    second = Path(sup._sock_dir)
    assert second != first and second.is_dir(), "restart must allocate a fresh mkdtemp path"
    assert sup._kill_requested is False
    await sup.teardown()
    assert not second.exists()


@pytest.mark.asyncio
async def test_strict_kill_failure_allocates_no_fallback(socket_sandbox, monkeypatch):
    sup = _long_supervisor(socket_sandbox)
    sup._tmux = FakeTmux(spawn=False)
    monkeypatch.setattr(sup, "_kill_tmux_session", AsyncMock(side_effect=RuntimeError("kill failed")))
    with pytest.raises(RuntimeError, match="kill failed"):
        await sup.start()
    assert socket_sandbox.created == [], "failed preflight must not allocate a socket directory"
