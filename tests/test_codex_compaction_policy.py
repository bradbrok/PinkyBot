"""The fleet compaction limit reaches every Codex launch path and home mode."""

import shlex
import tomllib
from unittest.mock import MagicMock

import pytest

from pinky_daemon.codex_home import (
    PER_AGENT_CODEX_HOME_ENV,
    _write_managed_config,
)
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.streaming_session import StreamingSessionConfig


def test_managed_config_writes_compaction_at_top_level(tmp_path):
    _write_managed_config(tmp_path, tmp_path / "agent", lambda _message: None)
    text = (tmp_path / "config.toml").read_text()
    config = tomllib.loads(text)

    assert config.get("model_auto_compact_token_limit") == 130000
    assert text.index("model_auto_compact_token_limit") < text.index("[features]")
    assert config["features"] == {"apps": False, "plugins": False}


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-luna"])
@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resume"])
@pytest.mark.parametrize("isolated_home", [False, True], ids=["shared", "isolated"])
def test_exec_compaction_override_in_all_home_modes(
    tmp_path, monkeypatch, model, resume, isolated_home
):
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1" if isolated_home else "0")
    session = CodexSession(StreamingSessionConfig(
        agent_name="compact-test", working_dir=str(tmp_path), model=model,
    ))
    if resume:
        session.codex_session_id = "existing-thread"

    command = session._build_codex_cmd()
    overrides = [command[i + 1] for i, part in enumerate(command[:-1]) if part == "-c"]

    assert overrides.count("model_auto_compact_token_limit=130000") == 1
    assert ("resume" in command) == resume
    assert command[-1] == "-"


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-luna"])
@pytest.mark.parametrize("resume", [False, True], ids=["fresh", "resume"])
@pytest.mark.parametrize("isolated_home", [False, True], ids=["shared", "isolated"])
def test_tmux_compaction_override_in_all_home_modes(
    tmp_path, monkeypatch, model, resume, isolated_home
):
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1" if isolated_home else "0")
    session = CodexTmuxSession(StreamingSessionConfig(
        agent_name="compact-test", working_dir=str(tmp_path), model=model,
    ), tmux_control=MagicMock())
    monkeypatch.setattr(session, "_has_prior_transcript", lambda: resume)

    command = shlex.split(session._build_claude_cmd())
    overrides = [command[i + 1] for i, part in enumerate(command[:-1]) if part == "-c"]

    assert overrides.count("model_auto_compact_token_limit=130000") == 1
    assert ("resume" in command) == resume


@pytest.mark.parametrize("model", ["gpt-6-astra", "gpt-5.6-luna"])
@pytest.mark.parametrize("with_mcp", [False, True], ids=["no-mcp", "mcp"])
@pytest.mark.parametrize("isolated_home", [False, True], ids=["shared", "isolated"])
def test_appserver_compaction_override_in_all_home_modes(
    tmp_path, monkeypatch, model, with_mcp, isolated_home
):
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1" if isolated_home else "0")
    session = CodexSession(StreamingSessionConfig(
        agent_name="compact-test", working_dir=str(tmp_path), model=model,
        mcp_servers={"pinky": {"url": "http://example.invalid/mcp"}} if with_mcp else {},
    ))

    config = session._appserver_config()

    assert config.get("model_auto_compact_token_limit") == 130000
    assert type(config["model_auto_compact_token_limit"]) is int
    if with_mcp:
        assert config["mcp_servers"] == {"pinky": {"url": "http://example.invalid/mcp"}}
    else:
        assert "mcp_servers" not in config
