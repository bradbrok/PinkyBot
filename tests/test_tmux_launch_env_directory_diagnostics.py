"""Directory refusals stay private and startup warnings reach the daemon log."""

import asyncio
import copy
import errno
import importlib
import os
import pickle
import stat
import sys
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from pinky_daemon import tmux_launch_env, tmux_session
from pinky_daemon.command_runner import ContainerCommandRunner, RunuserCommandRunner
from tests.tmux_env_support import LaunchRecorder

SCOPE = "c" * 64
NONCE = "d" * 32
PARTS = (".local", "state", "pinkybot", "tmux-launch-env")


@pytest.fixture
def home(tmp_path, monkeypatch):
    root = tmp_path / "home"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(root))
    return root


@pytest.fixture(autouse=True)
def fresh_startup_process():
    module = sys.modules.get("pinky_daemon.tmux_launch_env_startup")
    if module is not None:
        importlib.reload(module)


def state_tree(home):
    directory = home
    for part in PARTS:
        directory /= part
        directory.mkdir(mode=0o700)
    return directory


def stage():
    return tmux_launch_env.stage_env(
        {"SYNTHETIC": "synthetic-value"}, SCOPE, NONCE, deadline=time.time() + 60,
    )


def assert_private_message(message, home):
    for forbidden in (str(home), SCOPE, NONCE, "synthetic-value", "sensitive-detail"):
        assert forbidden not in message


def expected_refusal(label, mode, rule, owner=None):
    prefix = "unsafe launch environment home" if label == "~" else (
        "unsafe launch environment directory"
    )
    return (
        f"{prefix}: {label} mode {mode:04o} "
        f"owner uid {os.geteuid() if owner is None else owner} "
        f"effective uid {os.geteuid()} ({rule})"
    )


def assert_details(message, home, label, mode, rule, owner=None):
    assert message == expected_refusal(label, mode, rule, owner)
    assert_private_message(message, home)


def expected_warning(label, mode, rule, hint, owner=None):
    return (
        "WARNING launch environment state directory is unsafe: "
        f"{expected_refusal(label, mode, rule, owner)}; "
        f"agent panes will fail to launch until fixed: {hint}"
    )


@pytest.mark.parametrize("component,mode,rule", [
    (".local", 0o775, "group/other-writable"),
    (".local/state", 0o775, "group/other-writable"),
    (".local/state/pinkybot", 0o775, "group/other-writable"),
    (".local/state/pinkybot/tmux-launch-env", 0o750, "group/other access"),
])
def test_new_fixed_component_message(home, component, mode, rule):
    state_tree(home)
    (home / component).chmod(mode)
    with pytest.raises(PermissionError) as caught:
        stage()
    assert_details(str(caught.value), home, "~/" + component, mode, rule)


def fake_owner(monkeypatch, path):
    target = path.stat()
    original = os.fstat
    owner = os.geteuid() + 1

    def fstat(fd):
        info = original(fd)
        if (info.st_dev, info.st_ino) == (target.st_dev, target.st_ino):
            fields = list(info)
            fields[4] = owner
            return os.stat_result(fields)
        return info

    monkeypatch.setattr(tmux_launch_env.os, "fstat", fstat)
    return owner


@pytest.mark.parametrize("component,label", [
    ("", "~"), (".local/state", "~/.local/state"),
    (".local/state/pinkybot/tmux-launch-env/" + SCOPE, "scope"),
])
def test_new_owner_message(home, monkeypatch, component, label):
    scope = state_tree(home) / SCOPE
    scope.mkdir(mode=0o700)
    owner = fake_owner(monkeypatch, home / component)
    with pytest.raises(PermissionError) as caught:
        stage()
    assert_details(str(caught.value), home, label, 0o700, "owner", owner)


def test_new_home_message(home):
    home.chmod(0o777)
    with pytest.raises(PermissionError) as caught:
        stage()
    assert_details(str(caught.value), home, "~", 0o777, "group/other-writable")


def test_new_scope_message(home):
    scope = state_tree(home) / SCOPE
    scope.mkdir(mode=0o750)
    with pytest.raises(PermissionError) as caught:
        stage()
    assert_details(str(caught.value), home, "scope", 0o750, "group/other access")


@pytest.mark.parametrize("operation", ["copy", "deepcopy", "pickle"])
@pytest.mark.parametrize("annotated", [False, True])
def test_new_directory_error_round_trip(home, monkeypatch, operation, annotated):
    state_tree(home)
    target = home / ".local/state"
    target.chmod(0o1775)
    owner = fake_owner(monkeypatch, target)
    with pytest.raises(PermissionError) as caught:
        stage()
    original = caught.value
    assert_details(str(original), home, "~/.local/state", 0o1775, "owner", owner)
    fields = {
        "label": "~/.local/state", "euid": os.geteuid(), "rule": "owner",
    }
    assert vars(original) == fields
    if annotated:
        tmux_session.TmuxSession._annotate_spawn_rollback_failure(
            original, "synthetic rollback failure",
        )
        fields["__notes__"] = ["synthetic rollback failure"]
        assert str(original) == (
            expected_refusal("~/.local/state", 0o1775, "owner", owner)
            + "; synthetic rollback failure"
        )
    restored = (pickle.loads(pickle.dumps(original)) if operation == "pickle" else
                getattr(copy, operation)(original))
    assert type(restored) is tmux_launch_env.UnsafeDirectoryError
    assert str(restored) == str(original)
    assert restored.args == original.args
    assert getattr(restored, "__notes__", None) == getattr(original, "__notes__", None)
    assert vars(original) == vars(restored) == fields
    assert_private_message(str(restored), home)


def test_new_conflicting_owner_and_mode_refusal(home, monkeypatch):
    state_tree(home)
    target = home / ".local/state"
    target.chmod(0o1775)
    owner = fake_owner(monkeypatch, target)
    with pytest.raises(PermissionError) as caught:
        stage()
    assert_details(str(caught.value), home, "~/.local/state", 0o1775, "owner", owner)


async def test_new_local_runner_preserves_message(home):
    state_tree(home)
    (home / ".local").chmod(0o775)
    recorder = LaunchRecorder(home)
    control = tmux_session._TmuxControl("synthetic", command_runner=recorder)
    with pytest.raises(PermissionError) as caught:
        await control.new_session(cwd=str(home), command="true", env={"SYNTHETIC": "value"})
    assert_details(str(caught.value), home, "~/.local", 0o775, "group/other-writable")
    assert not recorder.tmux_calls


class StopStartup(BaseException):
    """Stop the isolated API fixture before unrelated services start."""


@pytest.fixture
def startup(mode, tmp_path, monkeypatch):
    if mode == "api":
        from pinky_daemon import api

        monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False)
        app = api.create_api(
            default_working_dir=str(tmp_path), db_path=str(tmp_path / "test.db"),
        )
        boundary = Mock(side_effect=StopStartup)
        monkeypatch.setattr(app.state.storage_observability, "enable_runtime", boundary)
        handler = next(h for h in app.router.on_startup if h.__name__ == "on_startup")

        async def start():
            with pytest.raises(StopStartup):
                await handler()
            assert boundary.called

        module = api
    else:
        from pinky_daemon import daemon

        instance = object.__new__(daemon.Daemon)
        instance._config = daemon.DaemonConfig()
        instance._running = False
        instance._pollers = []
        instance._tasks = []
        instance._registry = SimpleNamespace(get_main_agent=lambda: None)
        instance._scheduler = SimpleNamespace(start=AsyncMock())
        instance._autonomy = SimpleNamespace(start=AsyncMock())
        async def start():
            monkeypatch.setattr(
                asyncio.get_running_loop(), "add_signal_handler", lambda *args: None,
            )
            await instance.start()
            assert instance._scheduler.start.await_count > 0

        module = daemon

    logged = []
    normal_log = module._log

    def log(message):
        logged.append(message)
        normal_log(message)

    monkeypatch.setattr(module, "_log", log)
    return start, logged


def warnings(logged):
    return [line for line in logged if line.startswith("WARNING launch environment state directory")]


@pytest.mark.parametrize("mode", ["api", "poll"])
@pytest.mark.parametrize("component,permissions,label,rule,hint", [
    (".local/state", 0o775, "~/.local/state", "group/other-writable",
     "chmod g-w,o-w ~/.local/state"),
    (".local/state/pinkybot/tmux-launch-env", 0o750,
     "~/.local/state/pinkybot/tmux-launch-env", "group/other access",
     "chmod g-rwx,o-rwx ~/.local/state/pinkybot/tmux-launch-env"),
    ("", 0o777, "~", "group/other-writable", "chmod g-w,o-w ~"),
])
async def test_new_startup_warning_once_on_normal_sink(
    home, mode, startup, capsys, component, permissions, label, rule, hint,
):
    state_tree(home)
    (home / component).chmod(permissions)
    start, logged = startup
    await start()
    await start()
    lines = warnings(logged)
    line = expected_warning(label, permissions, rule, hint)
    assert lines == [line]
    assert_private_message(line, home)
    tmux_session._log("synthetic tmux launch sink marker")
    stderr = capsys.readouterr().err.splitlines()
    assert stderr.count(line) == 1
    assert "synthetic tmux launch sink marker" in stderr


@pytest.mark.parametrize("mode", ["api", "poll"])
async def test_new_startup_owner_hint(home, mode, startup, monkeypatch):
    state_tree(home)
    owner = fake_owner(monkeypatch, home / ".local/state")
    start, logged = startup
    await start()
    lines = warnings(logged)
    assert lines == [expected_warning(
        "~/.local/state", 0o700, "owner",
        f"~/.local/state must be owned by uid {os.geteuid()}", owner,
    )]
    assert "chown" not in lines[0]
    assert_private_message(lines[0], home)


@pytest.mark.parametrize("mode", ["api", "poll"])
async def test_new_conflicting_owner_and_mode_warning(home, mode, startup, monkeypatch):
    state_tree(home)
    target = home / ".local/state"
    target.chmod(0o1775)
    owner = fake_owner(monkeypatch, target)
    start, logged = startup
    await start()
    await start()
    assert warnings(logged) == [expected_warning(
        "~/.local/state", 0o1775, "owner",
        f"~/.local/state must be owned by uid {os.geteuid()}", owner,
    )]
    assert_private_message(warnings(logged)[0], home)


@pytest.mark.parametrize("mode", ["api", "poll"])
@pytest.mark.parametrize("error_number", [errno.EACCES, errno.ELOOP, 999999])
async def test_new_startup_unexpected_error_is_symbol_only(
    home, mode, startup, monkeypatch, error_number,
):
    def fail(**kwargs):
        raise OSError(error_number, "sensitive-detail", str(home / "sensitive-detail"))

    monkeypatch.setattr(tmux_launch_env, "_state_directory", fail)
    start, logged = startup
    await start()
    await start()
    symbol = errno.errorcode.get(error_number, "UNKNOWN")
    assert warnings(logged) == [
        f"WARNING launch environment state directory check failed: {symbol}",
    ]


@pytest.mark.parametrize("mode", ["api", "poll"])
async def test_new_startup_non_oserror_is_unknown(home, mode, startup, monkeypatch):
    class UnexpectedError(Exception):
        pass

    def fail(**kwargs):
        raise UnexpectedError("sensitive-detail: " + str(home))

    monkeypatch.setattr(tmux_launch_env, "_state_directory", fail)
    start, logged = startup
    await start()
    await start()
    assert warnings(logged) == [
        "WARNING launch environment state directory check failed: UNKNOWN",
    ]


@pytest.mark.parametrize("mode", ["api", "poll"])
async def test_new_startup_log_failure_does_not_stop_startup(home, mode, startup, monkeypatch):
    class UnexpectedError(Exception):
        pass

    state_tree(home)
    (home / ".local/state").chmod(0o775)
    start, logged = startup
    module = importlib.import_module("pinky_daemon." + ("api" if mode == "api" else "daemon"))
    normal_log = module._log
    attempted = []

    def fail(message):
        if message.startswith("WARNING launch environment state directory"):
            attempted.append(message)
            raise UnexpectedError("sensitive-detail: " + str(home))
        normal_log(message)

    monkeypatch.setattr(module, "_log", fail)
    await start()
    await start()
    assert attempted == [expected_warning(
        "~/.local/state", 0o775, "group/other-writable", "chmod g-w,o-w ~/.local/state",
    )]
    assert warnings(logged) == []


@pytest.mark.parametrize("mode", ["api", "poll"])
async def test_new_startup_symlink_error_is_symbol_only(home, mode, startup, tmp_path):
    target = tmp_path / "target"
    target.mkdir(mode=0o700)
    (home / ".local").symlink_to(target, target_is_directory=True)
    with pytest.raises(OSError) as caught:
        with tmux_launch_env._state_directory(create=False):
            pass
    start, logged = startup
    await start()
    symbol = errno.errorcode[caught.value.errno]
    assert warnings(logged) == [
        f"WARNING launch environment state directory check failed: {symbol}",
    ]


@pytest.mark.parametrize("mode", ["api", "poll"])
@pytest.mark.parametrize("tree", ["safe", "missing"])
async def test_control_safe_or_missing_startup_is_silent(home, mode, tree, startup):
    if tree == "safe":
        state_tree(home)
    before = sorted(str(path.relative_to(home)) for path in home.rglob("*"))
    start, logged = startup
    await start()
    assert warnings(logged) == []
    assert before == sorted(str(path.relative_to(home)) for path in home.rglob("*"))
    if tree == "missing":
        assert not (home / ".local").exists()


@pytest.mark.parametrize("mode", ["api", "poll"])
async def test_control_startup_never_changes_modes(home, mode, startup):
    state_tree(home)
    (home / ".local/state").chmod(0o775)
    paths = [home, *home.rglob("*")]
    before = [stat.S_IMODE(path.stat().st_mode) for path in paths]
    start, _ = startup
    await start()
    assert before == [stat.S_IMODE(path.stat().st_mode) for path in paths]


@pytest.mark.parametrize("remote", ["runuser", "container"])
async def test_control_nonlocal_refusal_text_is_exactly_generic(home, remote, capsys):
    state_tree(home)
    (home / ".local").chmod(0o775)
    recorder = LaunchRecorder(home)
    runner = (RunuserCommandRunner("synthetic", inner=recorder) if remote == "runuser" else
              ContainerCommandRunner("synthetic", inner=recorder))
    control = tmux_session._TmuxControl("synthetic", command_runner=runner)
    with pytest.raises(RuntimeError) as caught:
        await control.new_session(cwd=str(home), command="true", env={"SYNTHETIC": "value"})
    assert str(caught.value) == "launch environment staging failed"
    assert not recorder.tmux_calls
    assert_private_message(capsys.readouterr().err, home)
