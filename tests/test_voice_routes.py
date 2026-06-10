"""Tests for pinky_daemon.voice_routes.

Covers the ConversationRelay WS lifecycle guards (duplicate connections,
setup-validation rejections, ownership of the _active_ws registration) and
the terminate endpoint's failure reporting.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import pinky_daemon.voice_engine as voice_engine
import pinky_daemon.voice_routes as voice_routes


class FakeWS:
    """Minimal stand-in for a Starlette WebSocket."""

    def __init__(self, incoming: list[str] | None = None):
        self.incoming = list(incoming or [])
        self.sent: list[str] = []
        self.accepted = False
        self.closed: tuple[int, str] | None = None

    async def accept(self):
        self.accepted = True

    async def close(self, code: int = 1000, reason: str = ""):
        self.closed = (code, reason)

    async def receive_text(self) -> str:
        if self.incoming:
            return self.incoming.pop(0)
        raise RuntimeError("client disconnected")

    async def send_text(self, text: str):
        self.sent.append(text)


def _make_session(**overrides):
    session = MagicMock()
    session.id = overrides.get("id", "sess-1")
    session.status = overrides.get("status", "connecting")
    session.call_sid = overrides.get("call_sid", "CA_expected")
    session.call_request_id = overrides.get("call_request_id", None)
    session.to_number = overrides.get("to_number", "+15551234567")
    session.agent_name = overrides.get("agent_name", "barsik")
    session.max_duration_sec = overrides.get("max_duration_sec", 300)
    session.answered_at = overrides.get("answered_at", 0.0)
    session.started_at = overrides.get("started_at", 0.0)
    session.ended_at = overrides.get("ended_at", 0.0)
    return session


@pytest.fixture
def voice_deps():
    """Save/restore voice_routes module-level state around each test."""
    saved = (
        voice_routes._voice_store,
        voice_routes._agents,
        voice_routes._broker_send,
        voice_routes._base_url,
    )
    saved_ws = dict(voice_routes._active_ws)
    voice_routes._active_ws.clear()
    yield
    (
        voice_routes._voice_store,
        voice_routes._agents,
        voice_routes._broker_send,
        voice_routes._base_url,
    ) = saved
    voice_routes._active_ws.clear()
    voice_routes._active_ws.update(saved_ws)


# -- ConversationRelay WS guards ----------------------------------------------


async def test_ws_duplicate_connection_rejected(voice_deps):
    """A second WS for a live session must not clobber the first registration
    or stamp the session completed when it exits."""
    store = MagicMock()
    session = _make_session()
    store.get_session.return_value = session
    voice_routes.set_dependencies(voice_store=store, agents=None)

    first_ws = object()
    voice_routes._active_ws[session.id] = first_ws  # type: ignore[assignment]

    second = FakeWS()
    await voice_routes.conversationrelay_ws(second, session.id)

    assert second.closed is not None
    assert second.closed[0] == 4003
    # The live handler's registration is untouched.
    assert voice_routes._active_ws[session.id] is first_ws
    # The live session was not marked completed by the rejected connection.
    store.update_session.assert_not_called()


async def test_ws_callsid_mismatch_does_not_complete_session(voice_deps):
    """A setup-validation rejection must not stamp the session completed."""
    store = MagicMock()
    session = _make_session(call_sid="CA_expected")
    store.get_session.return_value = session
    voice_routes.set_dependencies(voice_store=store, agents=None)

    ws = FakeWS([
        json.dumps({
            "type": "setup",
            "sessionId": "CR1",
            "callSid": "CA_other",
            "accountSid": "",
        })
    ])
    await voice_routes.conversationrelay_ws(ws, session.id)

    assert ws.closed == (4003, "callSid mismatch")
    assert session.id not in voice_routes._active_ws
    store.update_session.assert_not_called()


async def test_ws_normal_disconnect_marks_completed(voice_deps):
    """The owning handler still marks the session completed on a real end."""
    store = MagicMock()
    session = _make_session()
    store.get_session.return_value = session
    voice_routes.set_dependencies(voice_store=store, agents=None)

    ws = FakeWS()  # immediate disconnect
    await voice_routes.conversationrelay_ws(ws, session.id)

    assert session.id not in voice_routes._active_ws
    completed_calls = [
        c for c in store.update_session.call_args_list
        if c.kwargs.get("status") == "completed"
    ]
    assert len(completed_calls) == 1
    assert completed_calls[0].kwargs.get("ended_at")


# -- terminate endpoint --------------------------------------------------------


def _make_client():
    app = FastAPI()
    app.include_router(voice_routes.router)
    return TestClient(app)


def test_terminate_reports_failure_when_both_paths_fail(voice_deps, monkeypatch):
    """No WS registered + Twilio REST failure must surface as an error, not
    a bogus {terminated: true} with the session stamped completed."""
    store = MagicMock()
    session = _make_session()
    store.get_session_by_call_sid.return_value = session
    voice_routes.set_dependencies(voice_store=store, agents=MagicMock())

    def boom(agents):
        raise RuntimeError("no twilio creds")

    monkeypatch.setattr(voice_engine, "get_twilio_client", boom)

    client = _make_client()
    resp = client.post(
        f"/api/voice/session/{session.call_sid}/terminate",
        json={"reason": "test"},
    )

    assert resp.status_code == 502
    store.update_session.assert_not_called()


def test_terminate_succeeds_via_rest(voice_deps, monkeypatch):
    store = MagicMock()
    session = _make_session()
    store.get_session_by_call_sid.return_value = session
    voice_routes.set_dependencies(voice_store=store, agents=MagicMock())

    monkeypatch.setattr(voice_engine, "get_twilio_client", lambda agents: MagicMock())

    client = _make_client()
    resp = client.post(
        f"/api/voice/session/{session.call_sid}/terminate",
        json={"reason": "test"},
    )

    assert resp.status_code == 200
    assert resp.json()["terminated"] is True
    store.update_session.assert_called_once()
    assert store.update_session.call_args.kwargs.get("status") == "completed"
