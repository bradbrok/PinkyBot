"""Non-empty launch values must cross the file boundary, never process argv."""

from __future__ import annotations

import os
import re
import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from pinky_daemon.codex_app_server_tmux import CodexAppServerSupervisor
from pinky_daemon.codex_tmux_session import CodexTmuxSession, _CodexTmuxControl
from pinky_daemon.command_runner import ContainerCommandRunner, RunuserCommandRunner
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession, _TmuxControl
from tests.tmux_env_support import (
    LaunchRecorder,
    child_payload,
    env_pairs,
    probe_command,
    run_pane,
    secret_files,
)

SENTINEL = "synthetic-launch-secret-71e9"


@pytest.fixture
def private_home(tmp_path):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    with patch.dict(os.environ, {"HOME": str(home), "PATH": os.defpath}, clear=True):
        yield home


def control(home, name="test-session", runner=None):
    recorder = runner or LaunchRecorder(home)
    return _TmuxControl(name, command_runner=recorder), recorder


async def stage(home, env=None, name="test-session", runner=None):
    tmux, recorder = control(home, name, runner)
    await tmux.new_session(
        cwd=str(home),
        command=probe_command(home),
        env=env if env is not None else {"UNKNOWN_CREDENTIAL": SENTINEL},
    )
    return tmux, recorder


def only_file(home):
    paths = secret_files(home, SENTINEL)
    assert len(paths) == 1, "the non-empty value must be staged in one protected source file"
    return paths[0]


def assert_clean_argv(recorder, secrets=(SENTINEL,)):
    for argv, _stdin in recorder.calls:
        for secret in secrets:
            assert secret not in "\n".join(argv), "non-empty launch value leaked to process argv"
    for argv in recorder.tmux_calls:
        assert all(value == "" for value in env_pairs(argv).values())


async def test_unknown_nonempty_value_uses_private_file_and_original_run_seam(private_home):
    tmux, recorder = control(private_home)
    original_run = tmux._run
    with patch.object(tmux, "_run", wraps=original_run) as run:
        await tmux.new_session(
            cwd=str(private_home),
            command=probe_command(private_home),
            env={"UNKNOWN_CREDENTIAL": SENTINEL, "PUBLIC_SETTING": "nonempty-option"},
        )
    assert_clean_argv(recorder, (SENTINEL, "nonempty-option"))
    assert any(call.args[0] == "new-session" for call in run.await_args_list)
    path = only_file(private_home)
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
    assert path.stat().st_uid == os.getuid()
    payload = child_payload(run_pane(recorder.tmux_calls[-1], private_home))
    assert payload["env"]["UNKNOWN_CREDENTIAL"] == SENTINEL
    assert payload["env"]["PUBLIC_SETTING"] == "nonempty-option"
    assert str(path) not in payload["files_at_exec"], "delete must precede exec"
    assert not path.exists()


@pytest.mark.parametrize("env", [None, {}, {"CLAUDE_CODE_OAUTH_TOKEN": ""}])
async def test_absent_or_empty_only_env_keeps_command_and_creates_no_file(private_home, env):
    tmux, recorder = control(private_home)
    command = probe_command(private_home)
    await tmux.new_session(cwd=str(private_home), command=command, env=env)
    assert recorder.tmux_calls[-1][-1] == command
    assert env_pairs(recorder.tmux_calls[-1]) == (env or {})
    assert not [p for p in private_home.rglob("*") if p.is_file()]


async def test_empty_shadow_overrides_inherited_token_alongside_file_values(private_home):
    _, recorder = await stage(
        private_home,
        {
            "CLAUDE_CODE_OAUTH_TOKEN": "",
            "UNKNOWN_CREDENTIAL": SENTINEL,
        },
    )
    assert env_pairs(recorder.tmux_calls[-1]) == {"CLAUDE_CODE_OAUTH_TOKEN": ""}
    assert_clean_argv(recorder)
    payload = child_payload(
        run_pane(
            recorder.tmux_calls[-1],
            private_home,
            {"CLAUDE_CODE_OAUTH_TOKEN": "synthetic-inherited-token"},
        )
    )
    assert payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == ""
    assert payload["env"]["UNKNOWN_CREDENTIAL"] == SENTINEL


@pytest.mark.parametrize("bad_key", ["", "1BAD", "BAD-NAME", "X;touch marker", "A\nB", "é", "A=B"])
async def test_invalid_key_refuses_before_any_side_effect(private_home, bad_key):
    tmux, recorder = control(private_home)
    with pytest.raises((ValueError, RuntimeError)) as error:
        await tmux.new_session(
            cwd=str(private_home),
            command="true",
            env={"VALID_FIRST": SENTINEL, bad_key: "invalid-key-value"},
        )
    assert SENTINEL not in str(error.value)
    assert "invalid-key-value" not in str(error.value)
    assert not recorder.calls
    assert not list(private_home.iterdir())


@pytest.mark.parametrize("bad_value", [None, 4, "embedded\x00nul"])
async def test_invalid_value_refuses_without_partial_file_or_launch(private_home, bad_value):
    tmux, recorder = control(private_home)
    with pytest.raises((TypeError, ValueError, RuntimeError)):
        await tmux.new_session(
            cwd=str(private_home),
            command="true",
            env={"VALID_FIRST": SENTINEL, "INVALID": bad_value},
        )
    assert not recorder.tmux_calls
    assert not secret_files(private_home, SENTINEL)


@pytest.mark.parametrize(
    "value",
    [
        "single'quote",
        'double"quote',
        "$HOME ${USER}",
        "`printf injected`",
        "spaces ; $(printf injected)",
        " leading and trailing ",
        "café-日本語",
    ],
)
async def test_real_shell_quoting_round_trip_without_evaluation(private_home, value):
    _, recorder = await stage(private_home, {"UNKNOWN_CREDENTIAL": SENTINEL, "QUOTED": value})
    assert_clean_argv(recorder, (SENTINEL, value))
    path = only_file(private_home)
    payload = child_payload(run_pane(recorder.tmux_calls[-1], private_home))
    assert payload["env"]["QUOTED"] == value
    assert not path.exists()
    expected_keys = {
        "HOME",
        "PATH",
        "PWD",
        "SHLVL",
        "_",
        "UNKNOWN_CREDENTIAL",
        "QUOTED",
        "LC_CTYPE",
    }
    assert not set(payload["env"]) - expected_keys, (
        "integrity bookkeeping must not leak to child env"
    )


@pytest.mark.parametrize(
    "corruption", ["missing", "empty", "truncated", "syntax", "return_failure"]
)
async def test_source_failure_never_executes_and_removes_file(private_home, corruption):
    _, recorder = await stage(private_home)
    path = only_file(private_home)
    original = path.read_text()
    if corruption == "missing":
        path.unlink()
    elif corruption == "empty":
        path.write_text("")
    elif corruption == "truncated":
        # A syntactically valid assignment prefix cannot count as a complete file.
        path.write_text("UNKNOWN_CREDENTIAL='synthetic-partial'\n")
    elif corruption == "syntax":
        path.write_text("UNKNOWN_CREDENTIAL='unterminated\n")
    else:
        path.write_text(original + "\nreturn 19\n")
    result = run_pane(recorder.tmux_calls[-1], private_home)
    assert result.returncode != 0
    assert not result.stdout, "the target process ran after a failed source"
    assert result.stderr, "source refusal must be visible"
    assert SENTINEL.encode() not in result.stderr
    assert not path.exists(), "shell special-builtin failure must still clean up"


async def test_delete_failure_refuses_exec(private_home):
    _, recorder = await stage(private_home)
    path = only_file(private_home)
    # A nonempty directory at the staged pathname deterministically defeats rm -f.
    # This also exercises refusal on an invalid source object without root-specific chmod behavior.
    path.unlink()
    path.mkdir()
    (path / "keep").write_text("synthetic")
    result = run_pane(recorder.tmux_calls[-1], private_home)
    assert result.returncode != 0
    assert not result.stdout
    assert result.stderr


async def test_cleanup_does_not_depend_on_forwarded_path(private_home):
    _, recorder = await stage(
        private_home, {"UNKNOWN_CREDENTIAL": SENTINEL, "PATH": "/nonexistent"}
    )
    path = only_file(private_home)
    payload = child_payload(run_pane(recorder.tmux_calls[-1], private_home))
    assert payload["env"]["PATH"] == "/nonexistent"
    assert not path.exists()


async def test_write_failure_raises_without_falling_back_to_argv(private_home, monkeypatch):
    tmux, recorder = control(private_home)
    recorder.fail_staging = True
    real_open = os.open

    def deny_create(path, flags, mode=0o777, **kwargs):
        if flags & os.O_CREAT:
            raise PermissionError("synthetic-write-refusal")
        return real_open(path, flags, mode, **kwargs)

    monkeypatch.setattr(os, "open", deny_create)
    with pytest.raises((OSError, RuntimeError)):
        await tmux.new_session(cwd=str(private_home), command="true", env={"SECRET": SENTINEL})
    assert not recorder.tmux_calls
    assert not secret_files(private_home, SENTINEL)
    assert_clean_argv(recorder)


async def test_sequential_relaunch_prunes_only_its_session_and_never_reuses_path(private_home):
    tmux, recorder = await stage(private_home, name="session-one")
    old = only_file(private_home)
    other, other_recorder = await stage(
        private_home,
        {"SECRET": "different-session-value"},
        name="session-two",
    )
    other_file = secret_files(private_home, "different-session-value")[0]
    # Simulate the callers' already-completed stale-session teardown; neither pane sourced.
    await tmux.new_session(
        cwd=str(private_home), command=probe_command(private_home), env={"SECRET": SENTINEL}
    )
    fresh = only_file(private_home)
    assert fresh != old and not old.exists()
    assert other_file.exists()
    assert (
        child_payload(run_pane(recorder.tmux_calls[-1], private_home))["env"]["SECRET"] == SENTINEL
    )
    assert (
        child_payload(run_pane(other_recorder.tmux_calls[-1], private_home))["env"]["SECRET"]
        == "different-session-value"
    )


@pytest.mark.parametrize("kind", ["container", "runuser"])
async def test_wrapped_runner_stages_in_target_home_and_host_argv_is_clean(
    private_home, tmp_path, monkeypatch, kind
):
    target = tmp_path / "target-home"
    target.mkdir(mode=0o700)
    inner = LaunchRecorder(target)
    runner = (
        ContainerCommandRunner("test-container", inner=inner)
        if kind == "container"
        else RunuserCommandRunner("test-user", inner=inner)
    )
    tmux = _TmuxControl("wrapped-session", command_runner=runner)
    session = claude_session(target, monkeypatch, isolated=True)
    if kind == "container":
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        session._registry.get.return_value.isolation_mode = "container"
    env = {**session._build_repl_env(), "UNKNOWN_CREDENTIAL": SENTINEL}
    assert "PINKY_SESSION_SECRET" not in env
    await tmux.new_session(
        cwd=str(target),
        command=probe_command(target),
        env=env,
    )
    assert_clean_argv(
        inner, (SENTINEL, "synthetic-agent-signing-key", "synthetic-global-signing-secret")
    )
    assert not secret_files(private_home, SENTINEL), (
        "host filesystem must not stage target credentials"
    )
    path = only_file(target)
    staging = [(argv, stdin) for argv, stdin in inner.calls if stdin and SENTINEL.encode() in stdin]
    assert staging, "target staging must receive the value on stdin"
    assert all(
        argv[0] == ("podman" if kind == "container" else "runuser") for argv, _ in inner.calls
    )
    if kind == "container":
        assert all("-i" in argv[:4] for argv, _ in staging)
    payload = child_payload(run_pane(inner.tmux_calls[-1], target))
    assert payload["env"]["UNKNOWN_CREDENTIAL"] == SENTINEL
    assert payload["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == SENTINEL
    assert payload["env"]["PINKY_AGENT_KEY"] == "synthetic-agent-signing-key"
    assert "PINKY_SESSION_SECRET" not in payload["env"]
    assert {key: payload["env"][key] for key in env} == env
    assert not path.exists()


def claude_session(home, monkeypatch, *, isolated=False, dedicated=False):
    monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SENTINEL)
    monkeypatch.setenv("PINKY_SESSION_SECRET", "synthetic-global-signing-secret")
    agent = SimpleNamespace(
        name="test-agent",
        isolated=isolated,
        isolation_mode="local",
        dedicated_config_dir=dedicated,
        tool_policy_enabled=False,
    )
    registry = MagicMock()
    registry.get.return_value = agent
    registry.get_signing_key.return_value = "synthetic-agent-signing-key"
    config = StreamingSessionConfig(agent_name="test-agent", working_dir=str(home))
    session = TmuxSession(config, tmux_control=MagicMock())
    session._registry = registry
    return session


@pytest.mark.parametrize("isolated,dedicated", [(False, False), (True, False), (False, True)])
async def test_claude_builder_preserves_withholding_markers_and_empty_shadow(
    private_home, monkeypatch, isolated, dedicated
):
    session = claude_session(private_home, monkeypatch, isolated=isolated, dedicated=dedicated)
    env = session._build_repl_env()
    _, recorder = await stage(private_home, env)
    assert_clean_argv(
        recorder, (SENTINEL, "synthetic-global-signing-secret", "synthetic-agent-signing-key")
    )
    if dedicated:
        assert env_pairs(recorder.tmux_calls[-1])["CLAUDE_CODE_OAUTH_TOKEN"] == ""
        assert not secret_files(private_home, SENTINEL)
    if isolated:
        assert "PINKY_SESSION_SECRET" not in env
        assert not secret_files(private_home, "synthetic-global-signing-secret")
    payload = child_payload(run_pane(recorder.tmux_calls[-1], private_home))
    assert {key: payload["env"][key] for key in env} == env
    assert payload["env"]["PINKY_TMUX_TRANSCRIPT_BIND"] == "1"
    assert payload["env"]["PINKY_AGENT_NAME"] == "test-agent"


@pytest.mark.parametrize("transport", ["repl", "app_server"])
async def test_codex_full_env_parity_and_unknown_secret_off_argv(
    private_home, monkeypatch, transport
):
    monkeypatch.setenv("FAKE_SECRET_XYZ", SENTINEL)
    monkeypatch.setenv("EXPLICIT_EMPTY", "")
    monkeypatch.setenv("MULTILINE", "do\nnot-copy")
    monkeypatch.setenv("TMUX", "do-not-copy")
    monkeypatch.setenv("OPENAI_API_KEY", "synthetic-old-provider-key")
    if transport == "repl":
        session = CodexTmuxSession(
            StreamingSessionConfig(
                agent_name="test-agent",
                working_dir=str(private_home),
                provider_key="synthetic-provider-key",
            ),
            tmux_control=MagicMock(),
        )
        env = session._build_repl_env()
        recorder = LaunchRecorder(private_home)
        tmux = _CodexTmuxControl("codex-repl-test", command_runner=recorder)
    else:
        # Force a short isolated socket directory without touching the real home.
        with patch.object(
            CodexAppServerSupervisor, "_resolve_sock_dir", return_value=(str(private_home), False)
        ):
            supervisor = CodexAppServerSupervisor(
                "test-agent",
                working_dir=str(private_home),
                openai_api_key="synthetic-provider-key",
            )
        env = supervisor._build_env()
        recorder = LaunchRecorder(private_home)
        tmux = _TmuxControl(supervisor.session_name, command_runner=recorder)
    await tmux.new_session(cwd=str(private_home), command=probe_command(private_home), env=env)
    assert_clean_argv(recorder, (SENTINEL, "synthetic-provider-key"))
    assert env_pairs(recorder.tmux_calls[-1]) == {
        key: value for key, value in env.items() if value == ""
    }
    assert "MULTILINE" not in env and "TMUX" not in env
    path = only_file(private_home)
    payload = child_payload(run_pane(recorder.tmux_calls[-1], private_home))
    assert {key: payload["env"][key] for key in env} == env
    assert payload["env"]["OPENAI_API_KEY"] == "synthetic-provider-key"
    assert payload["env"]["FAKE_SECRET_XYZ"] == SENTINEL
    assert not path.exists()


async def test_source_success_but_delete_failure_must_not_exec(private_home):
    _, recorder = await stage(private_home)
    path = only_file(private_home)
    argv = list(recorder.tmux_calls[-1])
    argv[-1], substitutions = re.subn(r"/(?:usr/)?bin/rm\b", "/bin/false", argv[-1])
    assert substitutions, "cleanup utility must be absolute"
    result = run_pane(argv, private_home)
    assert result.returncode != 0
    assert not result.stdout, "delete failure must not launch the target with a file left behind"
    assert result.stderr
    assert path.exists()


async def test_partial_local_write_failure_cleans_file_and_never_launches(
    private_home, monkeypatch
):
    tmux, recorder = control(private_home)
    recorder.fail_staging = True

    def fail_fdopen(fd, *args, **kwargs):
        os.close(fd)
        raise OSError("synthetic-write-failure")

    monkeypatch.setattr(os, "fdopen", fail_fdopen)
    with pytest.raises((OSError, RuntimeError)):
        await tmux.new_session(cwd=str(private_home), command="true", env={"SECRET": SENTINEL})
    assert not recorder.tmux_calls
    assert not [p for p in private_home.rglob("*") if p.is_file()]
    assert_clean_argv(recorder)


async def test_app_server_start_routes_actual_launch_through_file_boundary(
    private_home, monkeypatch
):
    monkeypatch.setenv("FAKE_SECRET_XYZ", SENTINEL)
    with patch.object(
        CodexAppServerSupervisor, "_resolve_sock_dir", return_value=(str(private_home), False)
    ):
        supervisor = CodexAppServerSupervisor("test-agent", working_dir=str(private_home))
    recorder = LaunchRecorder(private_home)
    supervisor._tmux = _TmuxControl(supervisor.session_name, command_runner=recorder)
    supervisor._kill_tmux_session = AsyncMock()

    class LaunchObservedError(Exception):
        pass

    supervisor._await_accept = AsyncMock(side_effect=LaunchObservedError)
    with pytest.raises(LaunchObservedError):
        await supervisor.start()
    assert_clean_argv(recorder)
    assert len(recorder.tmux_calls) == 1
    assert "pinky_daemon.codex_app_server_shim" in recorder.tmux_calls[0][-1]
    assert SENTINEL in only_file(private_home).read_text()
    supervisor._kill_tmux_session.assert_awaited_once_with(strict=True)


async def test_provider_values_off_argv_and_preserved_in_child(private_home, monkeypatch):
    session = claude_session(private_home, monkeypatch)
    session._config.provider_key = "synthetic-provider-value"
    env = session._build_repl_env()
    _, recorder = await stage(private_home, env)
    assert_clean_argv(recorder, ("synthetic-provider-value", "synthetic-agent-signing-key"))
    payload = child_payload(run_pane(recorder.tmux_calls[-1], private_home))
    assert payload["env"]["ANTHROPIC_API_KEY"] == "synthetic-provider-value"
    assert payload["env"]["ANTHROPIC_AUTH_TOKEN"] == "synthetic-provider-value"
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in env


@pytest.mark.parametrize("kind", ["container", "runuser"])
async def test_remote_stage_failure_is_fatal_without_tmux_launch(private_home, kind):
    inner = LaunchRecorder(private_home)
    inner.fail_staging = True
    runner = (
        ContainerCommandRunner("test-container", inner=inner)
        if kind == "container"
        else RunuserCommandRunner("test-user", inner=inner)
    )
    tmux = _TmuxControl("test-session", command_runner=runner)
    with pytest.raises((OSError, RuntimeError)):
        await tmux.new_session(cwd=str(private_home), command="true", env={"SECRET": SENTINEL})
    assert not inner.tmux_calls
    assert not secret_files(private_home, SENTINEL)
    assert_clean_argv(inner)


@pytest.mark.parametrize("unsafe", ["symlink", "world_readable"])
async def test_unsafe_staging_directory_is_rejected(private_home, tmp_path, unsafe):
    root = private_home / ".local/state/pinkybot/tmux-launch-env"
    root.parent.mkdir(parents=True, mode=0o700)
    if unsafe == "symlink":
        outside = tmp_path / "outside"
        outside.mkdir(mode=0o700)
        root.symlink_to(outside, target_is_directory=True)
    else:
        root.mkdir(mode=0o755)
    tmux, recorder = control(private_home)
    with pytest.raises((OSError, RuntimeError, ValueError)):
        await tmux.new_session(cwd=str(private_home), command="true", env={"SECRET": SENTINEL})
    assert not recorder.tmux_calls
    assert not secret_files(tmp_path, SENTINEL)
