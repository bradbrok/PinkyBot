"""Verify-only server policy: raw line names, bounded reads, no repair."""

import asyncio
from types import SimpleNamespace

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.command_runner import CommandResult, LocalCommandRunner
from pinky_daemon.isolated_launch_env import LaunchEnvError
from tests.tmux_server_env_support import CANARY, control, no_values, owner, seed


@pytest.fixture
def rig(tmp_path, monkeypatch, caplog):
    seed(monkeypatch, tmp_path)
    monkeypatch.setenv("PINKY_TMUX_SOCKET", "test-fleet")
    state = SimpleNamespace(normal=b"PATH=/usr/bin:/bin\nPWD=/synthetic\nTMUX_TMPDIR=/synthetic\n",
                            hidden=b"", version=b"tmux 3.2a\n", calls=[], logs=[], staged=[], error=None)
    state.real_stage = tmux_session.tmux_launch_env.stage_env

    async def run(self, argv, **kwargs):
        state.calls.append((list(argv), kwargs))
        if "-V" in argv:
            return CommandResult(0, state.version, b"")
        if "show-environment" in argv:
            if state.error == "timeout":
                raise asyncio.TimeoutError(CANARY)
            if state.error == "exception":
                raise OSError(CANARY)
            if state.error == "nonzero":
                return CommandResult(1, CANARY.encode(), CANARY.encode())
            return CommandResult(0, state.hidden if "-h" in argv else state.normal, b"")
        return CommandResult(0, b"", b"")

    def stage(*args, **kwargs):
        state.staged.append(True)
        if kwargs.get("inherit") == "none":
            raise AssertionError("isolated launch staged before rejecting server")
        return None

    monkeypatch.setattr(LocalCommandRunner, "run", run)
    monkeypatch.setattr(tmux_session.tmux_launch_env, "stage_env", stage)
    monkeypatch.setattr(tmux_session, "_log", state.logs.append)
    state.ctrl = control(owner("claude", tmp_path))
    state.root = tmp_path
    yield state
    no_values(state.logs, caplog.text, [a for a, _ in state.calls])


async def launch(rig, *, clean=True):
    return await rig.ctrl.new_session(cwd=str(rig.root), command="true", env={},
                                      inherit="none" if clean else "all")


@pytest.mark.parametrize("view", ["normal", "hidden"])
@pytest.mark.parametrize("entry", ["UNLISTED_DAEMON_NAME=value\n", "-UNLISTED_DAEMON_NAME\n",
                                   "ODD-NAME=value\n", "PATH=/safe\nPHANTOM=value\n"])
async def test_foreign_names_refuse_before_staging(rig, view, entry):
    # A multiline allowed value can add a phantom assignment-looking line.
    # The deliberately line-based parser rejects this in the safe direction.
    setattr(rig, view, entry.replace("value", CANARY).encode())
    with pytest.raises(LaunchEnvError) as caught:
        await launch(rig)
    assert not rig.staged
    assert not any("new-session" in a for a, _ in rig.calls)
    assert all(name not in str(caught.value) for name in ("UNLISTED", "ODD", "PHANTOM"))
    no_values(str(caught.value))


@pytest.mark.parametrize("failure", ["nonzero", "timeout", "exception", "oversize", "utf8", "old_version", "bad_version"])
@pytest.mark.parametrize("clean", [True, False])
async def test_read_failure_refuses_or_warns_without_value_leak(rig, failure, clean, caplog):
    if failure == "oversize":
        rig.normal = b"HOME=" + b"x" * (2 * 1024 * 1024) + b"\n"
    elif failure == "utf8":
        rig.hidden = b"HOME=\xff\n"
    elif failure == "old_version":
        rig.version = b"tmux 3.1c\n"
    elif failure == "bad_version":
        rig.version = CANARY.encode()
    else:
        rig.error = failure
    if clean:
        with pytest.raises(LaunchEnvError) as caught:
            await launch(rig)
        assert not rig.staged
        no_values(str(caught.value))
    else:
        assert (await launch(rig, clean=False)).ok
        assert (await launch(rig, clean=False)).ok
        messages = rig.logs + [r.getMessage() for r in caplog.records]
        assert len(messages) == 1, "compatibility read failure needs one deduplicated warning"


@pytest.mark.parametrize("view", ["normal", "hidden"])
async def test_compatibility_warns_once_names_only_and_never_repairs(rig, view, caplog):
    setattr(rig, view, ("UNLISTED_DAEMON_NAME=" + CANARY + "\n").encode())
    await launch(rig, clean=False)
    await launch(rig, clean=False)
    messages = rig.logs + [r.getMessage() for r in caplog.records]
    assert len(messages) == 1, "foreign server names need one deduplicated warning"
    assert "UNLISTED_DAEMON_NAME" in messages[0]
    assert not any("set-environment" in a or "kill-server" in a for a, _ in rig.calls)


async def test_both_views_verified_each_launch_capability_once_per_server(rig):
    await launch(rig, clean=False)
    await launch(rig, clean=False)
    views = [(a, kw) for a, kw in rig.calls if "show-environment" in a]
    assert len(views) == 4, "each launch must verify both normal and hidden views"
    assert sum("-h" in a for a, _ in views) == 2
    assert all("-g" in a and 0 < kw.get("timeout", 0) <= 5 for a, kw in views)
    assert sum("-V" in a for a, _ in rig.calls) == 1
    assert not any("set-environment" in a for a, _ in rig.calls)


async def test_clean_allowed_server_verifies_before_staging_and_launch(rig, monkeypatch):
    reads_at_stage = []

    def stage(*args, **kwargs):
        reads_at_stage.append([a for a, _ in rig.calls if "show-environment" in a])
        return rig.real_stage(*args, **kwargs)

    monkeypatch.setattr(tmux_session.tmux_launch_env, "stage_env", stage)
    result = await launch(rig)
    try:
        assert result.ok
        assert reads_at_stage and len(reads_at_stage[0]) == 2, "payload staged before both environment views were verified"
        assert any("new-session" in a for a, _ in rig.calls)
    finally:
        await tmux_session._cleanup_launch_env(result.launch_env)


async def test_capability_cached_across_controls_on_same_server(rig):
    await launch(rig, clean=False)
    rig.ctrl = control(owner("claude", rig.root, agent="other-agent"))
    await launch(rig, clean=False)
    assert sum("-V" in a for a, _ in rig.calls) == 1
    assert sum("show-environment" in a for a, _ in rig.calls) == 4


@pytest.mark.parametrize("view", ["normal", "hidden"])
async def test_allowed_removed_markers_do_not_become_foreign_names(rig, view, caplog):
    setattr(rig, view, b"-HOME\n-TMUX_TMPDIR\n-PWD\nLC_TIME=C\nXDG_CONFIG_HOME=/synthetic\n")
    assert (await launch(rig, clean=False)).ok
    assert not rig.logs and not caplog.records
    assert sum("show-environment" in a for a, _ in rig.calls) == 2


@pytest.mark.parametrize("clean", [True, False])
async def test_shared_default_skips_verification_but_refuses_clean(rig, monkeypatch, clean):
    monkeypatch.setenv("PINKY_TMUX_SOCKET", "")
    rig.ctrl = control(owner("claude", rig.root))
    if clean:
        with pytest.raises(LaunchEnvError):
            await launch(rig)
        assert not rig.staged
    else:
        assert (await launch(rig, clean=False)).ok
    assert not any("show-environment" in a or "set-environment" in a for a, _ in rig.calls)


def test_diagnostic_scanner_positive_control():
    with pytest.raises(AssertionError, match="protected synthetic"):
        no_values(CANARY)
