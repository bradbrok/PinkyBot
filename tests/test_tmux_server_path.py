"""Constructed base PATH and tmux-specific Codex payload normalization."""

import os
from types import SimpleNamespace

import pytest

from pinky_daemon import codex_launch_env
from pinky_daemon.command_runner import CommandResult, LocalCommandRunner
from pinky_daemon.isolated_launch_env import LaunchConfigError, LaunchPolicy
from tests.tmux_server_env_support import STANDARD_DIRS, control, owner, seed


@pytest.fixture
def daemon(tmp_path, monkeypatch):
    seed(monkeypatch, tmp_path)
    return tmp_path


async def client_env(root, monkeypatch):
    captured = {}

    async def run(self, argv, **kwargs):
        captured.update(kwargs)
        return CommandResult(0, b"", b"")

    monkeypatch.setattr(LocalCommandRunner, "run", run)
    await control(owner("claude", root))._run("has-session")
    assert isinstance(captured.get("env"), dict), "constructed client environment absent"
    return captured["env"]


async def test_default_preserves_daemon_order_appends_missing_standard_dirs(daemon, monkeypatch):
    monkeypatch.setenv("PATH", "/synthetic/tools:/bin:/usr/bin:/bin")
    env = await client_env(daemon, monkeypatch)
    assert env["PATH"].split(":") == list(dict.fromkeys(["/synthetic/tools", "/bin", "/usr/bin", *STANDARD_DIRS]))


async def test_explicit_path_preserves_order_and_accepts_nonexistent_dirs(daemon, monkeypatch):
    monkeypatch.setenv("PINKY_TMUX_PANE_PATH", "/synthetic/first:/synthetic/second")
    env = await client_env(daemon, monkeypatch)
    assert env["PATH"] == "/synthetic/first:/synthetic/second"


@pytest.mark.parametrize("path", ["", ":/bin", "/bin:", "/bin::/usr/bin", ".:/bin", "relative:/bin", "~/bin:/bin"])
async def test_bad_explicit_path_refuses(daemon, monkeypatch, path):
    monkeypatch.setenv("PINKY_TMUX_PANE_PATH", path)
    with pytest.raises(LaunchConfigError):
        await client_env(daemon, monkeypatch)


@pytest.mark.parametrize("kind", ["codex", "app_server"])
@pytest.mark.parametrize("clean", [True, False])
def test_codex_tmux_payload_uses_same_candidate(daemon, monkeypatch, kind, clean):
    monkeypatch.setenv("PINKY_TMUX_PANE_PATH", "/synthetic/first:/usr/bin:/bin")
    obj = owner(kind, daemon)
    builder = obj._build_env if kind == "app_server" else obj._build_repl_env
    env = builder(launch_policy=LaunchPolicy("enforce", "isolated") if clean else LaunchPolicy())
    matches = env["PATH"] == "/synthetic/first:/usr/bin:/bin"
    assert matches, "Codex tmux payload replaces candidate PATH with daemon PATH"


def test_direct_codex_keeps_existing_path_behavior(daemon, monkeypatch):
    monkeypatch.setenv("PINKY_TMUX_PANE_PATH", "/synthetic/tmux-only")
    env = codex_launch_env.build_env(agent_name="test-agent", config=None, api_key="", policy=LaunchPolicy(), log=lambda _: None)
    assert env["PATH"] == "/usr/bin:/bin"


async def test_missing_base_uses_simple_platform_fallbacks(daemon, monkeypatch):
    for name in ("HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "PATH"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr("pwd.getpwuid", lambda _: SimpleNamespace(pw_dir=str(daemon / "account"), pw_name="synthetic-user", pw_shell="/ignore"))
    env = await client_env(daemon, monkeypatch)
    assert env["HOME"] == str(daemon / "account")
    assert env["USER"] == env["LOGNAME"] == "synthetic-user"
    assert env["SHELL"] == "/bin/sh" and env["TERM"] == "xterm-256color"
    assert env["LANG"] == ("en_US.UTF-8" if os.sys.platform == "darwin" else "C.UTF-8")
    assert env["PATH"].split(":") == STANDARD_DIRS


async def test_present_base_values_come_from_daemon(daemon, monkeypatch):
    expected = {"USER": "synthetic-user", "LOGNAME": "synthetic-user", "SHELL": "/bin/sh",
                "TERM": "synthetic-term", "LANG": "synthetic.UTF-8", "LC_TIME": "C",
                "TZ": "UTC", "XDG_CONFIG_HOME": str(daemon / "xdg"),
                "HTTPS_PROXY": "http://synthetic.invalid", "SSL_CERT_FILE": str(daemon / "cert")}
    for name, value in expected.items():
        monkeypatch.setenv(name, value)
    env = await client_env(daemon, monkeypatch)
    assert all(env.get(k) == v for k, v in expected.items()), "base provenance differs"
