"""Dream launch receipts are revoked and staging failures retain their result shape."""

import asyncio
import secrets
import shlex
import time
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import tmux_launch_env
from pinky_daemon.command_runner import LocalCommandRunner
from pinky_daemon.isolated_launch_env import LaunchEnvError
from pinky_daemon.tmux_dream_runner import TmuxDreamConfig, TmuxDreamRunner
from pinky_daemon.tmux_session import TmuxCommandResult, _cleanup_launch_env
from tests.test_claude_host_env_payload import (
    SENTINEL,
    private_server,
    scan_outputs,
    wait_report,
)
from tests.test_claude_host_env_payload import (
    clean_daemon as clean_daemon,
)


def runner_at(root, monkeypatch):
    runner = TmuxDreamRunner(
        TmuxDreamConfig(working_dir=str(root), claude_binary=str(root / "unused-inert-provider")),
        agent_name="test-agent",
    )
    monkeypatch.setattr(runner, "_seed_trust", lambda *args: False)
    return runner


def receipt_path(home, receipt):
    _, scope, nonce = receipt
    return home / ".local/state/pinkybot/tmux-launch-env" / scope / f"env-{nonce}.json"


@pytest.mark.parametrize("error_type", [TimeoutError, LaunchEnvError, RuntimeError])
async def test_spawn_error_returns_failure_without_values(clean_daemon, monkeypatch, error_type):
    runner = runner_at(clean_daemon, monkeypatch)
    monkeypatch.setattr(runner, "_tmux", AsyncMock(return_value=(0, "")))
    monkeypatch.setattr(runner._control, "new_session", AsyncMock(side_effect=error_type(SENTINEL)))
    escaped = False
    result = None
    try:
        result = await runner.run("synthetic prompt")
    except (TimeoutError, LaunchEnvError, RuntimeError):
        escaped = True
    assert not escaped, "spawn error escaped the result boundary"
    assert result is not None and result.exit_code == 1
    prefix_matches = result.error.startswith("tmux new-session failed")
    assert prefix_matches
    safe_error = SENTINEL not in result.error
    assert safe_error, "failure result contains a protected value"
    scan_outputs(result.error)


async def test_spawn_cancellation_propagates(clean_daemon, monkeypatch):
    runner = runner_at(clean_daemon, monkeypatch)
    monkeypatch.setattr(runner, "_tmux", AsyncMock(return_value=(0, "")))
    monkeypatch.setattr(
        runner._control, "new_session", AsyncMock(side_effect=asyncio.CancelledError)
    )
    with pytest.raises(asyncio.CancelledError):
        await runner.run("synthetic prompt")


@pytest.mark.parametrize("cancel", [False, True])
async def test_preloader_shell_exit_revokes_receipt(clean_daemon, tmp_path, monkeypatch, cancel):
    async with private_server(tmp_path, monkeypatch) as probe:
        shell = tmp_path / "exiting-shell"
        shell.write_text("#!/bin/sh\nexit 0\n")
        shell.chmod(0o700)
        changed = await probe.control._run("set-option", "-g", "default-shell", str(shell))
        assert changed.ok
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SENTINEL)
        runner = runner_at(tmp_path, monkeypatch)
        runner._config.claude_binary = str(probe.script)
        probe.control.session_name = runner.session_name
        runner._control = probe.control
        real_new = probe.control.new_session
        captured = []

        async def new_session(**kwargs):
            result = await real_new(**kwargs)
            assert result.ok and result.launch_env is not None
            path = receipt_path(clean_daemon, result.launch_env)
            assert path.is_file(), "pre-loader payload positive control"
            captured.append(path)

            async def wait_for_exit():
                while await probe.control.has_session():
                    await asyncio.sleep(0.01)

            await asyncio.wait_for(wait_for_exit(), timeout=2)
            return result

        monkeypatch.setattr(probe.control, "new_session", new_session)
        monkeypatch.setattr(
            runner,
            "_wait_ready",
            AsyncMock(side_effect=asyncio.CancelledError)
            if cancel
            else AsyncMock(return_value=True),
        )
        if cancel:
            with pytest.raises(asyncio.CancelledError):
                await runner.run("synthetic prompt")
        else:
            result = await runner.run("synthetic prompt")
            assert not result.ok
        assert not probe.report.exists(), "inert provider must never have started"
        assert captured
        payload_remains = captured[0].exists()
        assert not payload_remains, "unconsumed private payload remains after dream exit"


def staged_receipt(home):
    scope, nonce = secrets.token_hex(32), secrets.token_hex(16)
    staged = tmux_launch_env.stage_env(
        {"CLAUDE_CODE_OAUTH_TOKEN": SENTINEL},
        scope,
        nonce,
        deadline=time.time() + tmux_launch_env.PUBLICATION_TIMEOUT,
    )
    assert staged is not None
    return (LocalCommandRunner(), scope, nonce), Path(staged["path"])


async def test_non_ok_spawn_revokes_receipt_and_hides_stderr(clean_daemon, monkeypatch):
    # Defense in depth: today's control revokes receipts before returning non-ok.
    runner = runner_at(clean_daemon, monkeypatch)
    receipt, path = staged_receipt(clean_daemon)
    monkeypatch.setattr(runner, "_tmux", AsyncMock(return_value=(0, "")))
    monkeypatch.setattr(
        runner._control,
        "new_session",
        AsyncMock(
            return_value=TmuxCommandResult(
                returncode=1,
                stdout="",
                stderr=SENTINEL,
                launch_env=receipt,
            )
        ),
    )
    result = await runner.run("synthetic prompt")
    assert result.exit_code == 1
    payload_remains = path.exists()
    assert not payload_remains, "failed spawn retained private payload"
    safe_error = SENTINEL not in result.error
    assert safe_error, "failure result contains a protected value"
    scan_outputs(result.error)


async def test_real_non_ok_spawn_revokes_staged_payload(clean_daemon, tmp_path, monkeypatch):
    async with private_server(tmp_path, monkeypatch) as probe:
        runner = runner_at(tmp_path, monkeypatch)
        runner._config.claude_binary = str(probe.script)
        probe.control.session_name = runner.session_name
        runner._control = probe.control
        monkeypatch.setenv("CUSTOM_TOOL_TOKEN", SENTINEL + "daemon")
        staged_paths = []
        real_stage = tmux_launch_env.stage_env
        real_new = probe.control.new_session

        def stage(*args, **kwargs):
            staged = real_stage(*args, **kwargs)
            assert staged is not None
            path = Path(staged["path"])
            assert path.is_file(), "payload was staged before the real tmux failure"
            staged_paths.append(path)
            return staged

        async def duplicate_then_spawn(**kwargs):
            duplicate = await probe.control._run(
                "new-session", "-d", "-s", runner.session_name, "/bin/sleep 60"
            )
            assert duplicate.ok
            result = await real_new(**kwargs)
            assert not result.ok and result.launch_env is None
            assert staged_paths and not staged_paths[-1].exists()
            return result

        monkeypatch.setattr(tmux_launch_env, "stage_env", stage)
        monkeypatch.setattr(probe.control, "new_session", duplicate_then_spawn)
        result = await runner.run("synthetic prompt")
        assert result.exit_code == 1
        assert result.error.startswith("tmux new-session failed")
        assert not probe.report.exists(), "the rejected launch never started its provider"
        assert staged_paths and all(not path.exists() for path in staged_paths)
        scan_outputs(result.error)


async def test_cancel_consumed_receipt_keeps_child_alive(clean_daemon, tmp_path, monkeypatch):
    async with private_server(tmp_path, monkeypatch) as probe:
        spawned = await probe.control.new_session(
            cwd=str(tmp_path),
            command=shlex.quote(str(probe.script)),
            env={"CUSTOM_TOOL_TOKEN": SENTINEL + "daemon"},
        )
        assert spawned.ok and spawned.launch_env is not None
        report = await wait_report(probe.report)
        assert report["CUSTOM_TOOL_TOKEN"]["daemon"]
        path = receipt_path(clean_daemon, spawned.launch_env)
        assert not path.exists(), "loader consumed its payload"
        await _cleanup_launch_env(spawned.launch_env)
        await _cleanup_launch_env(spawned.launch_env)
        assert not path.exists()
        assert await probe.control.has_session(), "receipt revocation must not kill consumed child"


async def test_teardown_error_still_revokes_receipt(clean_daemon, monkeypatch):
    runner = runner_at(clean_daemon, monkeypatch)
    receipt, path = staged_receipt(clean_daemon)
    kills = 0

    async def tmux(*args, **kwargs):
        nonlocal kills
        if args[0] == "kill-session":
            kills += 1
            if kills == 2:
                raise RuntimeError(SENTINEL)
        return 0, ""

    monkeypatch.setattr(runner, "_tmux", tmux)
    monkeypatch.setattr(runner, "_wait_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(runner, "_ensure_submitted", AsyncMock())
    monkeypatch.setattr(runner, "_wait_for_result", AsyncMock(return_value="completed"))
    monkeypatch.setattr(
        runner._control,
        "new_session",
        AsyncMock(
            return_value=TmuxCommandResult(
                returncode=0,
                stdout="",
                stderr="",
                launch_env=receipt,
            )
        ),
    )
    with pytest.raises(RuntimeError):
        await runner.run("synthetic prompt")
    assert kills == 2
    payload_remains = path.exists()
    assert not payload_remains, "teardown failure retained private payload"
