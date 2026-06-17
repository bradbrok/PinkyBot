"""Integration wiring for the tmux app-server transport in CodexSession (#791).

Covers the flag gating (PINKY_CODEX_TMUX_APP_SERVER, layered on
PINKY_CODEX_APP_SERVER) and that _ensure_app_server routes through the
supervisor (vs the direct-subprocess spawn) without changing the downstream
initialize / stats contract.
"""

from __future__ import annotations

import pytest

from pinky_daemon.codex_app_server_tmux import CodexAppServerSupervisor
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.streaming_session import StreamingSessionConfig


def _make_session(**overrides) -> CodexSession:
    config = StreamingSessionConfig(
        agent_name="test-agent",
        label="main",
        model="",
        working_dir="/tmp",
        provider_url="codex_cli",
        provider_key="test-key",
        **overrides,
    )
    return CodexSession(config)


def test_tmux_flag_off_by_default(monkeypatch):
    monkeypatch.delenv("PINKY_CODEX_APP_SERVER", raising=False)
    monkeypatch.delenv("PINKY_CODEX_TMUX_APP_SERVER", raising=False)
    s = _make_session()
    assert s._use_tmux_app_server is False
    assert s._app_supervisor is None


def test_tmux_flag_requires_base_app_server_flag(monkeypatch):
    # The tmux flag is meaningless without the app-server path itself.
    monkeypatch.delenv("PINKY_CODEX_APP_SERVER", raising=False)
    monkeypatch.setenv("PINKY_CODEX_TMUX_APP_SERVER", "1")
    s = _make_session()
    assert s._use_tmux_app_server is False
    assert s._app_supervisor is None


def test_tmux_flag_on_creates_supervisor(monkeypatch):
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1")
    monkeypatch.setenv("PINKY_CODEX_TMUX_APP_SERVER", "1")
    s = _make_session()
    assert s._use_tmux_app_server is True
    assert isinstance(s._app_supervisor, CodexAppServerSupervisor)
    assert s._app_supervisor.session_name == "pinky-codex-as-test-agent"


class _FakeClient:
    def __init__(self) -> None:
        self.initialized = False

    def start(self) -> None:  # pragma: no cover - not called on this path
        pass

    async def initialize(self, *, name: str = "", version: str = "") -> dict:
        self.initialized = True
        return {"userAgent": "fake/1"}

    async def close(self) -> None:
        pass


class _FakeProc:
    def __init__(self) -> None:
        self.pid = 4321
        self.returncode = None

    def kill(self) -> None:
        pass

    async def wait(self) -> int:
        return -9


class _FakeSupervisor:
    def __init__(self) -> None:
        self.started = 0
        self.session_name = "pinky-codex-as-test-agent"
        self.sock_path = "/tmp/x/.codex-app-server/app.sock"
        self.client = _FakeClient()
        self.proc = _FakeProc()

    async def start(self, *, notification_handler=None, server_request_handler=None):
        self.started += 1
        return self.client, self.proc


@pytest.mark.asyncio
async def test_ensure_app_server_uses_supervisor_in_tmux_mode(monkeypatch):
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1")
    monkeypatch.setenv("PINKY_CODEX_TMUX_APP_SERVER", "1")
    s = _make_session()
    fake = _FakeSupervisor()
    s._app_supervisor = fake

    await s._ensure_app_server()

    assert fake.started == 1
    assert s._app_client is fake.client
    assert s._app_proc is fake.proc
    assert fake.client.initialized is True  # the single real gate still runs

    # Stats surface the tmux transport.
    stats = s.stats
    assert stats["app_server_mode"] == "tmux"
    assert stats["tmux_session"] == "pinky-codex-as-test-agent"
    assert stats["sock_path"] == fake.sock_path
    assert stats["child_pid"] == 4321


def test_stats_mode_subprocess_when_tmux_off(monkeypatch):
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "1")
    monkeypatch.delenv("PINKY_CODEX_TMUX_APP_SERVER", raising=False)
    s = _make_session()
    assert s.stats["app_server_mode"] == "subprocess"
