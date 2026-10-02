"""Signed, self-scoped read of one routed inbound message context."""

from __future__ import annotations

import time
from contextlib import contextmanager

import pytest
from fastapi import HTTPException, Request
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


def test_only_records_stamped_inbound_by_routing_are_served(tmp_path, monkeypatch):
    """The routing path stamps direction=inbound last; nothing else counts as proof."""
    with _gateway(tmp_path, monkeypatch) as client:
        broker = client.app.state.broker
        store = client.app.state.message_context_store
        # Platform metadata claiming "outbound" is overridden at routing.
        broker.remember_message_context(BrokerMessage(
            platform="slack", chat_id="D1", sender_name="s", sender_id="U1", content="hi",
            agent_name="sample", timestamp=1.0, message_id="m1",
            metadata={"direction": "outbound", "team": "T1"},
        ))
        row = store.get("sample", "m1", platform="slack", chat_id="D1")
        assert row["metadata"]["team"] == "T1"
        assert row["metadata"]["direction"] == "inbound"
        assert _signed(client, PATH).status_code == 200

        # An outbound record the agent produced itself.
        broker.remember_outbound_message_context("sample", "o1", platform="slack", chat_id="D1")
        assert _signed(client, "/agents/sample/message-context/slack/D1/o1").status_code == 404

        # Legacy rows carry no stamp and are refused the same way.
        base = {"agent_name": "sample", "platform": "slack", "chat_id": "D1", "timestamp": 1.0}
        store.put({**base, "message_id": "legacy"})
        store.put({**base, "message_id": "blank", "metadata": {"direction": ""}})
        store.put({**base, "message_id": "odd", "metadata": {"direction": "Inbound"}})
        for message_id in ("legacy", "blank", "odd"):
            response = _signed(client, f"/agents/sample/message-context/slack/D1/{message_id}")
            assert response.status_code == 404
            assert response.json()["detail"]["code"] == "message_context_not_found"


def test_rows_past_retention_or_cap_are_404(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        store = client.app.state.message_context_store
        base = {
            "agent_name": "sample", "platform": "slack", "chat_id": "D1", "timestamp": 1.0,
            "metadata": {"direction": "inbound"},
        }
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


def test_cache_only_rows_are_not_served_when_the_store_has_dropped_them(tmp_path, monkeypatch):
    """The persisted store's bounds decide; a stale cache entry can never widen them."""
    with _gateway(tmp_path, monkeypatch) as client:
        broker = client.app.state.broker
        store = client.app.state.message_context_store
        _inbound(client)
        assert _signed(client, PATH).status_code == 200  # cached at the head of the order
        base = {"agent_name": "sample", "platform": "slack", "chat_id": "D1", "timestamp": 1.0}
        now = time.time()
        for index in range(store.max_per_agent):
            store.put({**base, "message_id": f"fill{index}"}, stored_at=now + index)
        assert store.get("sample", "m1", platform="slack", chat_id="D1") is None
        assert ("sample", "slack", "D1", "m1") in broker._message_contexts
        assert _signed(client, PATH).status_code == 404
        # The read is side-effect free: reply routing still owns the cache entry.
        assert ("sample", "slack", "D1", "m1") in broker._message_contexts


def test_a_row_that_never_persisted_is_not_served(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        broker = client.app.state.broker
        from pinky_daemon.broker import MessageContext

        with broker._message_context_lock:
            broker._cache_message_context(MessageContext(
                agent_name="sample", message_id="m1", platform="slack", chat_id="D1", timestamp=5.0,
            ))
        assert _signed(client, PATH).status_code == 404


def test_without_a_store_nothing_is_served(tmp_path, monkeypatch):
    """The bounds live in the store; a cache-only broker cannot enforce them."""
    with _gateway(tmp_path, monkeypatch) as client:
        broker = client.app.state.broker
        _inbound(client)
        assert _signed(client, PATH).status_code == 200
        broker._message_context_store = None
        assert broker.get_message_context_by_identity("sample", "slack", "D1", "m1") is None
        assert _signed(client, PATH).status_code == 404


@pytest.mark.parametrize("encoded, decoded", [("%3F", "?"), ("%23", "#")])
def test_identity_segments_with_reserved_characters_are_refused_before_lookup(
    tmp_path, monkeypatch, encoded, decoded
):
    """Reserved identity characters are rejected before a store lookup."""
    with _gateway(tmp_path, monkeypatch) as client:
        store = client.app.state.message_context_store
        base = {"agent_name": "sample", "platform": "slack", "timestamp": 1.0,
                "metadata": {"direction": "inbound"}}
        store.put({**base, "chat_id": f"D{decoded}one", "message_id": "original"})
        store.put({**base, "chat_id": f"D{decoded}two", "message_id": "different"})
        # The signature covers a plain path; both requests require a full routed path.
        signed_for = "/agents/sample/message-context/slack/D"
        headers = build_internal_auth_headers(
            client.app.state.agents.get_signing_key("sample"),
            agent_name="sample", method="GET", path=signed_for,
        )
        queried = []
        real_get = store.get

        def spy(*args, **kwargs):
            queried.append((args, kwargs))
            return real_get(*args, **kwargs)

        monkeypatch.setattr(store, "get", spy)
        first = client.get(f"/agents/sample/message-context/slack/D{encoded}one/original", headers=headers)
        second = client.get(f"/agents/sample/message-context/slack/D{encoded}two/different", headers=headers)
        assert first.status_code == 401
        assert second.status_code == 401
        assert first.json() == {"detail": "Unauthorized"}
        assert second.json() == first.json()
        assert queried == []


@pytest.mark.parametrize("segment", ["platform", "chat_id", "message_id"])
@pytest.mark.parametrize("delimiter", ["?", "#"])
async def test_message_context_handler_checks_identity_before_lookup(
    tmp_path, monkeypatch, segment, delimiter
):
    """The handler retains its own identity validation after authentication."""
    with _gateway(tmp_path, monkeypatch) as client:
        endpoint = next(
            route.endpoint for route in client.app.routes
            if getattr(route, "path", None)
            == "/agents/{name}/message-context/{platform}/{chat_id}/{message_id}"
        )

        def unexpected_lookup(*args, **kwargs):
            pytest.fail("identity validation must precede the context lookup")

        monkeypatch.setattr(
            client.app.state.broker, "get_message_context_by_identity", unexpected_lookup,
        )
        identity = {"name": "sample", "platform": "slack", "chat_id": "D1", "message_id": "m1"}
        identity[segment] += delimiter + "part"
        request = Request({"type": "http", "state": {"internal_caller": "sample"}})
        with pytest.raises(HTTPException) as denied:
            await endpoint(**identity, request=request)
        assert denied.value.status_code == 404
        assert denied.value.detail["code"] == "message_context_not_found"



_TEXT_BASE = "/agents/sample/message-context/test/chat/record"
_TEXT_KEYS = {"message_ts", "stored_at", "is_group", "sender_id", "source_was_voice",
              "text_status", "text", "text_sha256"}


def _remember_text(client, *, content="exact text", sender="sender-1", voice=False, **kwargs):
    values = dict(platform="test", chat_id="chat", sender_name="sender", sender_id=sender,
                  content=content, agent_name="sample", timestamp=123.0, message_id="record")
    values.update(kwargs)
    client.app.state.broker.remember_message_context(BrokerMessage(**values), source_was_voice=voice)


@pytest.mark.parametrize("text", ["", " exact\r\ntext e\u0301 é 😀 ", "é" * 8192],
                         ids=["empty", "unicode", "multibyte-limit"])
@pytest.mark.parametrize("voice", [False, True])
def test_text_path_returns_exact_private_fields_and_utf8_digest(tmp_path, monkeypatch, text, voice):
    import hashlib

    with _gateway(tmp_path, monkeypatch) as client:
        _remember_text(client, content=text, voice=voice, is_group=True)
        response = _signed(client, _TEXT_BASE + "/text")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == _TEXT_KEYS
        assert body["message_ts"] == 123.0 and body["is_group"] is True
        assert body["sender_id"] == "sender-1" and body["source_was_voice"] is voice
        assert body["text_status"] == "ok" and body["text"] == text
        assert body["text_sha256"] == hashlib.sha256(text.encode("utf-8")).hexdigest()


@pytest.mark.parametrize("query", ["", "?include=text", "?include=bogus", "?include="])
def test_routing_path_never_reads_or_returns_text_even_with_query(tmp_path, monkeypatch, query):
    with _gateway(tmp_path, monkeypatch) as client:
        _remember_text(client)

        def forbidden(*args, **kwargs):
            pytest.fail("routing-only response must not invoke the text getter")

        monkeypatch.setattr(client.app.state.message_context_store, "get_text", forbidden, raising=False)
        response = _signed(client, _TEXT_BASE + query)
        assert response.status_code == 200
        assert set(response.json()) == {"message_ts", "stored_at", "is_group"}


def test_routing_path_signature_cannot_authorize_text_path(tmp_path, monkeypatch):
    with _gateway(tmp_path, monkeypatch) as client:
        _remember_text(client)
        headers = build_internal_auth_headers(client.app.state.agents.get_signing_key("sample"),
                                              agent_name="sample", method="GET", path=_TEXT_BASE)
        assert client.get(_TEXT_BASE, headers=headers).status_code == 200
        response = client.get(_TEXT_BASE + "/text", headers=headers)
        assert response.status_code == 401
        assert response.json() == {"detail": "Unauthorized"}
        assert _signed(client, _TEXT_BASE + "/text").status_code == 200


@pytest.mark.parametrize("caller", ["other", "session"])
def test_text_path_requires_self_caller_before_lookup(tmp_path, monkeypatch, caller):
    with _gateway(tmp_path, monkeypatch) as client:
        def forbidden(*args, **kwargs):
            pytest.fail("self-caller check must precede lookup")

        monkeypatch.setattr(client.app.state.broker, "get_message_context_by_identity", forbidden)
        if caller == "session":
            client.cookies.set(SESSION_COOKIE_NAME, create_session_cookie(SECRET))
            response = client.get(_TEXT_BASE + "/text")
        else:
            response = _signed(client, _TEXT_BASE + "/text", caller=caller)
        assert response.status_code == 403
        assert response.json()["detail"] == "verified caller must match the target agent"


@pytest.mark.parametrize("kind", ["missing", "outbound", "legacy", "expired", "capped", "cache", "no-store", "failed-put"])
def test_text_path_all_invisible_rows_share_routing_miss_body(tmp_path, monkeypatch, kind):
    with _gateway(tmp_path, monkeypatch) as client:
        if kind == "capped":
            client.app.state.message_context_store.max_per_agent = 1
        expected = _signed(client, _TEXT_BASE).json()
        broker, store = client.app.state.broker, client.app.state.message_context_store
        if kind == "outbound":
            broker.remember_outbound_message_context("sample", "record", platform="test", chat_id="chat")
        elif kind == "legacy":
            store.put({"agent_name": "sample", "message_id": "record", "platform": "test", "chat_id": "chat"})
        elif kind == "failed-put":
            def fail(*args, **kwargs):
                raise RuntimeError("synthetic failure")
            monkeypatch.setattr(store, "put", fail)
            _remember_text(client)
        elif kind != "missing":
            _remember_text(client)
            if kind == "expired":
                store._db.execute("UPDATE message_contexts SET stored_at=?", (time.time() - 31 * 86400,))
                store._db.commit()
            elif kind == "capped":
                _remember_text(client, message_id="new-record")
            elif kind == "cache":
                store._db.execute("DELETE FROM message_contexts")
                store._db.commit()
            elif kind == "no-store":
                broker._message_context_store = None
        response = _signed(client, _TEXT_BASE + "/text")
        assert response.status_code == 404
        assert response.json() == expected


@pytest.mark.parametrize("status", ["too_long", "absent"])
def test_text_path_null_text_and_hash_for_unavailable_content(tmp_path, monkeypatch, status):
    with _gateway(tmp_path, monkeypatch) as client:
        if status == "too_long":
            _remember_text(client, content="é" * 8192 + "x")
        else:
            client.app.state.message_context_store.put({
                "agent_name": "sample", "message_id": "record", "platform": "test",
                "chat_id": "chat", "timestamp": 123.0, "metadata": {"direction": "inbound"},
            })
        response = _signed(client, _TEXT_BASE + "/text")
        assert response.status_code == 200
        body = response.json()
        assert set(body) == _TEXT_KEYS
        assert body["text_status"] == status
        assert body["text"] is None and body["text_sha256"] is None
        assert body["sender_id"] == ("sender-1" if status == "too_long" else "")


@pytest.mark.parametrize("change", ["replace", "remove"])
def test_text_path_refuses_row_changes_between_context_and_text_reads(tmp_path, monkeypatch, change):
    with _gateway(tmp_path, monkeypatch) as client:
        expected = _signed(client, _TEXT_BASE).json()
        _remember_text(client)
        store = client.app.state.message_context_store
        getter = getattr(store, "get_text", None)
        assert callable(getter), "dedicated snapshot-checked text getter required"

        def race(*args, **kwargs):
            if change == "replace":
                store.put({"agent_name": "sample", "message_id": "record", "platform": "test",
                           "chat_id": "chat", "metadata": {"direction": "inbound"}},
                          content="replacement", sender_id="replacement", stored_at=time.time() + 1)
            else:
                store.sweep_retention(now=time.time() + 31 * 86400)
            return getter(*args, **kwargs)

        monkeypatch.setattr(store, "get_text", race)
        response = _signed(client, _TEXT_BASE + "/text")
        assert response.status_code == 404 and response.json() == expected


@pytest.mark.parametrize("segment", ["platform", "chat_id", "message_id"])
@pytest.mark.parametrize("delimiter", ["?", "#"])
async def test_text_handler_checks_identity_before_lookup(tmp_path, monkeypatch, segment, delimiter):
    with _gateway(tmp_path, monkeypatch) as client:
        endpoint = next((route.endpoint for route in client.app.routes
                         if getattr(route, "path", None) ==
                         "/agents/{name}/message-context/{platform}/{chat_id}/{message_id}/text"), None)
        assert endpoint is not None, "a separately signed text route is required"

        def forbidden(*args, **kwargs):
            pytest.fail("identity refusal must precede lookup")

        monkeypatch.setattr(client.app.state.broker, "get_message_context_by_identity", forbidden)
        identity = {"name": "sample", "platform": "test", "chat_id": "chat", "message_id": "record"}
        identity[segment] += delimiter
        request = Request({"type": "http", "state": {"internal_caller": "sample"}})
        with pytest.raises(HTTPException) as denied:
            await endpoint(**identity, request=request)
        assert denied.value.status_code == 404
        assert denied.value.detail["code"] == "message_context_not_found"


def test_text_route_does_not_log_text_or_sender(tmp_path, monkeypatch, caplog, capsys):
    with _gateway(tmp_path, monkeypatch) as client:
        text, sender = "payload-canary-7824", "sender-canary-7824"
        _remember_text(client, content=text, sender=sender)
        response = _signed(client, _TEXT_BASE + "/text")
        assert response.status_code == 200
        assert response.json()["text"] == text
        captured = capsys.readouterr()
        logs = caplog.text + captured.out + captured.err
        assert text not in logs and sender not in logs
