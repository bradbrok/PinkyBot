"""Rollback cleanup and target-local publication leases cover late startup failures."""

import asyncio
import fcntl
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pinky_daemon import tmux_launch_env, tmux_session
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.command_runner import RunuserCommandRunner
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxCommandResult, TmuxSession, _TmuxControl
from tests.test_tmux_launch_env_json_control import FailingRunner
from tests.tmux_env_r3_support import NONCE, OTHER_NONCE, SCOPE, SECRET, cancel, stage
from tests.tmux_env_support import LaunchRecorder, secret_files

TMUX_BINARY = shutil.which("tmux")


@pytest.fixture
def home(tmp_path, monkeypatch):
    path = tmp_path / "home"
    path.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(path))
    return path


def session_for(home, control, monkeypatch, kind="claude"):
    cls = TmuxSession if kind == "claude" else CodexTmuxSession
    config = StreamingSessionConfig(agent_name="rollback-test", working_dir=str(home))
    session = cls(config, tmux_control=control)
    for name in ("_ensure_container_started", "_reap_retained_spawn_cleanup_debt",
                 "_seed_container_trust", "_seed_container_home_creds", "_stop_tailer"):
        monkeypatch.setattr(session, name, AsyncMock())
    monkeypatch.setattr(session, "_container_agent", Mock(return_value=None))
    monkeypatch.setattr(session, "_select_command_runner", Mock(return_value=control._runner))
    monkeypatch.setattr(session, "_build_repl_env", lambda: {"SECRET": SECRET})
    monkeypatch.setattr(session, "_build_claude_cmd", lambda: "true")
    monkeypatch.setattr(session, "_prepare_tmux_spawn", lambda: None)
    monkeypatch.setattr(session, "_spawn_cleanup_state_dir", lambda: home)
    monkeypatch.setattr(session, "_start_tailer", AsyncMock())
    return session


@pytest.mark.parametrize("kind", ["claude", "codex"])
@pytest.mark.parametrize("failure", ["timeout", "cancel", "dead", "probe", "delay_cancel", "tailer"])
async def test_every_post_spawn_rollback_cleans_after_kill(home, monkeypatch, kind, failure):
    control = _TmuxControl("rollback-test", command_runner=LaunchRecorder(home))
    session = session_for(home, control, monkeypatch, kind)
    monkeypatch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0)
    probe = False if failure == "dead" else RuntimeError("probe failed") if failure == "probe" else True
    monkeypatch.setattr(control, "has_session", AsyncMock(side_effect=[False, probe]))
    events = []
    real_new = control.new_session

    async def spawn(**kwargs):
        result = await real_new(**kwargs)
        assert result.ok and secret_files(home, SECRET)
        events.append("spawn")
        if failure == "cancel":
            asyncio.current_task().cancel()
        return result

    def check_owner():
        if events and failure == "timeout":
            raise TimeoutError("expired after successful spawn")

    async def kill():
        assert secret_files(home, SECRET), "cleanup must follow the kill attempt"
        events.append("kill")
        return TmuxCommandResult(0, "", "")

    real_cancel = tmux_launch_env.cancel_env

    def cleanup(scope, nonce):
        assert events[-1] == "kill"
        events.append("cleanup")
        real_cancel(scope, nonce)

    monkeypatch.setattr(control, "new_session", spawn)
    monkeypatch.setattr(control, "kill_session", kill)
    monkeypatch.setattr(session, "_check_startup_owner", check_owner)
    monkeypatch.setattr(tmux_launch_env, "cancel_env", cleanup)
    if failure == "delay_cancel":
        monkeypatch.setattr(tmux_session, "_async_sleep", AsyncMock(side_effect=asyncio.CancelledError))
    if failure == "tailer":
        monkeypatch.setattr(session, "_start_tailer", AsyncMock(side_effect=RuntimeError("tailer failed")))
    task = asyncio.create_task(session._spawn_tmux_repl())
    with pytest.raises((RuntimeError, asyncio.CancelledError)):
        await task
    assert events == ["spawn", "kill", "cleanup"]
    assert not secret_files(home, SECRET)


async def test_real_tmux_missing_loader_is_cleaned_on_liveness_rollback(home, monkeypatch):
    if TMUX_BINARY is None:
        pytest.skip("real tmux unavailable")
    root = Path(tempfile.mkdtemp(prefix="tmux-orphan-", dir="/tmp"))
    socket = root / "server.sock"
    control = _TmuxControl("orphan-test", tmux_binary=TMUX_BINARY, socket_path=str(socket))
    session = session_for(home, control, monkeypatch)
    monkeypatch.setattr(tmux_session, "sys", SimpleNamespace(executable="/nonexistent/python3"))
    try:
        with pytest.raises(RuntimeError, match="session died immediately"):
            await session._spawn_tmux_repl()
        assert not secret_files(home, SECRET)
    finally:
        subprocess.run([TMUX_BINARY, "-S", str(socket), "kill-server"], capture_output=True, timeout=5)
        shutil.rmtree(root)


def test_any_scope_collects_abandoned_secrets_but_preserves_young_and_locked(home):
    old = Path(stage(home)["path"])
    young = Path(stage(home, nonce=OTHER_NONCE)["path"])
    locked = Path(stage(home, scope="d4" * 32)["path"])
    metadata = old.with_suffix(".lock")
    age = time.time() - 900
    for path in (old, metadata, locked):
        os.utime(path, (age, age))
    fd = os.open(locked.with_suffix(".lock"), os.O_RDWR)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        stage(home, scope="e5" * 32)
        assert not old.exists(), "abandoned scope must not require its own next launch"
        assert young.exists() and locked.exists() and metadata.exists()
    finally:
        os.close(fd)


def test_empty_launch_sweeps_siblings_without_creating_its_own_scope(home):
    old = Path(stage(home)["path"])
    os.utime(old, (time.time() - 900,) * 2)
    assert stage(home, scope="e5" * 32, env={"SECRET": ""}) is None
    assert not old.exists()
    assert not (old.parent.parent / ("e5" * 32)).exists()


@pytest.mark.parametrize("offset", [-90, -2, 2, 90])
def test_target_clock_skew_does_not_reject_fresh_request(home, monkeypatch, offset):
    now = time.time()
    monkeypatch.setattr(tmux_launch_env.time, "time", lambda: now + offset)
    assert Path(stage(home, deadline=now + 60)["path"]).exists()


@pytest.mark.parametrize("skew", [-300, 300])
def test_first_entry_after_marker_gc_rejects_original_request(home, monkeypatch, skew):
    original_now = time.time()
    original_deadline = original_now + 60
    cancel()
    directory = home / ".local/state/pinkybot/tmux-launch-env" / SCOPE
    metadata = list(directory.iterdir())
    for path in metadata:
        os.utime(path, (original_now - 2 * 86400,) * 2)
    stage(home, nonce=OTHER_NONCE)
    assert all(not path.exists() for path in metadata)
    monkeypatch.setattr(tmux_launch_env.time, "time", lambda: original_now + 2 * 86400 + skew)
    with pytest.raises(ValueError):
        stage(home, deadline=original_deadline)
    assert not any(NONCE in p.name for p in directory.iterdir())


def test_local_lease_starts_before_directory_walk(home, monkeypatch):
    mono = time.monotonic()
    monkeypatch.setattr(tmux_launch_env.time, "monotonic", lambda: mono)
    real_home = Path.home

    def slow_home():
        nonlocal mono
        mono += 61
        return real_home()

    monkeypatch.setattr(Path, "home", slow_home)
    with pytest.raises(ValueError):
        stage(home)
    assert not secret_files(home, SECRET)
    assert not (home / ".local").exists(), "expired lease created metadata after the walk delay"


async def test_cancellation_during_nonok_cleanup_does_not_clean_twice(home):
    recorder = FailingRunner(home, launch_failure="returncode")
    recorder.cleanup_release.clear()
    control = _TmuxControl("one-cleanup", command_runner=RunuserCommandRunner("test", inner=recorder))
    task = asyncio.create_task(control.new_session(cwd=str(home), command="true", env={"SECRET": SECRET}))
    try:
        await asyncio.wait_for(recorder.cleanup_entered.wait(), 3)
        task.cancel()
        recorder.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert len([r for r in recorder.requests if r.get("action") == "cancel"]) == 1
        assert not secret_files(home, SECRET)
    finally:
        recorder.cleanup_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("kind", ["symlink", "writable", "invalid_name"])
def test_sibling_sweep_preserves_untrusted_directories(home, kind):
    own = Path(stage(home)["path"])
    root = own.parent.parent
    sibling = root / ("f6" * 32)
    if kind == "symlink":
        directory = home / "foreign"
        directory.mkdir(mode=0o700)
        sibling.symlink_to(directory, target_is_directory=True)
    else:
        directory = sibling if kind == "writable" else root / "unrelated"
        directory.mkdir(mode=0o700)
        if kind == "writable":
            directory.chmod(0o777)
    foreign = directory / f"env-{NONCE}.json"
    foreign.write_text(SECRET)
    foreign.chmod(0o600)
    os.utime(foreign, (time.time() - 900,) * 2)
    stage(home, scope="e5" * 32)
    assert foreign.read_text() == SECRET
