"""Signed, self-scoped read of one routed inbound message context."""

from __future__ import annotations

import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.auth import (
    SESSION_COOKIE_NAME,
    build_internal_auth_headers,
    create_session_cookie,
)
from pinky_daemon.broker import BrokerMessage

pytestmark = pytest.mark.real_auth
SECRET = "message-context-api-test-secret-not-for-runtime"
PATH = "/agents/sample/message-context/slack/D1/m1"


@contextmanager
def _gateway(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_SESSION_SECRET", SECRET)
    app = create_api(db_path=str(tmp_path / "memory.db"), default_working_dir=str(tmp_path))
    agents = app.state.agents
    agents.register("sample", working_dir=str(tmp_path / "sample"))
    agents.register("other", working_dir=str(tmp_path / "other"))
    client = TestClient(app)
    try:
        yield client
    finally:
        client.close()
        app.state.store_catalog.close()


def _signed(client, path, *, caller="sample", secret=None):
    key = secret or client.app.state.agents.get_signing_key(caller)
    headers = build_internal_auth_headers(key, agent_name=caller, method="GET", path=path)
    return client.get(path, headers=headers)


def _inbound(client, message_id="m1", *, chat_id="D1", is_group=False, ts=1700000000.0):
    client.app.state.broker.remember_message_context(BrokerMessage(
        platform="slack", chat_id=chat_id, sender_name="s", sender_id="U1", content="hi",
        agent_name="sample", timestamp=ts, message_id=message_id, is_group=is_group,
    ))


def test_self_scoped_read_returns_timestamp_store_time_and_group_flag(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        before = time.time()
        _inbound(client)
        response = _signed(client, PATH)
        assert response.status_code == 200
        body = response.json()
        assert set(body) == {"message_ts", "stored_at", "is_group"}
        assert body["message_ts"] == 1700000000.0
        assert before <= body["stored_at"] <= time.time()
        assert body["is_group"] is False

        _inbound(client, "g1", chat_id="C1", is_group=True)
        grouped = _signed(client, "/agents/sample/message-context/slack/C1/g1")
        assert grouped.status_code == 200
        assert grouped.json()["is_group"] is True


def test_read_survives_a_restart_through_the_persisted_store(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        _inbound(client)
        broker = client.app.state.broker
        with broker._message_context_lock:
            broker._message_contexts.clear()
            broker._message_context_stored_at.clear()
            broker._message_context_order.clear()
        assert _signed(client, PATH).status_code == 200


@pytest.mark.parametrize("path", [
    "/agents/sample/message-context/slack/D1/missing",
    "/agents/sample/message-context/slack/D2/m1",
    "/agents/sample/message-context/telegram/D1/m1",
])
def test_unknown_identity_is_404_with_retention_text(tmp_path, monkeypatch, path):
    with _gateway(tmp_path, monkeypatch) as client:
        _inbound(client)
        response = _signed(client, path)
        assert response.status_code == 404
        detail = response.json()["detail"]
        assert detail["code"] == "message_context_not_found"
        assert "30 days" in detail["detail"] and "1000" in detail["detail"]


def test_outbound_records_are_not_served(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        client.app.state.broker.remember_outbound_message_context(
            "sample", "m1", platform="slack", chat_id="D1",
        )
        assert _signed(client, PATH).status_code == 404


def test_rows_past_retention_or_cap_are_404(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        store = client.app.state.message_context_store
        base = {"agent_name": "sample", "platform": "slack", "chat_id": "D1", "timestamp": 1.0}
        store.put({**base, "message_id": "m1"}, stored_at=time.time() - 31 * 86400)
        assert _signed(client, PATH).status_code == 404

        now = time.time()
        store.put({**base, "message_id": "m1"}, stored_at=now - 5000)
        for index in range(store.max_per_agent):
            store.put({**base, "message_id": f"fill{index}"}, stored_at=now - 4000 + index)
        assert _signed(client, PATH).status_code == 404
        assert _signed(client, "/agents/sample/message-context/slack/D1/fill7").status_code == 200


def test_other_agents_and_the_global_secret_cannot_read_another_agents_contexts(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        _inbound(client)
        # Another agent's own key: authenticated, wrong identity.
        assert _signed(client, PATH, caller="other").status_code == 403
        # The shared global secret authenticates a name; it does not widen scope.
        assert _signed(client, PATH, caller="other", secret=SECRET).status_code == 403
        # Signed as the owning agent, the global secret still reads only its own rows.
        assert _signed(client, PATH, caller="sample", secret=SECRET).status_code == 200
        assert _signed(client, "/agents/other/message-context/slack/D1/m1", caller="sample").status_code == 403


def test_unsigned_and_owner_session_callers_are_refused(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        _inbound(client)
        assert client.get(PATH).status_code in (401, 403)
        client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(SECRET))
        response = client.get(PATH)
        assert response.status_code == 403
        assert response.json()["detail"] == "verified caller must match the target agent"
