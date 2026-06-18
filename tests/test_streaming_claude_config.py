"""Per-agent Claude config-dir wiring for SDK streaming sessions."""

from __future__ import annotations

from pathlib import Path

from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig


class _Agent:
    def __init__(self, working_dir: str, isolation_mode: str = "local") -> None:
        self.working_dir = working_dir
        self.isolation_mode = isolation_mode


class _Registry:
    def __init__(self, agent: _Agent) -> None:
        self.agent = agent

    def get(self, name: str):
        return self.agent


def _session(tmp_path: Path, *, isolation_mode: str = "local") -> StreamingSession:
    work_dir = tmp_path / "agent"
    cfg = StreamingSessionConfig(agent_name="kuzya", working_dir=str(work_dir))
    return StreamingSession(
        cfg,
        registry=_Registry(_Agent(str(work_dir), isolation_mode=isolation_mode)),
    )


def test_streaming_per_agent_claude_env_flagged_local(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PINKY_PER_AGENT_CLAUDE_CONFIG", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-sdk")
    ss = _session(tmp_path)

    env = ss._per_agent_claude_env()

    assert env["CLAUDE_CONFIG_DIR"] == str(tmp_path / "agent" / ".claude-config")
    assert env["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-sdk"


def test_streaming_per_agent_claude_env_skips_without_token(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PINKY_PER_AGENT_CLAUDE_CONFIG", "1")
    monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
    ss = _session(tmp_path)

    assert ss._per_agent_claude_env() == {}


def test_streaming_per_agent_claude_env_skips_nonlocal_mode(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("PINKY_PER_AGENT_CLAUDE_CONFIG", "1")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-sdk")
    ss = _session(tmp_path, isolation_mode="unix_user")

    assert ss._per_agent_claude_env() == {}
