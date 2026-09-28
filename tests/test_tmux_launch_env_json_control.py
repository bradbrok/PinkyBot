"""Launch failure cleanup is nonce-specific and awaited through cancellation."""

import asyncio
import hashlib
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from pinky_daemon.codex_app_server_tmux import CodexAppServerSupervisor
from pinky_daemon.command_runner import CommandResult, RunuserCommandRunner
from pinky_daemon.tmux_session import _TmuxControl
from tests.tmux_env_r3_support import SECRET, probe_command
from tests.tmux_env_support import LaunchRecorder, secret_files


@pytest.fixture
def home(tmp_path, monkeypatch):
    home = tmp_path / "target-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    return home


class FailingRunner(LaunchRecorder):
    """Execute the real target helper, then lose its response or fail tmux."""

    def __init__(self, home, *, stage_failure=None, launch_failure=None):
        super().__init__(home)
        self.stage_failure = stage_failure
        self.launch_failure = launch_failure
        self.stage_entered = asyncio.Event()
        self.launch_entered = asyncio.Event()
        self.cleanup_entered = asyncio.Event()
        self.cleanup_release = asyncio.Event()
        self.cleanup_release.set()
        self.written = []
        self.requests = []

    async def run(self, argv, *, timeout=None, stdin_data=None):
        request = json.loads(stdin_data) if stdin_data else None
        if request is not None:
            self.requests.append(request)
        action = request.get("action", "stage") if request else None
        if action == "cancel":
            self.cleanup_entered.set()
            await self.cleanup_release.wait()
        if "new-session" in argv:
            self.launch_entered.set()
            if self.launch_failure == "returncode":
                self.calls.append((list(argv), stdin_data))
                self.tmux_calls.append(list(argv))
                return CommandResult(1, b"", b"synthetic tmux refusal")
            if self.launch_failure == "timeout":
                raise TimeoutError("synthetic tmux timeout")
            if self.launch_failure == "exception":
                raise OSError("synthetic tmux exception")
            if self.launch_failure == "cancel":
                await asyncio.Event().wait()
        result = await super().run(argv, timeout=timeout, stdin_data=stdin_data)
        if action == "stage":
            self.written.extend(secret_files(self.home, SECRET))
            self.stage_entered.set()
            if self.stage_failure == "timeout":
                raise TimeoutError("synthetic staging timeout after write")
            if self.stage_failure == "cancel":
                await asyncio.Event().wait()
            if self.stage_failure == "malformed":
                return CommandResult(0, b"{truncated", b"")
            if callable(self.stage_failure):
                return CommandResult(0, json.dumps(self.stage_failure(json.loads(result.stdout))).encode(), b"")
        return result


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("failure", ["returncode", "timeout", "exception", "cancel"])
async def test_tmux_failure_removes_own_file_before_return_or_raise(home, remote, failure):
    recorder = FailingRunner(home, launch_failure=failure)
    runner = RunuserCommandRunner("test", inner=recorder) if remote else recorder
    control = _TmuxControl("launch-failure", command_runner=runner)
    task = asyncio.create_task(control.new_session(cwd=str(home), command="true", env={"SECRET": SECRET}))
    if failure == "cancel":
        await asyncio.wait_for(recorder.launch_entered.wait(), 3)
        task.cancel()
    if failure == "returncode":
        assert not (await task).ok
    else:
        with pytest.raises((TimeoutError, OSError, asyncio.CancelledError)):
            await task
    assert not secret_files(home, SECRET), "launch completion must include cleanup completion"


@pytest.mark.parametrize("failure", ["timeout", "cancel", "malformed"])
async def test_remote_staging_write_then_lost_response_is_cleaned(home, failure):
    recorder = FailingRunner(home, stage_failure=failure)
    control = _TmuxControl("stage-failure", command_runner=RunuserCommandRunner("test", inner=recorder))
    task = asyncio.create_task(control.new_session(cwd=str(home), command="true", env={"SECRET": SECRET}))
    if failure == "cancel":
        await asyncio.wait_for(recorder.stage_entered.wait(), 3)
        task.cancel()
    with pytest.raises((TimeoutError, RuntimeError, asyncio.CancelledError)):
        await task
    assert recorder.written, "the failure must occur AFTER the real target write"
    assert not recorder.tmux_calls
    assert not secret_files(home, SECRET)


@pytest.mark.parametrize("phase", ["stage", "launch"])
async def test_repeated_cancellation_cannot_finish_before_cleanup(home, phase):
    recorder = FailingRunner(home, stage_failure="cancel" if phase == "stage" else None,
                             launch_failure="cancel" if phase == "launch" else None)
    recorder.cleanup_release.clear()
    control = _TmuxControl("cancel-cleanup", command_runner=RunuserCommandRunner("test", inner=recorder))
    task = asyncio.create_task(control.new_session(cwd=str(home), command="true", env={"SECRET": SECRET}))
    try:
        entered = recorder.stage_entered if phase == "stage" else recorder.launch_entered
        await asyncio.wait_for(entered.wait(), 3)
        task.cancel()
        await asyncio.wait_for(recorder.cleanup_entered.wait(), 3)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        await asyncio.sleep(0)
        assert not task.done(), "repeated cancellation detached still-running cleanup"
        assert secret_files(home, SECRET)
        recorder.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not secret_files(home, SECRET)
    finally:
        recorder.cleanup_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def test_delayed_cleanup_of_a_preserves_staged_b(home):
    first = FailingRunner(home, launch_failure="cancel")
    first.cleanup_release.clear()
    a = _TmuxControl("same-session", command_runner=RunuserCommandRunner("test", inner=first))
    task = asyncio.create_task(a.new_session(cwd=str(home), command="true", env={"SECRET": SECRET}))
    try:
        await asyncio.wait_for(first.launch_entered.wait(), 3)
        first_path = secret_files(home, SECRET)[0]
        task.cancel()
        await asyncio.wait_for(first.cleanup_entered.wait(), 3)
        second = FailingRunner(home)
        b = _TmuxControl("same-session", command_runner=RunuserCommandRunner("test", inner=second))
        await b.new_session(cwd=str(home), command="true", env={"SECRET": SECRET})
        second_path = next(p for p in secret_files(home, SECRET) if p != first_path)
        first.cleanup_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert not first_path.exists()
        assert second_path.exists()
    finally:
        first.cleanup_release.set()
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.parametrize("shape", ["relative", "outside", "nul", "newline", "traversal",
                                    "extra", "missing", "list", "none", "wrong_nonce", "wrong_scope"])
async def test_forged_staging_response_refused_and_real_file_cleaned(home, shape):
    foreign = home / "foreign-file"
    foreign.write_text("preserve foreign contents")

    def corrupt(data):
        path = data["path"]
        if shape == "relative":
            return {"path": "relative.json"}
        if shape == "outside":
            return {"path": str(foreign)}
        if shape == "nul":
            return {"path": path + "\x00"}
        if shape == "newline":
            return {"path": path + "\n"}
        if shape == "traversal":
            return {"path": str(Path(path).parent / ".." / Path(path).parent.name / Path(path).name)}
        if shape == "extra":
            return {**data, "extra": "untrusted"}
        if shape == "missing":
            return {}
        if shape == "list":
            return [path]
        if shape == "none":
            return None
        if shape == "wrong_nonce":
            return {"path": str(Path(path).with_name("env-" + "f" * 32 + ".json"))}
        return {"path": str(Path(path).parent.parent / ("f" * 64) / Path(path).name)}

    recorder = FailingRunner(home, stage_failure=corrupt)
    control = _TmuxControl("response-validation", command_runner=RunuserCommandRunner("test", inner=recorder))
    with pytest.raises(RuntimeError) as error:
        await control.new_session(cwd=str(home), command="true", env={"SECRET": SECRET})
    assert SECRET not in str(error.value)
    assert recorder.written
    assert not recorder.tmux_calls
    assert not secret_files(home, SECRET)
    assert foreign.read_text() == "preserve foreign contents"


async def test_remote_scope_contract_uses_target_home_and_explicit_socket(home, monkeypatch):
    monkeypatch.setenv("HOME", "/different/daemon-home")
    monkeypatch.setenv("TMUX", "/daemon/socket,1,0")
    monkeypatch.setenv("TMUX_TMPDIR", "/daemon/tmp")
    recorder = FailingRunner(home)
    for socket in ["first", "second"]:
        control = _TmuxControl("scope-contract", socket_name=socket,
                               command_runner=RunuserCommandRunner("test", inner=recorder))
        await control.new_session(cwd=str(home), command=probe_command(), env={"SECRET": SECRET})
        request = next(r for r in reversed(recorder.requests) if "env" in r)
        scope = hashlib.sha256(json.dumps([control._base_cmd(), control.session_name],
                                         separators=(",", ":")).encode()).hexdigest()
        assert request["scope"] == scope
        assert len(request["nonce"]) == 32
        expected = home / ".local/state/pinkybot/tmux-launch-env" / scope / f"env-{request['nonce']}.json"
        assert expected.exists(), "target must use daemon scope verbatim, never rehash"
    scopes = [r["scope"] for r in recorder.requests if "env" in r]
    assert len(set(scopes)) == 2


@pytest.mark.parametrize("failure", [TimeoutError, OSError, asyncio.CancelledError])
async def test_app_server_accept_failure_cleans_actual_staged_file(home, failure):
    recorder = FailingRunner(home)
    control = _TmuxControl("accept-failure", command_runner=recorder)
    with patch.object(CodexAppServerSupervisor, "_resolve_sock_dir", return_value=(str(home), False)):
        supervisor = CodexAppServerSupervisor("test", working_dir=str(home))
    supervisor._tmux = control
    with patch.object(supervisor, "_kill_tmux_session", new=AsyncMock()), \
         patch.object(supervisor, "_ensure_sock_dir_secure"), \
         patch.object(supervisor, "_unlink_sock"), \
         patch.object(supervisor, "_build_env", return_value={"SECRET": SECRET}), \
         patch.object(supervisor, "_await_accept", new=AsyncMock(side_effect=failure("synthetic"))):
        with pytest.raises(failure):
            await supervisor.start()
    assert recorder.tmux_calls
    assert not secret_files(home, SECRET)
