"""Inbound sender facts and first writes survive storage and replay."""

import asyncio
import hashlib
import json
import time
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, Mock

import httpx
import pytest
from fastapi.testclient import TestClient

from pinky_daemon import broker as broker_module
from pinky_daemon import message_context_store as store_module
from pinky_daemon import pollers
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.api import create_api
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.message_context_store import MessageContextStore
from pinky_daemon.sessions import SessionManager
from pinky_daemon.transport_state import SessionState
from pinky_outreach.telegram import TelegramAdapter

pytestmark = pytest.mark.real_auth

FIELDS = (
    "user_id", "was_bot", "bot_id", "forwarded", "via_bot_id", "sender_business_bot_id",
    "reply_to", "reply_to_sender_id", "platform_ts",
)
TEXT_KEYS = {"message_ts", "stored_at", "is_group", "sender_id", "source_was_voice",
             "text_status", "text", "text_sha256"}


def _facts(**changes):
    values = dict(user_id="202", was_bot=False, bot_id=None, forwarded=False,
                  via_bot_id=None, sender_business_bot_id=None, reply_to=None,
                  reply_to_sender_id=None, platform_ts=1700000000)
    values.update(changes)
    return values


def _contract(metadata):
    return {key: metadata[key] for key in FIELDS if key in metadata}


@pytest.fixture(autouse=True)
def private_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("PINKY_SESSION_CLASS_REBUILD", "0")
    monkeypatch.setenv("PINKY_SESSION_SECRET", "fixture-inbound-context-secret")


@pytest.fixture
def store(tmp_path):
    result = MessageContextStore(str(tmp_path / "context.db"))
    yield result
    result.close()


@pytest.fixture
def gateway(tmp_path):
    app = create_api(db_path=str(tmp_path / "memory.db"), default_working_dir=str(tmp_path))
    work = tmp_path / "work"
    work.mkdir()
    app.state.agents.register("sample", working_dir=str(work))
    app.state.agents.set_default_timezone("UTC")
    client = TestClient(app)
    yield SimpleNamespace(app=app, client=client, broker=app.state.broker,
                          store=app.state.message_context_store, agents=app.state.agents)
    client.close()
    app.state.store_catalog.close()


def _message(**changes):
    values = dict(platform="telegram", chat_id="101", sender_name="Sender", sender_id="202",
                  content="first", agent_name="sample", timestamp=1700000000.0,
                  message_id="41", metadata=_facts())
    values.update(changes)
    return BrokerMessage(**values)


def _context(message_id="41", *, direction="inbound", **changes):
    values = dict(agent_name="sample", platform="telegram", chat_id="101", message_id=message_id,
                  timestamp=1700000000.0, reply_to="", is_group=False, source_was_voice=False,
                  attachments=[], metadata={**_facts(), "direction": direction})
    values.update(changes)
    return values


def _text(gateway, *, platform="telegram", chat_id="101", message_id="41"):
    path = f"/agents/sample/message-context/{platform}/{chat_id}/{message_id}/text"
    headers = build_internal_auth_headers(gateway.agents.get_signing_key("sample"),
                                         agent_name="sample", method="GET", path=path)
    return gateway.client.get(path, headers=headers)


def _telegram_data(**changes):
    values = {"message_id": 41, "date": 1700000000, "chat": {"id": 101, "type": "private"},
              "from": {"id": 202, "first_name": "Sender", "is_bot": False}, "text": "first"}
    values.update(changes)
    return values


def _telegram(data):
    def response(request):
        assert json.loads(request.content)["allowed_updates"] == ["message"]
        return httpx.Response(200, json={"ok": True, "result": [{"update_id": 1, "message": data}]})

    adapter = TelegramAdapter("fixture-token")
    adapter._client.close()
    adapter._client = httpx.Client(transport=httpx.MockTransport(response))
    try:
        messages = adapter.get_updates()
        assert len(messages) == 1
        return messages[0]
    finally:
        adapter.close()


def test_plain_telegram_message_carries_every_sender_fact():
    message = _telegram(_telegram_data())
    assert _contract(message.metadata) == _facts()
    assert message.reply_to == ""
    assert message.metadata["sender_id"] == "202"
    assert message.metadata["username"] == ""
    assert message.metadata["chat_type"] == "private"
    assert message.metadata["entities"] == []
    assert type(message.metadata["platform_ts"]) is int
    assert message.metadata["was_bot"] is False


@pytest.mark.parametrize("signal,value,expected", [
    ("forward_origin", {}, True), ("forward_from", {"id": 303}, True),
    ("forward_from_chat", {"id": 404}, True), ("forward_sender_name", "Sender", True),
    ("forward_date", 1600000000, True), ("forward_future_kind", None, True),
    ("is_automatic_forward", True, True), ("is_automatic_forward", False, False),
])
def test_each_telegram_forward_signal_is_preserved(signal, value, expected):
    message = _telegram(_telegram_data(**{signal: value}))
    assert _contract(message.metadata) == _facts(forwarded=expected)
    assert isinstance(message.metadata["forwarded"], bool)


@pytest.mark.parametrize("field", ["via_bot", "sender_business_bot"])
@pytest.mark.parametrize("present", [False, True])
def test_telegram_bot_attribution_keeps_decimal_ids(field, present):
    data = _telegram_data(**({field: {"id": 7000000123}} if present else {}))
    metadata = _telegram(data).metadata
    assert _contract(metadata) == _facts(**{field + "_id": "7000000123" if present else None})


@pytest.mark.parametrize("kind", ["bot", "absent"])
def test_telegram_sender_kind_does_not_invent_a_user(kind):
    data = _telegram_data()
    if kind == "bot":
        data["from"]["is_bot"] = True
    else:
        del data["from"]
    metadata = _telegram(data).metadata
    assert _contract(metadata) == _facts(user_id="202" if kind == "bot" else None,
                                         was_bot=kind == "bot", bot_id="202" if kind == "bot" else None)
    assert metadata["sender_id"] == ("202" if kind == "bot" else "")


@pytest.mark.parametrize("has_sender", [False, True])
def test_telegram_reply_facts_keep_the_replied_sender_when_available(has_sender):
    reply = {"message_id": 39}
    if has_sender:
        reply["from"] = {"id": 303}
    message = _telegram(_telegram_data(reply_to_message=reply))
    assert _contract(message.metadata) == _facts(reply_to="39", reply_to_sender_id="303" if has_sender else None)
    assert message.reply_to == "39"


def test_large_telegram_ids_keep_exact_decimal_digits():
    large = 7000000123
    data = _telegram_data(**{"from": {"id": large, "is_bot": True},
                            "via_bot": {"id": large + 1}, "sender_business_bot": {"id": large + 2},
                            "reply_to_message": {"message_id": large + 3, "from": {"id": large + 4}}})
    metadata = _telegram(data).metadata
    expected = _facts(user_id=str(large), was_bot=True, bot_id=str(large),
                      via_bot_id=str(large + 1), sender_business_bot_id=str(large + 2),
                      reply_to=str(large + 3), reply_to_sender_id=str(large + 4))
    assert _contract(metadata) == expected
    assert all(isinstance(metadata[key], str) and metadata[key].isdecimal()
               for key in FIELDS if key.endswith("_id") or key == "reply_to")


async def _slack(monkeypatch, event):
    current = SimpleNamespace(handle_inbound=AsyncMock())
    adapter = MagicMock()
    poller = pollers.BrokerSlackPoller(adapter, "sample", current, registry=None, app_token="fixture")
    poller._bot_user_id, poller._bot_id = "USELF", "BSELF"
    monkeypatch.setattr(poller, "_resolve_user_name", AsyncMock(return_value="Sender"))
    monkeypatch.setattr(poller, "_resolve_channel_title", AsyncMock(return_value="Channel"))
    monkeypatch.setattr(poller, "_resolve_text_refs", AsyncMock(side_effect=lambda value: value))
    deliveries = []
    monkeypatch.setattr(pollers, "_deliver_in_background", lambda coro, prefix: deliveries.append(coro))
    await poller._handle_event({"event": {"type": "message", "user": "UPERSON", "channel": "D_FIXTURE",
                                         "channel_type": "im", "text": "first", "ts": "1700000000.1", **event}})
    assert len(deliveries) == 1
    await deliveries[0]
    return current.handle_inbound.call_args.args[0]


def _slack_facts(**changes):
    return _facts(user_id="UPERSON", forwarded=None, platform_ts=None, **changes)


@pytest.mark.asyncio
async def test_slack_person_message_keeps_all_nine_fields(monkeypatch):
    message = await _slack(monkeypatch, {})
    assert _contract(message.metadata) == _slack_facts()
    assert message.sender_id == "UPERSON"
    assert message.reply_to == ""
    assert message.metadata["channel_type"] == "im"
    assert message.metadata["thread_ts"] == ""
    assert message.metadata["was_bot"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["id", "subtype", "both"])
async def test_slack_bot_signals_preserve_missing_user_ids(monkeypatch, kind):
    event = {"user": None}
    if kind in ("id", "both"):
        event["bot_id"] = "BOTHER"
    if kind in ("subtype", "both"):
        event["subtype"] = "bot_message"
    message = await _slack(monkeypatch, event)
    expected = _slack_facts()
    expected.update(user_id=None, was_bot=True, bot_id="BOTHER" if kind != "subtype" else None)
    assert _contract(message.metadata) == expected
    assert message.metadata["was_bot"] is True
    assert message.sender_id == ("BOTHER" if kind != "subtype" else "")


@pytest.mark.asyncio
@pytest.mark.parametrize("is_root", [False, True])
async def test_slack_thread_facts_distinguish_the_root_from_a_reply(monkeypatch, is_root):
    ts, root = "1700000000.2", "1700000000.1"
    thread = ts if is_root else root
    message = await _slack(monkeypatch, {"ts": ts, "thread_ts": thread, "parent_user_id": "UPARENT"})
    assert _contract(message.metadata) == _slack_facts(reply_to=None if is_root else root,
                                                      reply_to_sender_id="UPARENT")
    assert message.reply_to == thread


@pytest.mark.parametrize("kind", ["user", "bot"])
def test_nullable_slack_ids_keep_the_existing_prompt_header(gateway, kind):
    old = {"user_id": "UPERSON" if kind == "user" else "", "bot_id": "BOTHER" if kind == "bot" else ""}
    new = {key: value or None for key, value in old.items()}
    values = dict(platform="slack", chat_id="D_FIXTURE", timestamp=0, metadata=old)
    legacy = gateway.broker._format_prompt(_message(**values))
    current = gateway.broker._format_prompt(_message(**{**values, "metadata": new}))
    assert current == legacy
    assert current.splitlines()[0].endswith(f"| slack_{kind}_id:{old[kind + '_id']}]")


@pytest.mark.asyncio
@pytest.mark.parametrize("legacy", [False, True])
@pytest.mark.parametrize("reopen", [False, True])
@pytest.mark.parametrize("is_group", [False, True])
async def test_held_slack_fields_match_live_storage_after_approval(
    tmp_path, monkeypatch, legacy, reopen, is_group,
):
    registry_path, store_path = str(tmp_path / "agents.db"), str(tmp_path / "context.db")
    registry = AgentRegistry(registry_path)
    registry.register("sample", working_dir=str(tmp_path))
    registry.set_primary_user("owner", display_name="Owner")
    durable = MessageContextStore(store_path)

    def make_broker():
        result = MessageBroker(registry, SessionManager(), message_context_store=durable)
        monkeypatch.setattr(result, "_notify_approval_request", AsyncMock())
        session = SimpleNamespace(state=SessionState.CONNECTED, send=AsyncMock(return_value=True))
        result.register_streaming("sample", session)
        return result, session

    try:
        broker, session = make_broker()
        expected = {"user_id": "UPERSON", "bot_id": None} if legacy else _slack_facts()
        channel = "C_FIXTURE" if is_group else "D_FIXTURE"
        gate = channel if is_group else "UPERSON"
        message = _message(platform="slack", chat_id=channel, message_id="held",
                           sender_id="UPERSON", timestamp=1700000123.25, is_group=is_group,
                           metadata={**expected, "unrelated": "not retained"})
        broker.remember_message_context(replace(message, message_id="live"))
        live = durable.get("sample", "live", platform="slack", chat_id=channel)
        await broker.handle_inbound(message)
        pending = registry.get_pending_messages("sample", gate)
        assert len(pending) == 1
        assert pending[0]["slack_envelope"]["metadata"] == expected
        session.send.assert_not_awaited()
        if reopen:
            registry.close()
            durable.close()
            registry = AgentRegistry(registry_path)
            durable = MessageContextStore(store_path)
            broker, session = make_broker()
        registry.approve_user("sample", gate, display_name="Approved")
        assert await broker.handle_approval("sample", gate) == 1
        session.send.assert_awaited_once()
        stored = durable.get("sample", "held", platform="slack", chat_id=channel)
        assert _contract(stored["metadata"]) == _contract(live["metadata"]) == expected
        assert stored["metadata"]["direction"] == "inbound"
        assert registry.get_pending_messages("sample", gate) == []
        assert durable.get_text("sample", "held", platform="slack", chat_id=channel) == (
            "first", "ok", "UPERSON",
        )
    finally:
        registry.close()
        durable.close()


def _raw_row(store, message_id="41"):
    row = store._db.execute("SELECT * FROM message_contexts WHERE agent_name=? AND platform=? "
                            "AND chat_id=? AND message_id=?", ("sample", "telegram", "101", message_id)).fetchone()
    return tuple(row) if row is not None else None


def test_second_inbound_put_preserves_every_first_column(store):
    now = time.time()
    first = _context(attachments=[{"type": "file", "file_id": "first"}])
    first_result = store.put(first, content="first", sender_id="202", stored_at=now)
    before = _raw_row(store)
    second = _context(timestamp=1700000001.5, reply_to="37", is_group=True, source_was_voice=True,
                      attachments=[{"type": "file", "file_id": "second"}],
                      metadata={**_facts(user_id="303", forwarded=True), "direction": "inbound"})
    second_result = store.put(second, content="second", sender_id="303", stored_at=now + 1)
    assert _raw_row(store) == before
    assert store.get_text("sample", "41", platform="telegram", chat_id="101") == ("first", "ok", "202")
    assert first_result is True and second_result is False


@pytest.mark.parametrize("direction", ["outbound", None])
def test_inbound_put_replaces_an_outbound_or_unstamped_row(store, direction):
    metadata = {} if direction is None else {"direction": direction}
    store.put(_context(metadata=metadata), content="first", sender_id="202")
    written = store.put(_context(), content="second", sender_id="303")
    assert store.get_text("sample", "41", platform="telegram", chat_id="101") == ("second", "ok", "303")
    assert store.get("sample", "41")["metadata"]["direction"] == "inbound"
    assert written is True


@pytest.mark.parametrize("direction", ["outbound", None])
def test_non_inbound_put_cannot_replace_visible_inbound_text(store, direction):
    store.put(_context(), content="first", sender_id="202")
    before = _raw_row(store)
    metadata = {} if direction is None else {"direction": direction}
    written = store.put(_context(metadata=metadata), content="second", sender_id="303")
    assert _raw_row(store) == before
    assert store.get_text("sample", "41", platform="telegram", chat_id="101") == ("first", "ok", "202")
    assert written is False


@pytest.mark.parametrize("at_cutoff", [False, True])
def test_store_first_write_rule_uses_the_current_visibility_cutoff(store, monkeypatch, at_cutoff):
    now = 1700000000.0
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: now))
    age = store.retention_seconds + (0 if at_cutoff else 0.001)
    store.put(_context(), content="first", sender_id="202", stored_at=now - age)
    written = store.put(_context(), content="second", sender_id="303", stored_at=now)
    expected = ("first", "ok", "202") if at_cutoff else ("second", "ok", "303")
    assert store.get_text("sample", "41", platform="telegram", chat_id="101") == expected
    assert written is (not at_cutoff)


def test_refused_store_put_neither_sweeps_another_row_nor_commits(store, monkeypatch):
    now = time.time()
    store.put(_context(), content="first", sender_id="202", stored_at=now)
    store.put(_context("expired"), content="old", sender_id="303",
              stored_at=now - store.retention_seconds - 1)
    assert _raw_row(store, "expired") is not None
    original = store._db
    commit = Mock(wraps=original.commit)

    class ObservedConnection:
        def __getattr__(self, name):
            return commit if name == "commit" else getattr(original, name)

    monkeypatch.setattr(store, "_db", ObservedConnection())
    before = _raw_row(store)
    written = store.put(_context(), content="second", sender_id="303")
    assert _raw_row(store, "expired") is not None
    assert _raw_row(store) == before
    commit.assert_not_called()
    assert written is False


@pytest.mark.parametrize("part", ["agent_name", "platform", "chat_id", "message_id"])
def test_empty_store_identity_returns_false_without_writing(store, part):
    result = store.put(_context(**{part: ""}), content="first", sender_id="202")
    assert result is False
    assert store._db.execute("SELECT count(*) FROM message_contexts").fetchone()[0] == 0


def test_cached_inbound_refuses_before_the_store_and_all_reads_keep_the_first(gateway, monkeypatch):
    first = _message()
    gateway.broker.remember_message_context(first)
    key = ("sample", "telegram", "101", "41")
    before = gateway.broker._message_contexts[key]
    put = Mock(wraps=gateway.store.put)
    monkeypatch.setattr(gateway.store, "put", put)
    gateway.broker.remember_message_context(_message(content="second", sender_id="303",
                                                    timestamp=1700000001,
                                                    metadata=_facts(user_id="303", forwarded=True)))
    put.assert_not_called()
    assert gateway.broker._message_contexts[key] is before
    assert gateway.broker.get_message_context("sample", "41").to_dict() == before.to_dict()
    assert gateway.store.get_text("sample", "41", platform="telegram", chat_id="101") == ("first", "ok", "202")
    body = _text(gateway)
    assert body.status_code == 200
    assert body.json()["text"] == "first"
    assert _contract(body.json()) == _facts()


@pytest.fixture
def cache_broker(tmp_path):
    registry = AgentRegistry(str(tmp_path / "agents.db"))
    registry.register("sample", working_dir=str(tmp_path))
    yield MessageBroker(registry, SessionManager())
    registry.close()


def test_cache_without_a_store_keeps_the_first_inbound_context(cache_broker):
    cache_broker.remember_message_context(_message())
    first = cache_broker.get_message_context("sample", "41")
    assert first is not None
    before = first.to_dict()
    cache_broker.remember_message_context(_message(timestamp=1700000001, metadata=_facts(user_id="303")))
    current = cache_broker.get_message_context("sample", "41")
    assert current is not None
    assert current.to_dict() == before


def test_expired_inbound_cache_entry_does_not_block_a_new_write(cache_broker, monkeypatch):
    clock = [1700000000.0]
    monkeypatch.setattr(broker_module, "time", SimpleNamespace(**{**vars(time), "time": lambda: clock[0]}))
    cache_broker.remember_message_context(_message())
    clock[0] += 31 * 86400
    cache_broker.remember_message_context(_message(timestamp=1700000001, metadata=_facts(user_id="303")))
    context = cache_broker.get_message_context("sample", "41")
    assert context is not None
    assert context.metadata["user_id"] == "303"


def test_cached_outbound_context_remains_replaceable(cache_broker):
    cache_broker.remember_outbound_message_context("sample", "41", platform="telegram", chat_id="101")
    cache_broker.remember_message_context(_message())
    assert _contract(cache_broker.get_message_context("sample", "41").metadata) == _facts()
    assert cache_broker.get_message_context("sample", "41").metadata["direction"] == "inbound"


def test_store_failure_on_the_first_write_preserves_cache_and_the_existing_log(gateway, monkeypatch):
    logs = []
    monkeypatch.setattr(broker_module, "_log", logs.append)
    put = Mock(side_effect=RuntimeError("synthetic persistence fault"))
    monkeypatch.setattr(gateway.store, "put", put)
    gateway.broker.remember_message_context(_message())
    context = gateway.broker.get_message_context("sample", "41")
    assert context is not None
    assert _contract(context.metadata) == _facts()
    assert logs == ["message-context persistence failed: RuntimeError"]
    put.assert_called_once()


def test_store_refusal_after_restart_never_caches_the_redelivery(gateway):
    gateway.store.put(_context(), content="first", sender_id="202")
    key = ("sample", "telegram", "101", "41")
    assert key not in gateway.broker._message_contexts
    gateway.broker.remember_message_context(_message(content="second", sender_id="303",
                                                    metadata=_facts(user_id="303")))
    assert key not in gateway.broker._message_contexts
    assert _contract(gateway.broker.get_message_context("sample", "41").metadata) == _facts()
    body = _text(gateway)
    assert body.status_code == 200
    assert body.json()["text"] == "first" and body.json()["sender_id"] == "202"
    assert _contract(body.json()) == _facts()


def test_text_route_returns_all_nine_stored_values_without_coercion(gateway):
    message = _message(metadata=_facts())
    gateway.broker.remember_message_context(message)
    response = _text(gateway)
    assert response.status_code == 200
    body = response.json()
    assert set(body) == TEXT_KEYS | set(FIELDS)
    assert _contract(body) == _facts()
    stored_at = gateway.store.get("sample", "41", include_stored_at=True)["_stored_at"]
    assert {key: body[key] for key in TEXT_KEYS} == dict(
        message_ts=1700000000.0, stored_at=stored_at, is_group=False, sender_id="202",
        source_was_voice=False, text_status="ok", text="first",
        text_sha256=hashlib.sha256(b"first").hexdigest(),
    )


@pytest.mark.parametrize("fields", [{}, {"user_id": "202", "forwarded": False, "via_bot_id": None}],
                         ids=["legacy", "partial"])
def test_text_route_omits_contract_keys_absent_from_the_row(gateway, fields):
    gateway.broker.remember_message_context(_message(metadata=fields))
    response = _text(gateway)
    assert response.status_code == 200
    assert set(response.json()) == TEXT_KEYS | set(fields)
    assert _contract(response.json()) == fields


def test_text_route_never_exposes_other_metadata(gateway):
    unrelated = dict(direction="outbound", username="sender", entities=[], chat_title="title",
                     attachments=[], sender_id="not the envelope sender", future_key="private")
    gateway.broker.remember_message_context(_message(metadata=unrelated))
    response = _text(gateway)
    assert response.status_code == 200
    assert set(response.json()) == TEXT_KEYS
    assert response.json()["sender_id"] == "202"


@pytest.mark.asyncio
async def test_telegram_update_reaches_the_signed_text_route_through_the_broker(gateway, monkeypatch):
    adapter = TelegramAdapter("fixture-token")
    adapter._client.close()
    adapter._client = httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(
        200, json={"ok": True, "result": [{"update_id": 1, "message": _telegram_data()}]},
    )))
    gateway.agents.set_primary_user("owner", display_name="Owner")
    gateway.agents.approve_user("sample", "202", display_name="Sender")
    session = SimpleNamespace(state=SessionState.CONNECTED, send=AsyncMock(return_value=True))
    gateway.broker.register_streaming("sample", session)
    monkeypatch.setattr(gateway.broker, "_start_typing", AsyncMock())
    pending = []
    monkeypatch.setattr(pollers, "_deliver_in_background", lambda coro, prefix: pending.append(coro))
    poller = pollers.BrokerTelegramPoller(adapter, "sample", gateway.broker)
    try:
        await poller._poll_once()
        assert len(pending) == 1
        await asyncio.gather(*pending)
        session.send.assert_awaited_once()
        response = _text(gateway)
        assert response.status_code == 200
        assert _contract(response.json()) == _facts()
        assert response.json()["text"] == "first"
        assert response.json()["message_ts"] != response.json()["platform_ts"]
    finally:
        adapter.close()
        poller._shutdown_watchdog()
