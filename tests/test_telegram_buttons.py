"""Inline button sends and callback taps preserve delivery and stored facts."""

import asyncio
import copy
import json
import time
from contextlib import asynccontextmanager, contextmanager, suppress
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest

from pinky_daemon import broker as broker_module
from pinky_daemon import message_context_store as store_module
from pinky_daemon import pollers
from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.auth_relay import AuthRelayCoordinator
from pinky_daemon.transport_state import SessionState
from pinky_messaging.server import create_server
from pinky_outreach import telegram as telegram_module
from pinky_outreach.telegram import TelegramAdapter, TelegramError
from tests.test_inbound_message_fields import (
    FIELDS,
    TEXT_KEYS,
    _facts,
    _message,
    _telegram_data,
    _text,
)
from tests.test_inbound_message_fields import gateway as gateway
from tests.test_inbound_message_fields import private_home as private_home
from tests.test_messaging_server import _mock_response, _tools

pytestmark = pytest.mark.real_auth

DATA = "ap:sc-0123456789abcdef"
BUTTONS = [{"text": "Approve", "data": DATA}]
ROW = [{"text": "Approve", "callback_data": DATA}]
MARKUP = {"inline_keyboard": [ROW]}
ABSENT = object()


def _callback(**changes):
    value = {
        "id": "tap_1",
        "from": {"id": 202, "first_name": "Button", "last_name": "Sender",
                 "username": "sender", "is_bot": False},
        "message": {"message_id": 41, "date": 1700000000,
                    "chat": {"id": 101, "type": "private", "title": "Conversation"},
                    "from": {"id": 303, "is_bot": True}},
        "data": DATA,
    }
    value.update(changes)
    return value


def _update(callback=None, update_id=100):
    return {"update_id": update_id, "callback_query": callback or _callback()}


@contextmanager
def _adapter(updates=(), *, answer_error=None, repeats=1):
    calls, events = [], []
    polls = []

    def response(request):
        method = request.url.path.rsplit("/", 1)[-1]
        body = json.loads(request.content)
        calls.append((method, body, request.extensions.get("timeout")))
        if method == "getUpdates":
            polls.append(body.get("offset"))
            result = list(updates) if len(polls) <= repeats else []
        elif method == "answerCallbackQuery":
            events.append("answer")
            if isinstance(answer_error, TelegramError):
                return httpx.Response(200, json={"ok": False, "description": "synthetic failure"})
            if answer_error is not None:
                raise answer_error
            result = True
        elif method == "editMessageReplyMarkup":
            result = True
        elif method == "sendMessage":
            result = {"message_id": 51, "date": 1700000000, "chat": {"id": 101}}
        else:
            raise AssertionError("unexpected adapter request")
        return httpx.Response(200, json={"ok": True, "result": result})

    adapter = TelegramAdapter("fixture")
    adapter._client.close()
    adapter._client = httpx.Client(transport=httpx.MockTransport(response))
    try:
        yield SimpleNamespace(adapter=adapter, calls=calls, events=events, offsets=polls)
    finally:
        adapter.close()


def _poll(adapter):
    try:
        return adapter.get_updates()
    except Exception as error:
        raise AssertionError("poll raised " + type(error).__name__) from error


def _answers(state):
    return [body for method, body, _ in state.calls if method == "answerCallbackQuery"]


@pytest.fixture
def outbound(gateway):
    gateway.agents.set_token("sample", "telegram", "fixture")
    calls = []

    def sent(*args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(message_id=str(500 + len(calls)))

    with patch.object(TelegramAdapter, "send_message", side_effect=sent) as mock:
        yield SimpleNamespace(mock=mock, calls=calls)


def _post(gateway, buttons=ABSENT, **changes):
    body = {"agent_name": "sample", "platform": "telegram", "chat_id": "101",
            "content": "approval card"}
    if buttons is not ABSENT:
        body["buttons"] = buttons
    body.update(changes)
    return _signed_post(gateway, "/broker/send", body)


def _signed_post(gateway, path, body):
    headers = build_internal_auth_headers(
        gateway.agents.get_signing_key("sample"), agent_name="sample", method="POST", path=path
    )
    try:
        return gateway.client.post(path, json=body, headers=headers)
    except Exception as error:
        raise AssertionError("send route raised " + type(error).__name__) from error


def _read(gateway, message_id="cb-tap_1"):
    try:
        return _text(gateway, message_id=message_id)
    except Exception as error:
        raise AssertionError("text route raised " + type(error).__name__) from error


@contextmanager
def _incoming(gateway, monkeypatch, callback=None, *, repeats=1):
    gateway.agents.set_primary_user("202", display_name="Sender")
    gateway.agents.approve_user("sample", "202", display_name="Sender")
    session = SimpleNamespace(state=SessionState.CONNECTED, send=AsyncMock(return_value=True))
    gateway.broker.register_streaming("sample", session)
    monkeypatch.setattr(gateway.broker, "_start_typing", AsyncMock())
    pending = []
    monkeypatch.setattr(pollers, "_deliver_in_background", lambda coro, prefix: pending.append(coro))
    with _adapter([_update(callback)], repeats=repeats) as state:
        poller = pollers.BrokerTelegramPoller(state.adapter, "sample", gateway.broker)
        try:
            yield SimpleNamespace(poller=poller, pending=pending, session=session, http=state)
        finally:
            for coroutine in pending:
                coroutine.close()
            poller._shutdown_watchdog()


async def _deliver(state):
    await state.poller._poll_once()
    assert len(state.pending) == 1
    pending = list(state.pending)
    state.pending.clear()
    await asyncio.gather(*pending)


def test_poll_requests_messages_and_callback_queries():
    with _adapter() as state:
        assert _poll(state.adapter) == []
        assert state.calls[0][1]["allowed_updates"] == ["message", "callback_query"]


@pytest.mark.parametrize("is_bot", [False, True])
def test_callback_is_answered_before_mapping_and_keeps_tapper_facts(monkeypatch, is_bot):
    callback = _callback()
    callback["from"]["is_bot"] = is_bot
    with _adapter([_update(callback)]) as state:
        original = state.adapter._message_from_update

        def mapping(update):
            state.events.append("map")
            return original(update)

        monkeypatch.setattr(state.adapter, "_message_from_update", mapping)
        before = datetime.now(timezone.utc)
        messages = _poll(state.adapter)
        after = datetime.now(timezone.utc)
        assert state.events == ["answer", "map"]
        assert _answers(state) == [{"callback_query_id": "tap_1", "text": "Selected"}]
        answer = next(call for call in state.calls if call[0] == "answerCallbackQuery")
        assert answer[2] == {"connect": 5, "read": 5, "write": 5, "pool": 5}
        assert len(messages) == 1
        message = messages[0]
        assert message.platform.value == "telegram" and message.chat_id == "101"
        assert message.message_id == "cb-tap_1" and message.reply_to == "41"
        assert message.content == "Button tapped: " + DATA and message.sender == "Button Sender"
        assert before <= message.timestamp <= after and message.timestamp.tzinfo == timezone.utc
        assert message.metadata == {
            "sender_id": "202", "username": "sender", "chat_type": "private",
            "chat_title": "Conversation", "entities": [],
            **_facts(was_bot=is_bot, bot_id="202" if is_bot else None, reply_to="41",
                     reply_to_sender_id="303", platform_ts=None),
            "button_data": DATA, "button_message_id": "41",
        }


def test_inaccessible_card_keeps_a_null_reply_sender():
    callback = _callback()
    callback["message"].pop("from")
    callback["message"]["date"] = 0
    with _adapter([_update(callback)]) as state:
        messages = _poll(state.adapter)
        assert len(messages) == 1
        assert messages[0].metadata["reply_to_sender_id"] is None
        assert messages[0].reply_to == "41"
        assert _answers(state) == [{"callback_query_id": "tap_1", "text": "Selected"}]


@pytest.mark.parametrize("kind", [
    "inline", "game", "data-space", "data-slash", "data-long", "data-unicode",
    "data-non-string", "no-from", "from-string", "from-none", "from-float", "from-bool",
    "id-space", "id-long", "id-unicode",
])
def test_invalid_callback_is_answered_and_dropped(capsys, kind):
    callback = _callback()
    if kind == "inline":
        callback.pop("message")
        callback["inline_message_id"] = "inline"
    elif kind == "game":
        callback.pop("data")
        callback["game_short_name"] = "game"
    elif kind.startswith("data-"):
        callback["data"] = {"data-space": "a b", "data-slash": "/stop", "data-long": "a" * 65,
                            "data-unicode": "é", "data-non-string": 1}[kind]
    elif kind == "no-from":
        callback.pop("from")
    elif kind.startswith("from-"):
        callback["from"]["id"] = {"from-string": "202", "from-none": None,
                                   "from-float": 202.0, "from-bool": True}[kind]
    else:
        callback["id"] = {"id-space": "a b", "id-long": "a" * 129, "id-unicode": "é"}[kind]
    with _adapter([_update(callback)]) as state:
        assert _poll(state.adapter) == []
        assert _answers(state) == [{"callback_query_id": callback["id"]}]
        assert capsys.readouterr().err == ""


@pytest.mark.parametrize("failure", [TelegramError("synthetic"), Exception("synthetic")],
                         ids=["platform-error", "ordinary-error"])
def test_answer_failure_keeps_callback_batch_and_cursor(capsys, failure):
    updates = [_update(), {"update_id": 101, "message": _telegram_data(message_id=42)}]
    with _adapter(updates, answer_error=failure) as state:
        assert [message.message_id for message in _poll(state.adapter)] == ["cb-tap_1", "42"]
        assert _answers(state) == [{"callback_query_id": "tap_1", "text": "Selected"}]
        assert _poll(state.adapter) == []
        assert state.offsets == [None, 102]
        assert capsys.readouterr().err.splitlines() == [type(failure).__name__]


def test_mixed_update_batch_keeps_order_and_cursor():
    updates = [{"update_id": 10, "message": _telegram_data(message_id=40)},
               _update(update_id=11),
               {"update_id": 12, "message": _telegram_data(message_id=42)}]
    with _adapter(updates) as state:
        assert [message.message_id for message in _poll(state.adapter)] == ["40", "cb-tap_1", "42"]
        assert _poll(state.adapter) == []
        assert state.offsets == [None, 13]
        assert len(_answers(state)) == 1


@pytest.mark.parametrize("present", [False, True])
def test_adapter_sends_optional_reply_markup(present):
    with _adapter() as state:
        options = {"reply_markup": MARKUP} if present else {}
        message = state.adapter.send_message("101", "card", **options)
        assert message.message_id == "51"
        body = state.calls[0][1]
        if present:
            assert body.get("reply_markup") == MARKUP
        else:
            assert "reply_markup" not in body


@pytest.mark.parametrize("present", [False, True])
def test_send_tool_forwards_buttons_and_empty_as_null(present):
    server = create_server(agent_name="sample")
    value = json.dumps(BUTTONS) if present else ""
    with patch("urllib.request.urlopen", return_value=_mock_response({"sent": True})) as opened:
        result = _tools(server)["send"](chat_id="101", platform="telegram", text="card", buttons=value)
    assert json.loads(result)["sent"] is True
    body = json.loads(opened.call_args.args[0].data)
    assert "buttons" in body
    assert body["buttons"] == (value or None)
    for name, function in _tools(server).items():
        if name != "send":
            import inspect

            assert "buttons" not in inspect.signature(function).parameters


@pytest.mark.parametrize("style", [None, "success", "danger", "primary"])
@pytest.mark.parametrize("raw_list", [False, True])
@pytest.mark.parametrize("shape", ["single", "boundary", "three"])
def test_send_route_builds_one_button_row_and_keeps_style(gateway, outbound, style, raw_list, shape):
    buttons, row = copy.deepcopy(BUTTONS), copy.deepcopy(ROW)
    if shape == "boundary":
        buttons[0] = {"text": "é" * 64, "data": "a" * 64}
        row[0] = {"text": "é" * 64, "callback_data": "a" * 64}
    if shape == "three":
        for index in (1, 2):
            buttons.append({"text": "Option", "data": f"option_{index}"})
            row.append({"text": "Option", "callback_data": f"option_{index}"})
    if style is not None:
        buttons[0]["style"] = row[0]["style"] = style
    response = _post(gateway, buttons if raw_list else json.dumps(buttons), parse_mode="HTML")
    assert response.status_code == 200, response.text
    assert response.json()["message_id"] == "501"
    assert outbound.calls[0][1].get("reply_markup") == {"inline_keyboard": [row]}


@pytest.mark.asyncio
@pytest.mark.parametrize("branch", ["explicit", "markdown", "plain", "unquoted"])
async def test_every_text_fallback_and_unquoted_retry_keeps_buttons(gateway, outbound, branch):
    calls = []

    def send(*args, **kwargs):
        calls.append(kwargs)
        if branch == "plain" and len(calls) == 1:
            raise TelegramError("synthetic formatting failure")
        if branch == "unquoted" and kwargs.get("reply_parameters") is not None:
            raise TelegramError("synthetic quote rejection")
        return SimpleNamespace(message_id="501")

    outbound.mock.side_effect = send
    try:
        result = await gateway.broker.send_callback(
            "sample", "telegram", "101", "card", buttons=json.dumps(BUTTONS),
            parse_mode="HTML" if branch in ("explicit", "unquoted") else "",
            reply_to="41" if branch == "unquoted" else "",
            quote="excerpt" if branch == "unquoted" else "",
        )
    except Exception as error:
        raise AssertionError("send callback raised " + type(error).__name__) from error
    assert result["message_id"] == "501"
    assert len(calls) == (2 if branch in ("plain", "unquoted") else 1)
    assert all(call.get("reply_markup") == MARKUP for call in calls)
    if branch == "plain":
        assert calls[0]["parse_mode"] == "MarkdownV2" and "parse_mode" not in calls[1]
    if branch == "unquoted":
        assert calls[0]["reply_parameters"] == {"message_id": 41, "quote": "excerpt"}
        assert calls[1]["reply_parameters"] is None and calls[1]["reply_to_message_id"] == 41


@pytest.mark.parametrize("buttons", [
    "not json", "{}", "[]",
    json.dumps([{"text": "Option", "data": f"option_{index}"} for index in range(4)]),
    json.dumps([{"text": "Approve", "data": "one", "url": "unused"}]),
    json.dumps([{"text": "", "data": "one"}]),
    json.dumps([{"text": "  ", "data": "one"}]),
    json.dumps([{"text": None, "data": "one"}]),
    json.dumps([{"text": "a\nb", "data": "one"}]),
    json.dumps([{"text": "a\x00b", "data": "one"}]),
    json.dumps([{"text": "a\x7fb", "data": "one"}]),
    json.dumps([{"text": "a" * 65, "data": "one"}]),
    json.dumps([{"text": "Approve", "data": "a b"}]),
    json.dumps([{"text": "Approve", "data": "a" * 65}]),
    json.dumps([{"text": "Approve", "data": ""}]),
    json.dumps([{"text": "Approve", "data": 1}]),
    json.dumps(BUTTONS * 2),
    json.dumps([{"text": "Approve", "data": "one", "style": "unknown"}]),
    json.dumps([{"text": "Approve", "data": "one", "style": None}]),
    json.dumps(["not an object"]),
], ids=["json", "object", "empty", "too-many", "unknown-key", "empty-text", "blank-text",
        "text-type", "newline", "nul", "delete-control", "long-text", "data-space", "long-data",
        "empty-data", "data-type", "duplicate", "style", "null-style", "item-type"])
def test_invalid_button_rows_are_refused_without_sending(gateway, outbound, buttons):
    response = _post(gateway, buttons)
    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith("invalid buttons: ")
    outbound.mock.assert_not_called()


@pytest.mark.parametrize("platform", ["slack", "discord"])
def test_buttons_are_refused_on_other_platforms(gateway, outbound, platform):
    response = _post(gateway, json.dumps(BUTTONS), platform=platform)
    assert response.status_code == 400, response.text
    assert response.json()["detail"] == "buttons are Telegram only"
    outbound.mock.assert_not_called()


def test_dedupe_distinguishes_buttons_and_preserves_plain_identity(gateway, outbound, monkeypatch):
    deliver = AsyncMock(wraps=gateway.broker.deliver_deduped)
    monkeypatch.setattr(gateway.broker, "deliver_deduped", deliver)
    other = [{"text": "Reject", "data": "reject"}]
    first = _post(gateway, json.dumps(BUTTONS))
    second = _post(gateway, json.dumps(other))
    repeated = _post(gateway, '[{"data":"' + DATA + '","text":"Approve"}]')
    plain = _post(gateway)
    assert [response.status_code for response in (first, second, repeated, plain)] == [200] * 4
    assert len(outbound.calls) == 3
    assert first.json()["message_id"] != second.json()["message_id"]
    assert repeated.json()["message_id"] == first.json()["message_id"]
    assert repeated.json().get("deduped") is True
    keys = [call.kwargs["key_extra"] for call in deliver.await_args_list]
    assert json.loads(keys[0])["btn"] == json.dumps(ROW, sort_keys=True, separators=(",", ":"))
    assert keys[0] == keys[2] and keys[0] != keys[1]
    assert keys[3] == ""


@pytest.mark.parametrize("present", [False, True])
def test_outbound_record_includes_buttons_only_when_present(gateway, outbound, present):
    response = _post(gateway, json.dumps(BUTTONS) if present else ABSENT)
    assert response.status_code == 200
    messages = gateway.app.state.conversation_store.get_history("sample-main")
    assert len(messages) == 1
    if present:
        assert messages[0].metadata.get("buttons") == ROW
    else:
        assert "buttons" not in messages[0].metadata


@pytest.mark.parametrize("fields", [
    {"button_data": DATA, "button_message_id": "41"}, {"button_data": DATA},
    {"button_message_id": "41"}, {}, {"button_data": None},
], ids=["both", "data", "card", "neither", "null-value"])
def test_text_route_returns_only_present_button_fields(gateway, fields):
    gateway.broker.remember_message_context(_message(metadata={**_facts(), **fields}))
    response = _read(gateway, "41")
    assert response.status_code == 200
    assert set(response.json()) == TEXT_KEYS | set(FIELDS) | set(fields)
    assert {key: response.json()[key] for key in fields} == fields
    assert {key: response.json()[key] for key in FIELDS} == _facts()


@pytest.mark.asyncio
async def test_callback_reaches_signed_text_and_threads_under_its_card(gateway, outbound, monkeypatch):
    gateway.broker.remember_outbound_message_context("sample", "41", platform="telegram", chat_id="101")
    with _incoming(gateway, monkeypatch) as state:
        await _deliver(state)
        state.session.send.assert_awaited_once()
        response = _read(gateway)
        assert response.status_code == 200, response.text
        body = response.json()
        assert body["text"] == "Button tapped: " + DATA
        assert {key: body[key] for key in FIELDS} == _facts(reply_to="41", reply_to_sender_id="303",
                                                           platform_ts=None)
        assert body["button_data"] == DATA and body["button_message_id"] == "41"
        prompt = state.session.send.await_args.args[0]
        assert "msg_id:cb-tap_1" in prompt and "thread_root_ts:41" in prompt
        result = _signed_post(gateway, "/broker/thread", {"agent_name": "sample",
                              "message_id": "cb-tap_1", "content": "reply"})
        assert result.status_code == 200, result.text
        assert outbound.calls[-1][1]["reply_to_message_id"] == 41
        assert _read(gateway, "41").status_code == 404


@pytest.mark.asyncio
async def test_redelivered_callback_preserves_every_text_field_and_store_time(gateway, monkeypatch):
    clock = [time.time()]
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=lambda: clock[0]))
    with _incoming(gateway, monkeypatch, repeats=2) as state:
        await _deliver(state)
        first = _read(gateway)
        assert first.status_code == 200, first.text
        first_body = first.json()
        assert type(first_body["stored_at"]) is float
        clock[0] += 30
        with _adapter([_update()]) as restarted:
            state.poller._adapter = restarted.adapter
            await _deliver(state)
        second = _read(gateway)
        assert second.status_code == 200
        assert second.json() == first_body
        count = gateway.store._db.execute(
            "SELECT COUNT(*) FROM message_contexts WHERE agent_name=? AND platform=? "
            "AND chat_id=? AND message_id=?", ("sample", "telegram", "101", "cb-tap_1"),
        ).fetchone()[0]
        assert count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["approve_x", "stop"])
async def test_callback_data_never_runs_owner_commands(gateway, monkeypatch, data):
    commands = []
    for name in ("_handle_stop_command", "_handle_voice_approval_command", "_handle_approval_command"):
        handler = AsyncMock(return_value=True)
        commands.append(handler)
        monkeypatch.setattr(gateway.broker, name, handler)
    with _incoming(gateway, monkeypatch, _callback(data=data)) as state:
        await _deliver(state)
        response = _read(gateway)
        assert response.status_code == 200
        assert response.json()["text"] == "Button tapped: " + data
        for handler in commands:
            handler.assert_not_awaited()



def _slow_answers(monkeypatch, adapter):
    clock = SimpleNamespace(now=100.0, slow=True, attempted=[])
    monkeypatch.setattr(telegram_module, "time",
                        SimpleNamespace(monotonic=lambda: clock.now), raising=False)

    def answer(callback_id, text=None):
        clock.attempted.append(callback_id)
        if clock.slow:
            clock.now += 5.0
            raise httpx.ReadTimeout("synthetic answer timeout")

    monkeypatch.setattr(adapter, "answer_callback_query", answer)
    return clock


def _twenty_taps():
    callbacks = [_callback(id=f"tap_{index}") for index in range(1, 21)]
    for index, callback in enumerate(callbacks, start=1):
        callback["message"]["message_id"] = index
    return [_update(callback, update_id=index)
            for index, callback in enumerate(callbacks, start=1)]


def test_callback_answer_budget_keeps_every_tap_and_counts_skips(monkeypatch, capsys):
    with _adapter(_twenty_taps()) as state:
        clock = _slow_answers(monkeypatch, state.adapter)
        messages = _poll(state.adapter)
        assert clock.attempted == ["tap_1", "tap_2"]
        assert [message.message_id for message in messages] == [f"cb-tap_{i}" for i in range(1, 21)]
        assert state.adapter._last_update_id == 20
        assert _poll(state.adapter) == []
        assert state.offsets == [None, 21]
    assert capsys.readouterr().err.splitlines() == [
        "ReadTimeout", "ReadTimeout", "callback answers skipped: 18",
    ]


def test_callback_answer_budget_resets_for_instant_answers(monkeypatch, capsys):
    updates = _twenty_taps()
    with _adapter(updates, repeats=2) as state:
        clock = _slow_answers(monkeypatch, state.adapter)
        assert len(_poll(state.adapter)) == 20
        assert clock.attempted == ["tap_1", "tap_2"]
        assert capsys.readouterr().err.splitlines()[-1] == "callback answers skipped: 18"
        for update in updates:
            update["callback_query"]["message"]["message_id"] += 20
        clock.slow = False
        clock.attempted.clear()
        messages = _poll(state.adapter)
        assert clock.attempted == [f"tap_{i}" for i in range(1, 21)]
        assert [message.message_id for message in messages] == [f"cb-tap_{i}" for i in range(1, 21)]
        assert state.offsets == [None, 21]
        assert capsys.readouterr().err == ""


def test_spent_answer_budget_preserves_mixed_batch_order(monkeypatch, capsys):
    updates = [{"update_id": 0, "message": _telegram_data(message_id=31)}]
    updates += _twenty_taps()
    updates += [{"update_id": 21, "message": _telegram_data(message_id=32)}]
    with _adapter(updates) as state:
        clock = _slow_answers(monkeypatch, state.adapter)
        messages = _poll(state.adapter)
        assert clock.attempted == ["tap_1", "tap_2"]
        assert [message.message_id for message in messages] == [
            "31", *[f"cb-tap_{i}" for i in range(1, 21)], "32",
        ]
        assert state.adapter._last_update_id == 21
        assert _poll(state.adapter) == []
        assert state.offsets == [None, 22]
    assert capsys.readouterr().err.splitlines() == [
        "ReadTimeout", "ReadTimeout", "callback answers skipped: 18",
    ]


@asynccontextmanager
async def _pending_login(gateway, monkeypatch, *, relay_mid="41"):
    gateway.agents.set_primary_user("202", display_name="Sender")
    gateway.agents.set_setting("PINKY_TMUX_AUTH_RELAY", "1")
    relay = AuthRelayCoordinator()
    sent = AsyncMock(return_value={"message_id": relay_mid})
    relay.configure(sent, gateway.agents.get_primary_user)
    monkeypatch.setattr(broker_module, "_auth_relay", relay)
    replies = AsyncMock()
    monkeypatch.setattr(gateway.broker, "_send_callback", replies)
    task = asyncio.create_task(relay.open("sample", "https://example.invalid/login", ttl_sec=30))
    try:
        for _ in range(200):
            if relay.has_pending("sample"):
                break
            await asyncio.sleep(0)
        assert relay.has_pending("sample")
        assert relay.pending_relay_mid("sample") == relay_mid
        assert relay.pending_relay_chat_id("sample") == "202"
        assert sent.await_args.args[2] == "202"
        yield SimpleNamespace(relay=relay, task=task, replies=replies)
    finally:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
@pytest.mark.parametrize("data,card_id", [(DATA, 41), ("abcdefghijkl", 41), (DATA, 42)])
async def test_group_tap_routes_while_private_login_relay_waits(
    gateway, monkeypatch, data, card_id
):
    callback = _callback(data=data)
    callback["message"]["message_id"] = card_id
    callback["message"]["chat"] = {"id": -101, "type": "supergroup", "title": "Group"}
    gateway.agents.approve_user("sample", "-101", display_name="Group")
    with _incoming(gateway, monkeypatch, callback) as state:
        async with _pending_login(gateway, monkeypatch) as pending:
            await _deliver(state)
            response = _text(gateway, chat_id="-101", message_id="cb-tap_1")
            assert response.status_code == 200
            assert response.json()["button_data"] == data
            assert response.json()["button_message_id"] == str(card_id)
            state.session.send.assert_awaited_once()
            assert data in state.session.send.await_args.args[0]
            pending.replies.assert_not_awaited()
            assert pending.relay.has_pending("sample")
            assert not pending.task.done()


@pytest.mark.asyncio
async def test_group_quote_with_colliding_number_routes_during_private_login(gateway, monkeypatch):
    gateway.agents.approve_user("sample", "-101", display_name="Group")
    with _incoming(gateway, monkeypatch) as state:
        async with _pending_login(gateway, monkeypatch) as pending:
            await gateway.broker.handle_inbound(_message(
                chat_id="-101", is_group=True, reply_to="41", content="abcdefghijkl",
            ))
            response = _text(gateway, chat_id="-101", message_id="41")
            assert response.status_code == 200
            state.session.send.assert_awaited_once()
            pending.replies.assert_not_awaited()
            assert pending.relay.has_pending("sample")
            assert not pending.task.done()


@pytest.mark.asyncio
async def test_private_quote_submits_code_to_recorded_login_chat(gateway, monkeypatch):
    async with _pending_login(gateway, monkeypatch) as pending:
        handled = await gateway.broker._handle_auth_code_reply(_message(
            chat_id="202", reply_to="41", content="abcdefghijkl",
        ))
        assert handled is True
        assert await asyncio.wait_for(pending.task, timeout=1) == "abcdefghijkl"
        pending.replies.assert_awaited_once()
        assert pending.replies.await_args.args[2] == "202"
        assert pending.relay.pending_relay_chat_id("sample") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("recorded_chat", [None, ""])
async def test_empty_recorded_login_chat_never_matches(gateway, monkeypatch, recorded_chat):
    async with _pending_login(gateway, monkeypatch) as pending:
        monkeypatch.setattr(pending.relay, "pending_relay_chat_id", lambda agent: recorded_chat)
        handled = await gateway.broker._handle_auth_code_reply(_message(
            chat_id=recorded_chat, sender_id="202", reply_to="41", content="abcdefghijkl",
        ))
        assert handled is False
        pending.replies.assert_not_awaited()
        assert not pending.task.done()


@pytest.mark.asyncio
async def test_empty_recorded_login_message_never_matches(gateway, monkeypatch):
    async with _pending_login(gateway, monkeypatch, relay_mid="") as pending:
        handled = await gateway.broker._handle_auth_code_reply(_message(
            chat_id="202", reply_to="", content="abcdefghijkl",
        ))
        assert handled is False
        pending.replies.assert_not_awaited()
        assert not pending.task.done()


@pytest.mark.asyncio
@pytest.mark.parametrize("button_data", [None, "", DATA])
async def test_tap_presence_bypasses_login_reader_before_pending_check(
    gateway, monkeypatch, button_data
):
    async with _pending_login(gateway, monkeypatch) as pending:
        pending_check = Mock(wraps=pending.relay.has_pending)
        monkeypatch.setattr(pending.relay, "has_pending", pending_check)
        handled = await gateway.broker._handle_auth_code_reply(_message(
            chat_id="202", reply_to="41", content="abcdefghijkl",
            metadata={"button_data": button_data},
        ))
        assert handled is False
        pending_check.assert_not_called()
        pending.replies.assert_not_awaited()
        assert not pending.task.done()
