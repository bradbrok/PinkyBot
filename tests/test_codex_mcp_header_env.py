"""MCP header values cross private launch channels, never argv or launch logs."""

import asyncio
import json
import os
import re
import shlex
import stat
import sys
import tomllib
from unittest.mock import AsyncMock, Mock

import pytest

from pinky_daemon import codex_session, codex_tmux_session, isolated_launch_env, tmux_session
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.command_runner import RunuserCommandRunner
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import _TmuxControl
from tests.tmux_env_support import LaunchRecorder, child_payload, run_pane, secret_files

TOKEN = "SENTINEL-TOKEN-7f3a-only-for-tests"
OTHER_TOKEN = "SENTINEL-OTHER-AGENT-8d42"
PREFIX = "PINKY_MCP_HDR_"
SERVERS = {
    "api-one": {
        "url": "https://one.example.test/mcp",
        "headers": {"Authorization": f"Bearer {TOKEN}", "X-Agent-Name": "sample-identity"},
    },
    "api_two": {
        "url": "https://two.example.test/mcp",
        "headers": {"Authorization": f"Bearer second-{TOKEN}", "X.Trace-ID": 'quoted "value" \\ path'},
    },
    "stdio": {"command": "unused", "headers": {"Authorization": "unused-value"}},
}
EXPECTED = {
    "PINKY_MCP_HDR_API_ONE_AUTHORIZATION": f"Bearer {TOKEN}",
    "PINKY_MCP_HDR_API_ONE_X_AGENT_NAME": "sample-identity",
    "PINKY_MCP_HDR_API_TWO_AUTHORIZATION": f"Bearer second-{TOKEN}",
    "PINKY_MCP_HDR_API_TWO_X_TRACE_ID": 'quoted "value" \\ path',
}


@pytest.fixture
def harness(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PINKY_CODEX_PER_AGENT_HOME", "0")
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "0")
    logs = []
    for mod in (codex_session, codex_tmux_session, tmux_session):
        monkeypatch.setattr(mod, "_log", logs.append)
    return home, logs


def session(kind, home, monkeypatch, *, resume=False, servers=None):
    config = StreamingSessionConfig(
        agent_name="test-agent", working_dir=str(home), model="test-model",
        thinking_effort="high", provider_key="", mcp_servers=SERVERS if servers is None else servers,
    )
    if kind == "exec":
        result = CodexSession(config)
        result.codex_session_id = "test-session-id" if resume else None
        return result
    result = CodexTmuxSession(config)
    monkeypatch.setattr(result, "_has_prior_transcript", lambda: resume)
    return result


def command(s):
    return s._build_codex_cmd() if isinstance(s, CodexSession) else shlex.split(s._build_claude_cmd())


def environment(s, *, clean=False):
    if isinstance(s, CodexSession):
        return s._build_codex_env()
    policy = isolated_launch_env.LaunchPolicy(
        mode="enforce" if clean else "off", status="isolated", agent_key="test-scoped-key",
    )
    return s._build_repl_env(launch_policy=policy)


def assert_header_config(argv, env):
    entries = {}
    for i, arg in enumerate(argv):
        if i and argv[i - 1] == "-c" and arg.startswith("mcp_servers."):
            for name, fields in tomllib.loads(arg)["mcp_servers"].items():
                dest = entries.setdefault(name, {})
                for key, value in fields.items():
                    if isinstance(value, dict):
                        dest.setdefault(key, {}).update(value)
                    else:
                        dest[key] = value
    assert set(entries) == {"api-one", "api_two"}
    for name, cfg in entries.items():
        assert "http_headers" not in cfg
        assert cfg["url"] == SERVERS[name]["url"]
        refs = cfg["env_http_headers"]
        assert set(refs) == set(SERVERS[name]["headers"])
        for header, env_name in refs.items():
            assert re.fullmatch(r"[A-Z_][A-Z0-9_]*", env_name)
            assert env[env_name] == SERVERS[name]["headers"][header]
    assert {k: v for k, v in env.items() if k.startswith(PREFIX)} == EXPECTED


@pytest.mark.parametrize("kind", ["tmux", "exec"])
@pytest.mark.parametrize("resume", [False, True])
def test_command_references_header_env_without_values(harness, monkeypatch, kind, resume):
    home, logs = harness
    s = session(kind, home, monkeypatch, resume=resume)
    argv = command(s)
    assert TOKEN not in " ".join(argv)
    assert "sample-identity" not in " ".join(argv)
    assert_header_config(argv, environment(s))
    assert ("resume" in argv) is resume
    assert ("-C" in argv) is not resume
    assert 'model_reasoning_effort="high"' in argv
    assert TOKEN not in "\n".join(logs)


@pytest.mark.parametrize("kind", ["tmux", "exec"])
def test_environment_delivers_only_current_config_headers(harness, monkeypatch, kind):
    home, _ = harness
    monkeypatch.setenv(PREFIX + "OTHER_AUTHORIZATION", OTHER_TOKEN)
    monkeypatch.setenv(PREFIX + "API_ONE_AUTHORIZATION", OTHER_TOKEN)
    s = session(kind, home, monkeypatch)
    assert {k: v for k, v in environment(s).items() if k.startswith(PREFIX)} == EXPECTED


def test_isolated_headers_are_added_after_scoping_and_grants(harness, monkeypatch):
    home, logs = harness
    other = PREFIX + "OTHER_AUTHORIZATION"
    monkeypatch.setenv(other, OTHER_TOKEN)
    monkeypatch.setenv(PREFIX + "API_ONE_AUTHORIZATION", OTHER_TOKEN)
    s = session("tmux", home, monkeypatch)
    scoped = Mock(wraps=isolated_launch_env.scoped_codex_env)
    monkeypatch.setattr(isolated_launch_env, "scoped_codex_env", scoped)
    policy = isolated_launch_env.LaunchPolicy(
        mode="enforce", status="isolated", agent_key="test-scoped-key", grants=(other,),
    )
    env = s._build_repl_env(launch_policy=policy)
    scoped.assert_called_once_with(policy, "test-agent")
    assert {k: v for k, v in env.items() if k.startswith(PREFIX)} == EXPECTED
    assert env["PINKY_AGENT_KEY"] == "test-scoped-key"
    assert OTHER_TOKEN not in env.values()
    assert TOKEN not in "\n".join(logs)
    assert OTHER_TOKEN not in "\n".join(logs)


@pytest.mark.parametrize("kind", ["tmux", "exec"])
@pytest.mark.parametrize("build", ["command", "environment"])
@pytest.mark.parametrize("pairs", [
    [("api-one", "Authorization"), ("api_one", "Authorization")],
    [("api", "X-Trace"), ("api", "X_Trace")],
    [("api", "Authorization"), ("api", "authorization")],
    [("api_one", "X"), ("api", "one_X")],
])
def test_sanitized_header_name_collision_refuses_without_values(
    harness, monkeypatch, kind, build, pairs,
):
    home, logs = harness
    servers = {}
    for name, header in pairs:
        cfg = servers.setdefault(name, {"url": "https://example.test/mcp", "headers": {}})
        cfg["headers"][header] = TOKEN
    s = session(kind, home, monkeypatch, servers=servers)
    with pytest.raises(ValueError, match="MCP header environment name collision") as caught:
        command(s) if build == "command" else environment(s)
    assert TOKEN not in str(caught.value)
    assert TOKEN not in "\n".join(logs)


@pytest.mark.parametrize("resume", [False, True])
async def test_exec_spawn_receives_private_env_and_logs_no_header_values(harness, monkeypatch, resume):
    home, logs = harness
    s = session("exec", home, monkeypatch, resume=resume)
    proc = Mock(returncode=0)
    proc.stdin = Mock(drain=AsyncMock(), wait_closed=AsyncMock())
    proc.stdout = asyncio.StreamReader()
    proc.stdout.feed_eof()
    proc.stderr = asyncio.StreamReader()
    proc.stderr.feed_eof()
    proc.wait = AsyncMock(return_value=0)
    spawn = AsyncMock(return_value=proc)
    monkeypatch.setattr(codex_session.asyncio, "create_subprocess_exec", spawn)
    result = await s._exec_codex("test prompt")
    assert not result.failed
    argv = spawn.call_args.args
    assert TOKEN not in " ".join(argv)
    assert_header_config(argv, spawn.call_args.kwargs["env"])
    proc.stdin.write.assert_called_once_with(b"test prompt")
    assert any(": exec " in line for line in logs), "assert logs from the real exec launch path"
    assert TOKEN not in "\n".join(logs)


@pytest.mark.parametrize("remote", [False, True])
@pytest.mark.parametrize("clean", [False, True])
@pytest.mark.parametrize("resume", [False, True])
async def test_tmux_stages_headers_privately_then_shell_delivers_and_unlinks(
    harness, monkeypatch, remote, clean, resume,
):
    home, logs = harness
    binary_dir = home / "bin"
    binary_dir.mkdir()
    # Substitute only the provider executable; execute the real staging helper and shell loader.
    fake = binary_dir / "codex"
    fake.write_text(
        f"#!{sys.executable}\nimport json,os,sys\n"
        "print(json.dumps({'argv':sys.argv,'env':dict(os.environ)}))\n"
    )
    fake.chmod(0o700)
    monkeypatch.setenv("PATH", str(binary_dir) + os.pathsep + os.environ["PATH"])
    monkeypatch.setenv(PREFIX + "OTHER_AUTHORIZATION", OTHER_TOKEN)
    s = session("tmux", home, monkeypatch, resume=resume)
    recorder = LaunchRecorder(home)
    runner = RunuserCommandRunner("test", inner=recorder) if remote else recorder
    s._tmux = _TmuxControl("header-env-test", command_runner=runner)
    policy = isolated_launch_env.LaunchPolicy(
        mode="enforce" if clean else "off", status="isolated", agent_key="test-scoped-key",
    )
    monkeypatch.setattr(s, "_launch_env_policy", lambda: policy)
    monkeypatch.setattr(s, "_select_command_runner", lambda *_: runner)
    monkeypatch.setattr(s, "_prepare_tmux_spawn", Mock())
    monkeypatch.setattr(s, "_start_tailer", AsyncMock())
    monkeypatch.setattr(s, "_codex_dismiss_nux_and_ready", AsyncMock())
    monkeypatch.setattr(s._tmux, "has_session", AsyncMock(side_effect=[False, True]))
    monkeypatch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0)
    monkeypatch.setattr(tmux_session, "_seed_claude_trust_file", lambda *_: False)
    await s._spawn_tmux_repl()
    assert len(recorder.tmux_calls) == 1
    for argv, _ in recorder.calls:
        assert TOKEN not in " ".join(argv), "neither tmux nor namespace helper argv carries a token"
    files = secret_files(home, TOKEN)
    assert len(files) == 1
    staged = files[0]
    assert stat.S_IMODE(staged.stat().st_mode) == 0o600
    assert_header_config(command(s), json.loads(staged.read_text())["env"])
    if remote:
        requests = [json.loads(data) for _, data in recorder.calls if data]
        assert requests[0]["env"][PREFIX + "API_ONE_AUTHORIZATION"] == f"Bearer {TOKEN}"
    pane = run_pane(recorder.tmux_calls[-1], home, inherited={
        "PATH": str(binary_dir) + os.pathsep + os.environ["PATH"],
        **({PREFIX + "OTHER_AUTHORIZATION": OTHER_TOKEN} if clean else {}),
    })
    payload = child_payload(pane)
    assert TOKEN not in " ".join(payload["argv"])
    assert_header_config(payload["argv"], payload["env"])
    assert not staged.exists(), "the loader removes the private payload before provider exec"
    assert not secret_files(home, TOKEN)
    assert any("codex_cmd_built" in line for line in logs)
    assert TOKEN not in "\n".join(logs)
    assert TOKEN not in pane.stderr.decode()


@pytest.mark.parametrize("remote", [False, True])
async def test_failed_tmux_launch_removes_staged_headers(harness, monkeypatch, remote):
    from pinky_daemon.command_runner import CommandResult

    home, logs = harness

    class RefusingRunner(LaunchRecorder):
        async def run(self, argv, **kwargs):
            if "new-session" in argv:
                assert secret_files(home, TOKEN), "refuse only after real staging"
                assert TOKEN not in " ".join(argv)
                return CommandResult(1, b"", b"synthetic launch refusal")
            return await super().run(argv, **kwargs)

    recorder = RefusingRunner(home)
    runner = RunuserCommandRunner("test", inner=recorder) if remote else recorder
    s = session("tmux", home, monkeypatch)
    control = _TmuxControl("header-env-failure", command_runner=runner)
    result = await control.new_session(
        cwd=str(home), command=s._build_claude_cmd(), env=environment(s),
    )
    assert not result.ok
    assert not secret_files(home, TOKEN)
    assert TOKEN not in "\n".join(logs)
