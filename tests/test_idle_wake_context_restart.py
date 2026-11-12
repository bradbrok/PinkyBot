"""Resume-handle identity and inbound delivery across a context restart."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_daemon import broker as broker_module
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.sessions import SessionManager
from pinky_daemon.streaming_session import StreamingSession, StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession
from pinky_daemon.transport_state import SessionState


class _RetainedSession:
    def __init__(self, state: SessionState, resume_handle: str = "") -> None:
        self.state = state
        self.resume_handle = resume_handle
        self.connect = AsyncMock()
        self.send = AsyncMock(return_value=True)


@pytest.fixture
def broker(tmp_path, monkeypatch):
    registry = AgentRegistry(db_path=str(tmp_path / "agents.db"))
    registry.register("test", working_dir=str(tmp_path))
    instance = MessageBroker(registry, SessionManager(), send_callback=AsyncMock())
    monkeypatch.setattr(broker_module, "_INBOUND_RECONNECT_WAIT_SEC", 0)
    try:
        yield instance
    finally:
        registry.close()


def _message() -> BrokerMessage:
    return BrokerMessage(
        platform="web", chat_id="", sender_name="", sender_id="",
        content="inbound after restart", agent_name="test",
    )


def _configure_context_restart(session) -> None:
    session._config.resume_handle = ""
    session._config.restart_reason = "context_restart"
    session._config.force_fresh_context_once = True
    session.resume_handle = ""


async def test_idle_sleep_with_empty_handle_uses_ensurer_once(broker) -> None:
    session = _RetainedSession(SessionState.IDLE_SLEEPING)
    broker.register_streaming("test", session)

    async def ensure_connected(name, *, label):
        session.state = SessionState.CONNECTED
        return session

    ensurer = AsyncMock(side_effect=ensure_connected)
    broker.set_ensure_session_callback(ensurer)

    delivered = await broker._route_streaming("test", _message())

    ensurer.assert_awaited_once_with("test", label="main")
    session.connect.assert_not_awaited()
    session.send.assert_awaited_once()
    assert "inbound after restart" in session.send.await_args.args[0]
    assert delivered is True
    assert broker._stats["errors"] == 0
    broker._send_callback.assert_not_awaited()


@pytest.mark.parametrize(
    ("session_type", "expected_handle"),
    [(TmuxSession, "pinky-test"), (CodexTmuxSession, "pinky-codex-test")],
)
def test_tmux_resume_handle_survives_context_restart(
    tmp_path, session_type, expected_handle,
) -> None:
    config = StreamingSessionConfig(agent_name="test", working_dir=str(tmp_path))
    session = session_type(config, tmux_control=MagicMock())
    assert session.resume_handle == expected_handle

    _configure_context_restart(session)

    assert session.resume_handle == expected_handle
    assert config.resume_handle == ""
    assert config.restart_reason == "context_restart"
    assert config.force_fresh_context_once is True


@pytest.mark.parametrize("blank_handle", ["", None])
def test_tmux_resume_handle_ignores_blanking(tmp_path, blank_handle) -> None:
    config = StreamingSessionConfig(agent_name="test", working_dir=str(tmp_path))
    session = TmuxSession(config, tmux_control=MagicMock())

    session.resume_handle = blank_handle

    assert session.resume_handle == "pinky-test"


@pytest.mark.parametrize("session_type", [StreamingSession, CodexSession])
def test_non_tmux_resume_handle_is_cleared_on_context_restart(tmp_path, session_type) -> None:
    config = StreamingSessionConfig(
        agent_name="test", working_dir=str(tmp_path), resume_handle="previous-context",
    )
    session = session_type(config)
    session.resume_handle = "previous-context"

    _configure_context_restart(session)

    assert session.resume_handle == ""
    assert config.resume_handle == ""


@pytest.mark.parametrize("state", [SessionState.RECONNECTING, SessionState.BOOTING])
async def test_empty_handle_during_connect_waits_without_ensurer(
    broker, monkeypatch, state,
) -> None:
    session = _RetainedSession(state)
    broker.register_streaming("test", session)
    ensurer = AsyncMock()
    broker.set_ensure_session_callback(ensurer)
    monkeypatch.setattr(broker_module, "_INBOUND_RECONNECT_WAIT_SEC", 1)
    monkeypatch.setattr(broker_module, "_INBOUND_RECONNECT_POLL_SEC", 0.001)

    async def finish_connect():
        await asyncio.sleep(0.01)
        session.state = SessionState.CONNECTED

    connection = asyncio.create_task(finish_connect())
    try:
        delivered = await broker._route_streaming("test", _message())
    finally:
        await connection

    ensurer.assert_not_awaited()
    session.connect.assert_not_awaited()
    session.send.assert_awaited_once()
    assert delivered is True
    broker._send_callback.assert_not_awaited()


@pytest.mark.parametrize("resume_handle", ["", "retained"])
async def test_isolation_guard_blocks_idle_wake_with_ensurer(
    broker, monkeypatch, resume_handle,
) -> None:
    session = _RetainedSession(SessionState.IDLE_SLEEPING, resume_handle)
    broker.register_streaming("test", session)
    ensurer = AsyncMock()
    broker.set_ensure_session_callback(ensurer)
    broker.set_isolation_guard(lambda name: (501, "isolation unavailable"))
    logs = []
    monkeypatch.setattr(broker_module, "_log", logs.append)

    delivered = await broker._route_streaming("test", _message())

    assert any("auto-wake blocked" in line and "isolation unavailable" in line for line in logs)
    ensurer.assert_not_awaited()
    session.connect.assert_not_awaited()
    session.send.assert_not_awaited()
    assert session.state == SessionState.IDLE_SLEEPING
    assert delivered is False


async def test_idle_sleep_without_handle_or_ensurer_does_not_connect(broker) -> None:
    session = _RetainedSession(SessionState.IDLE_SLEEPING)
    broker.register_streaming("test", session)

    delivered = await broker._route_streaming("test", _message())

    session.connect.assert_not_awaited()
    session.send.assert_not_awaited()
    assert delivered is False
