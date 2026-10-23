"""Managed Slack cards accept one allowed choice without affecting purchase approvals."""

import asyncio
import copy
import json
import sqlite3
import sys
import threading
import time
import types
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from pinky_daemon import broker as broker_module
from pinky_daemon import pollers
from pinky_daemon.auth_relay import AuthRelayCoordinator
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.message_context_store import MessageContextStore
from pinky_daemon.pollers import BrokerSlackPoller
from pinky_daemon.store_restore import STORE_SCHEMA_SENTINELS
from pinky_daemon.transport_state import SessionState
from pinky_messaging.server import create_server
from pinky_outreach import slack
from pinky_outreach.slack import SlackAdapter, SlackError
from tests.test_inbound_message_fields import FIELDS, TEXT_KEYS, _text
from tests.test_inbound_message_fields import gateway as gateway
from tests.test_inbound_message_fields import private_home as private_home
from tests.test_messaging_server import _tools
from tests.test_telegram_buttons import _signed_post

pytestmark = pytest.mark.real_auth

DATA = "ap:opaque-option"
CARD = "1700000000.000100"
ACTION = "1700000001.000200"
URL = "https://hooks.slack.com/actions/opaque-capability"
CONTENT = "Choose an option"
ROW = [{"text": "Approve", "callback_data": DATA}]
BUTTONS = [{"text": "Approve", "data": DATA}]
ABSENT = object()


def _blocks(content=CONTENT, row=None):
    elements = []
    for item in row or ROW:
        element = {"type": "button", "action_id": "pinky_btn:" + item["callback_data"],
                   "value": item["callback_data"],
                   "text": {"type": "plain_text", "text": item["text"]}}
        if "style" in item:
            element["style"] = {"success": "primary", "danger": "danger",
                                "primary": "primary"}[item["style"]]
        elements.append(element)
    return [{"type": "section", "block_id": "pinky_btn_text",
             "text": {"type": "mrkdwn", "text": content}},
            {"type": "actions", "block_id": "pinky_btn", "elements": elements}]


def _chosen(label="Approve"):
    return {"type": "context", "block_id": "pinky_btn_chosen",
            "elements": [{"type": "plain_text",
                          "text": "Selected: " + label if label is not None else "Selected"}]}


def _payload(*, channel="CCHANNEL", user="UCLICKER", ts=CARD, action_ts=ACTION, data=DATA):
    return {"type": "block_actions", "user": {"id": user}, "channel": {"id": channel},
            "container": {"type": "message", "is_ephemeral": False,
                          "channel_id": channel, "message_ts": ts},
            "message": {"ts": ts, "user": "UBOT", "bot_id": "BBOT", "text": CONTENT,
                        "blocks": _blocks(row=[{"text": "Approve", "callback_data": data}])},
            "actions": [{"type": "button", "action_id": "pinky_btn:" + data, "value": data,
                         "block_id": "pinky_btn", "action_ts": action_ts}],
            "response_url": URL}


def _required(owner, name):
    method = getattr(owner, name, None)
    assert callable(method), "missing button contract: " + name
    return method


def _state(*, store=None, registry=None, broker=None, event_callback=None):
    statuses = {"UCLICKER": "approved", "CCHANNEL": "approved"}
    if registry is None:
        registry = Mock()
        registry.get_user_status.side_effect = lambda agent, key: statuses.get(key)
        registry.get_primary_user.return_value = {"chat_id": "UOWNER"}
        registry.get_setting.return_value = ""
    if broker is None:
        broker = MessageBroker(registry, Mock(), message_context_store=store)
        broker.handle_inbound = AsyncMock()
    adapter = Mock()
    adapter.bot_token = "fixture"
    adapter.get_bot_info.return_value = {"user_id": "UBOT", "bot_id": "BBOT"}
    poller = BrokerSlackPoller(adapter, "sample", broker, registry, app_token="fixture",
                              event_callback=event_callback)
    poller._bot_user_id, poller._bot_id = "UBOT", "BBOT"
    poller._resolve_user_name = AsyncMock(return_value="Display")
    poller._resolve_channel_title = AsyncMock(return_value="Conversation")
    return SimpleNamespace(poller=poller, adapter=adapter, broker=broker, registry=registry,
                           statuses=statuses)


async def _drain():
    while pollers._DELIVERY_TASKS:
        await asyncio.gather(*tuple(pollers._DELIVERY_TASKS))
        await asyncio.sleep(0)


async def _tap(state, payload=None):
    await state.poller._handle_interactive(_payload() if payload is None else payload)
    await _drain()


@pytest.fixture
def outbound(gateway, monkeypatch):
    gateway.agents.set_token("sample", "slack", "fixture")
    send = Mock(side_effect=lambda *a, **k: SimpleNamespace(message_id=str(500 + send.call_count)))
    monkeypatch.setattr(SlackAdapter, "send_message", send)
    return send


def _post(gateway, buttons=ABSENT, **changes):
    body = {"agent_name": "sample", "platform": "slack", "chat_id": "CCHANNEL",
            "content": CONTENT}
    if buttons is not ABSENT:
        body["buttons"] = buttons
    body.update(changes)
    return _signed_post(gateway, "/broker/send", body)


@pytest.mark.parametrize("shape", ["single", "boundary", "three"])
@pytest.mark.parametrize("style", [None, "success", "danger", "primary"])
def test_slack_send_renders_text_section_and_one_actions_row(gateway, outbound, shape, style):
    buttons = copy.deepcopy(BUTTONS)
    if shape == "boundary":
        buttons = [{"text": "é" * 64, "data": "a" * 64}]
    elif shape == "three":
        buttons += [{"text": "Reject", "data": "reject"}, {"text": "Later", "data": "later"}]
    if style is not None:
        for item in buttons:
            item["style"] = style
    response = _post(gateway, buttons, reply_to="1600000000.1", disable_link_preview=True)
    assert response.status_code == 200, response.text
    expected = [{"text": b["text"], "callback_data": b["data"],
                 **({"style": style} if style else {})} for b in buttons]
    args, kwargs = outbound.call_args
    assert args == ("CCHANNEL", CONTENT)
    assert kwargs["blocks"] == _blocks(row=expected)
    assert kwargs["thread_ts"] == "1600000000.1"


def test_slack_style_mapping_success_and_primary_render_primary():
    render = _required(slack, "button_card_blocks")
    success = render(CONTENT, [{**ROW[0], "style": "success"}])
    primary = render(CONTENT, [{**ROW[0], "style": "primary"}])
    assert success == primary == _blocks(row=[{**ROW[0], "style": "primary"}])


INVALID_ROWS = [
    "not json", "{}", "[]",
    json.dumps([{"text": "Option", "data": f"option_{i}"} for i in range(4)]),
    json.dumps([{"text": "Approve", "data": "one", "url": "unused"}]),
    *[json.dumps([{"text": text, "data": "one"}])
      for text in ("", "  ", None, "a\nb", "a\x00b", "a\x7fb", "a" * 65)],
    *[json.dumps([{"text": "Approve", "data": data}]) for data in ("a b", "a" * 65, "", 1)],
    json.dumps(BUTTONS * 2),
    json.dumps([{"text": "Approve", "data": "one", "style": "unknown"}]),
    json.dumps([{"text": "Approve", "data": "one", "style": None}]),
    json.dumps(["not an object"]),
]


@pytest.mark.parametrize("buttons", INVALID_ROWS)
def test_slack_invalid_button_rows_are_refused_without_sending(gateway, outbound, buttons):
    response = _post(gateway, buttons)
    assert response.status_code == 400
    assert response.json()["detail"].startswith("invalid buttons: ")
    outbound.assert_not_called()


def test_slack_reserved_chosen_marker_is_refused(gateway, outbound):
    response = _post(gateway, [{"text": "Choose", "data": "__chosen__"}])
    assert response.status_code == 400
    assert "reserved" in response.json()["detail"]
    outbound.assert_not_called()


@pytest.mark.parametrize("blocks", ["not json", "[]", [{"type": "divider"}]])
def test_slack_buttons_with_blocks_are_refused_without_sending(gateway, outbound, blocks):
    response = _post(gateway, BUTTONS, blocks=blocks)
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid buttons: buttons cannot be combined with blocks"
    outbound.assert_not_called()


@pytest.mark.parametrize("kind", ["action", "button", "chosen", "text"])
def test_explicit_blocks_refuse_reserved_managed_identifiers(gateway, outbound, kind):
    block = {"type": "actions", "elements": [{"type": "button", "action_id": "custom"}]}
    if kind == "action":
        block["elements"][0]["action_id"] = "pinky_btn:opaque"
    else:
        block["block_id"] = {"button": "pinky_btn", "chosen": "pinky_btn_chosen",
                             "text": "pinky_btn_text"}[kind]
    response = _post(gateway, blocks=[block])
    assert response.status_code == 400
    assert "reserved" in response.json()["detail"]
    outbound.assert_not_called()


def test_explicit_custom_blocks_still_send_without_managed_buttons(gateway, outbound):
    blocks = [{"type": "actions", "elements": [{"type": "button", "action_id": "custom"}]}]
    assert _post(gateway, blocks=blocks).status_code == 200
    assert outbound.call_args.kwargs["blocks"] == blocks


@pytest.mark.parametrize("depth", [400, 1200])
def test_explicit_blocks_depth_preserves_existing_lenient_fallback(gateway, outbound, depth):
    raw = "[" * depth + '"plain"' + "]" * depth
    response = _post(gateway, blocks=raw)
    assert response.status_code == 200, response.text
    outbound.assert_called_once()
    assert outbound.call_args.args == ("CCHANNEL", CONTENT)
    if depth == 1200:
        assert outbound.call_args.kwargs["blocks"] is None
    else:
        assert outbound.call_args.kwargs["blocks"] == json.loads(raw)


def test_explicit_blocks_depth_still_refuses_a_reserved_id(gateway, outbound):
    raw = "[" * 400 + '{"action_id":"pinky_btn:opaque"}' + "]" * 400
    response = _post(gateway, blocks=raw)
    assert response.status_code == 400, response.text
    assert "reserved" in response.json()["detail"]
    outbound.assert_not_called()


def test_explicit_blocks_walk_fault_refuses_without_sending(gateway, outbound, monkeypatch):
    fault = Mock(side_effect=RuntimeError("private layout or capability"))
    monkeypatch.setattr(slack, "has_reserved_button_ids", fault, raising=False)
    response = _post(gateway, blocks=[{"type": "divider"}])
    assert response.status_code == 400, response.text
    assert response.json()["detail"].startswith("invalid buttons: ")
    assert "private" not in response.text and "capability" not in response.text
    outbound.assert_not_called()


def test_slack_buttons_text_limit_refuses_3001_and_accepts_3000(gateway, outbound):
    over = _post(gateway, BUTTONS, content="a" * 3001)
    assert over.status_code == 400
    assert over.json()["detail"] == "invalid buttons: text exceeds 3000 characters with Slack buttons"
    outbound.assert_not_called()
    assert _post(gateway, BUTTONS, content="a" * 3000).status_code == 200
    assert outbound.call_args.kwargs["blocks"][0]["text"]["text"] == "a" * 3000


def test_slack_dedupe_distinguishes_buttons_and_preserves_plain_identity(
    gateway, outbound, monkeypatch,
):
    deliver = AsyncMock(wraps=gateway.broker.deliver_deduped)
    monkeypatch.setattr(gateway.broker, "deliver_deduped", deliver)
    other = [{"text": "Reject", "data": "reject"}]
    responses = [_post(gateway, b) for b in (BUTTONS, other, json.dumps(BUTTONS), ABSENT)]
    assert [r.status_code for r in responses] == [200] * 4
    assert outbound.call_count == 3
    assert responses[0].json()["message_id"] != responses[1].json()["message_id"]
    assert responses[2].json()["message_id"] == responses[0].json()["message_id"]
    assert responses[2].json().get("deduped") is True
    keys = [c.kwargs["key_extra"] for c in deliver.await_args_list]
    assert json.loads(keys[0])["btn"] == json.dumps(ROW, sort_keys=True, separators=(",", ":"))
    assert keys[0] == keys[2] and keys[0] != keys[1] and keys[3] == ""


@pytest.mark.parametrize("present", [True, False])
def test_slack_outbound_record_includes_buttons_only_when_present(gateway, outbound, present):
    assert _post(gateway, BUTTONS if present else ABSENT).status_code == 200
    records = gateway.app.state.conversation_store.get_history("sample-main")
    assert len(records) == 1
    assert ("buttons" in records[0].metadata) is present
    if present:
        assert records[0].metadata["buttons"] == ROW


def test_buttons_are_refused_on_platforms_without_support(gateway, outbound):
    response = _post(gateway, BUTTONS, platform="discord")
    assert response.status_code == 400
    assert response.json()["detail"] == "buttons are Telegram and Slack only"
    outbound.assert_not_called()


@pytest.mark.parametrize("ok", [True, False])
def test_slack_adapter_update_message_posts_chat_update_with_timeout(ok):
    calls = []

    def response(request):
        calls.append(request)
        return httpx.Response(200, json={"ok": ok, "error": "synthetic"})

    adapter = SlackAdapter("fixture")
    adapter._client.close()
    adapter._client = httpx.Client(base_url=SlackAdapter.BASE_URL,
                                 transport=httpx.MockTransport(response))
    try:
        update = _required(adapter, "update_message")
        if ok:
            update("CCHANNEL", CARD, CONTENT, [_chosen()], timeout=0.5)
        else:
            with pytest.raises(SlackError):
                update("CCHANNEL", CARD, CONTENT, [_chosen()], timeout=0.5)
        assert len(calls) == 1
        assert calls[0].url.path == "/api/chat.update"
        assert json.loads(calls[0].content) == {
            "channel": "CCHANNEL", "ts": CARD, "text": CONTENT, "blocks": [_chosen()],
        }
        assert calls[0].extensions["timeout"] == dict.fromkeys(("connect", "read", "write", "pool"), 0.5)
    finally:
        adapter.close()


def test_send_tool_doc_names_slack_buttons():
    doc = _tools(create_server(agent_name="sample"))["send"].__doc__
    assert "buttons (Telegram and Slack)" in doc
    assert "3000" in doc and "cannot" in doc and "blocks" in doc
    assert "success and primary both render as Slack's primary" in doc


async def _socket(state, monkeypatch, events):
    class Client:
        def __init__(self, **kwargs):
            self.socket_mode_request_listeners = []

        async def connect(self):
            pass

        async def send_socket_mode_response(self, response):
            events.append("ack")

    for name in ("slack_sdk", "slack_sdk.socket_mode", "slack_sdk.web"):
        monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
    for name, attribute, value in (
        ("slack_sdk.socket_mode.aiohttp", "SocketModeClient", Client),
        ("slack_sdk.socket_mode.response", "SocketModeResponse",
         lambda **kwargs: SimpleNamespace(**kwargs)),
        ("slack_sdk.web.async_client", "AsyncWebClient", lambda **kwargs: Mock()),
    ):
        module = types.ModuleType(name)
        setattr(module, attribute, value)
        monkeypatch.setitem(sys.modules, name, module)
    await state.poller.start()
    return state.poller._client.socket_mode_request_listeners[0]


@pytest.mark.asyncio
async def test_button_click_is_acked_before_any_slack_call(monkeypatch):
    state = _state()
    events = []
    listener = await _socket(state, monkeypatch, events)
    state.adapter.update_message.side_effect = lambda *a, **k: events.append("update")
    state.poller._resolve_user_name.side_effect = lambda uid: events.append("resolve") or uid
    state.broker.handle_inbound.side_effect = lambda msg: events.append("inbound")
    await listener(state.poller._client, SimpleNamespace(
        type="interactive", envelope_id="opaque-envelope", payload=_payload(),
    ))
    await _drain()
    assert events[0] == "ack" and events.count("ack") == 1
    assert "resolve" in events and "inbound" in events and "update" in events
    assert state.poller.poll_count == 1


@pytest.mark.asyncio
async def test_button_click_becomes_one_inbound_message_with_tap_fields():
    callback = AsyncMock()
    state = _state(event_callback=callback)
    await _tap(state)
    state.broker.handle_inbound.assert_awaited_once()
    msg = state.broker.handle_inbound.await_args.args[0]
    assert msg.platform == "slack" and msg.chat_id == "CCHANNEL"
    assert msg.sender_id == "UCLICKER" and msg.sender_name == "Display"
    assert msg.agent_name == "sample" and msg.content == "Button tapped: " + DATA
    assert msg.message_id == "act-" + ACTION and msg.reply_to == CARD
    assert msg.chat_title == "Conversation" and msg.is_group is True
    assert msg.attachments == []
    assert msg.metadata == {
        "user_id": "UCLICKER", "bot_id": None, "was_bot": False, "forwarded": None,
        "via_bot_id": None, "sender_business_bot_id": None, "reply_to": CARD,
        "reply_to_sender_id": "UBOT", "platform_ts": None, "channel_type": "", "thread_ts": CARD,
        "button_data": DATA, "button_message_id": CARD,
    }
    callback.assert_awaited_once_with(platform="slack", chat_id="CCHANNEL", sender="Display",
                                     content="Button tapped: " + DATA)


@pytest.mark.asyncio
async def test_card_in_a_thread_threads_the_tap_into_that_thread():
    state = _state()
    payload = _payload()
    payload["message"]["thread_ts"] = "1600000000.000100"
    await _tap(state, payload)
    msg = state.broker.handle_inbound.await_args.args[0]
    assert msg.reply_to == msg.metadata["thread_ts"] == "1600000000.000100"
    assert msg.metadata["reply_to"] == CARD


def _gateway_incoming(gateway, monkeypatch):
    gateway.agents.set_primary_user("UOWNER", display_name="Owner")
    gateway.agents.approve_user("sample", "CCHANNEL")
    gateway.agents.approve_user("sample", "UCLICKER")
    session = SimpleNamespace(state=SessionState.CONNECTED, send=AsyncMock(return_value=True))
    gateway.broker.register_streaming("sample", session)
    monkeypatch.setattr(gateway.broker, "_start_typing", AsyncMock())
    state = _state(broker=gateway.broker, registry=gateway.agents)
    state.session = session
    return state


@pytest.mark.asyncio
async def test_tap_reaches_signed_text_and_threads_under_its_card(gateway, outbound, monkeypatch):
    gateway.broker.remember_outbound_message_context(
        "sample", CARD, platform="slack", chat_id="CCHANNEL",
    )
    state = _gateway_incoming(gateway, monkeypatch)
    await _tap(state)
    response = _text(gateway, platform="slack", chat_id="CCHANNEL", message_id="act-" + ACTION)
    assert response.status_code == 200, response.text
    body = response.json()
    assert set(body) == TEXT_KEYS | set(FIELDS) | {"button_data", "button_message_id"}
    assert body["button_data"] == DATA and body["button_message_id"] == CARD
    assert body["sender_id"] == "UCLICKER" and body["text"] == "Button tapped: " + DATA
    assert body["reply_to"] == CARD and body["reply_to_sender_id"] == "UBOT"
    result = _signed_post(gateway, "/broker/thread", {
        "agent_name": "sample", "message_id": "act-" + ACTION, "content": "Reply",
    })
    assert result.status_code == 200, result.text
    assert outbound.call_args.kwargs["thread_ts"] == CARD
    assert _text(gateway, platform="slack", chat_id="CCHANNEL", message_id=CARD).status_code == 404


@pytest.mark.asyncio
async def test_tap_prompt_header_carries_slack_user_id(gateway, monkeypatch):
    state = _gateway_incoming(gateway, monkeypatch)
    await _tap(state)
    state.session.send.assert_awaited_once()
    prompt = state.session.send.await_args.args[0]
    assert "slack_user_id:UCLICKER" in prompt
    assert "msg_id:act-" + ACTION in prompt and "thread_root_ts:" + CARD in prompt


@pytest.mark.asyncio
@pytest.mark.parametrize("data", ["approve_x", "stop"])
async def test_tap_data_never_runs_owner_commands(gateway, monkeypatch, data):
    state = _gateway_incoming(gateway, monkeypatch)
    handlers = []
    for name in ("_handle_stop_command", "_handle_voice_approval_command", "_handle_approval_command"):
        handler = AsyncMock(return_value=True)
        monkeypatch.setattr(gateway.broker, name, handler)
        handlers.append(handler)
    await _tap(state, _payload(data=data))
    state.session.send.assert_awaited_once()
    for handler in handlers:
        handler.assert_not_awaited()


MALFORMED = [
    ("actions", []), ("actions", [{}, {}]), ("actions", "invalid"), ("actions.0", None),
    ("actions.0.type", "static_select"), ("actions.0.action_id", "pinky_btn:bad data"),
    ("actions.0.action_id", "pinky_btn:" + "a" * 65), ("actions.0.value", "different"),
    ("actions.0.block_id", "custom"), ("user.id", "bad id"), ("user.id", "U"),
    ("channel.id", "bad-id"), ("container.channel_id", "COTHER"),
    ("container.type", "view"), ("container.is_ephemeral", True),
    ("message.ts", "bad"), ("container.message_ts", "1700000000.2"),
    ("message.thread_ts", "bad"), ("actions.0.action_ts", "bad"),
    ("message.user", "UOTHER"), ("message.blocks", None), ("message.blocks", []),
    ("message.blocks.1.elements.0.action_id", "pinky_btn:other"),
    ("message.blocks.1.elements.0.value", "different"),
    ("user", None), ("channel", []), ("container", None), ("message", "invalid"),
]


def _set(payload, path, value):
    parts = path.split(".")
    current = payload
    for part in parts[:-1]:
        current = current[int(part)] if isinstance(current, list) else current[part]
    part = parts[-1]
    current[int(part) if isinstance(current, list) else part] = value


@pytest.mark.asyncio
@pytest.mark.parametrize("path,value", MALFORMED, ids=[p + "-" + str(i) for i, (p, v) in enumerate(MALFORMED)])
async def test_malformed_clicks_are_dropped_fail_closed(path, value, capsys):
    state = _state()
    payload = _payload()
    payload["message"].pop("bot_id")
    _set(payload, path, value)
    claim = Mock()
    state.broker.claim_button_card = claim
    await _required(state.poller, "_handle_button_tap")(payload)
    await _drain()
    state.broker.handle_inbound.assert_not_awaited()
    state.adapter.update_message.assert_not_called()
    state.adapter.respond_via_url.assert_not_called()
    state.poller._resolve_user_name.assert_not_awaited()
    claim.assert_not_called()
    assert "button tap malformed (" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_bot_id_ownership_is_accepted_when_user_is_missing():
    state = _state()
    payload = _payload()
    payload["message"].pop("user")
    await _tap(state, payload)
    state.broker.handle_inbound.assert_awaited_once()
    assert state.broker.handle_inbound.await_args.args[0].metadata["reply_to_sender_id"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "pending", "denied", "approved"])
async def test_dm_approval_precheck_only_delivers_existing_approval(status):
    state = _state()
    state.statuses["UCLICKER"] = status
    await _tap(state, _payload(channel="DDIRECT"))
    assert state.broker.handle_inbound.await_count == (1 if status == "approved" else 0)
    state.registry.get_user_status.assert_called_with("sample", "UCLICKER")
    state.registry.add_pending_user.assert_not_called()
    state.registry.queue_pending_message_with_approval_request.assert_not_called()
    state.registry.set_primary_user.assert_not_called()
    if status != "approved":
        state.adapter.update_message.assert_not_called()
        state.adapter.respond_via_url.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("status", [None, "pending"])
async def test_dm_click_from_primary_user_is_delivered_without_prior_approval(status):
    state = _state()
    state.statuses["UCLICKER"] = status
    state.registry.get_primary_user.return_value = {"chat_id": "UCLICKER"}
    await _tap(state, _payload(channel="DDIRECT"))
    state.broker.handle_inbound.assert_awaited_once()
    state.registry.set_primary_user.assert_not_called()


@pytest.mark.asyncio
async def test_click_never_claims_ownership_when_no_primary_user():
    state = _state()
    state.statuses.clear()
    state.registry.get_primary_user.return_value = {}
    await _tap(state, _payload(channel="DDIRECT"))
    state.broker.handle_inbound.assert_not_awaited()
    state.registry.set_primary_user.assert_not_called()
    assert state.broker._button_claims == {}


@pytest.mark.asyncio
@pytest.mark.parametrize("approved", [True, False])
async def test_channel_click_uses_channel_approval(approved):
    state = _state()
    state.statuses["CCHANNEL"] = "approved" if approved else None
    state.registry.get_primary_user.return_value = {"chat_id": "UCLICKER"}
    await _tap(state)
    assert state.broker.handle_inbound.await_count == int(approved)
    state.registry.get_user_status.assert_called_with("sample", "CCHANNEL")
    if not approved:
        state.adapter.respond_via_url.assert_not_called()
        assert state.broker._button_claims == {}


@pytest.mark.asyncio
async def test_refused_click_does_not_claim_the_card():
    state = _state()
    state.statuses["CCHANNEL"] = "denied"
    await _tap(state)
    state.broker.handle_inbound.assert_not_awaited()
    state.statuses["CCHANNEL"] = "approved"
    await _tap(state, _payload(user="UOTHER", action_ts="1700000002.1"))
    state.broker.handle_inbound.assert_awaited_once()
    state.adapter.update_message.assert_called_once()


def test_approval_precheck_fails_closed_on_registry_fault():
    state = _state()
    state.registry.get_user_status.side_effect = RuntimeError("private")
    msg = BrokerMessage(platform="slack", chat_id="DDIRECT", sender_name="Display",
                        content="Button tapped: x", agent_name="sample", sender_id="UCLICKER")
    assert _required(state.broker, "button_tap_allowed")(msg) is False


@pytest.mark.asyncio
@pytest.mark.parametrize("group,sender,chat,key", [
    (False, "UCLICKER", "DDIRECT", "UCLICKER"),
    (False, "", "DDIRECT", "DDIRECT"), (True, "UCLICKER", "CCHANNEL", "CCHANNEL"),
    (True, "UCLICKER", "", "UCLICKER"),
])
async def test_handle_inbound_approval_key_refactor_is_behavior_neutral(group, sender, chat, key):
    state = _state()
    state.registry.get_user_status.side_effect = None
    state.registry.get_user_status.return_value = "approved"
    broker = MessageBroker(state.registry, Mock())
    broker._route_streaming = AsyncMock()
    msg = BrokerMessage(platform="slack", chat_id=chat, sender_name="Display",
                        content="Ordinary message", agent_name="sample",
                        sender_id=sender, is_group=group)
    await broker.handle_inbound(msg)
    state.registry.get_user_status.assert_called_once_with("sample", key)
    broker._route_streaming.assert_awaited_once_with("sample", msg)


@pytest.mark.asyncio
async def test_first_click_replaces_buttons_with_selected_label():
    state = _state()
    await _tap(state)
    state.adapter.update_message.assert_called_once_with(
        "CCHANNEL", CARD, text=CONTENT, blocks=[_blocks()[0], _chosen()], timeout=5.0,
    )
    state.adapter.respond_via_url.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("label", [None, "", "  ", "a" * 65, "a\nb", "a\u2028b", "a\u2029b"])
async def test_unknown_label_selects_once_with_plain_selected(label):
    state = _state()
    payload = _payload()
    payload["message"]["blocks"][1]["elements"][0]["text"]["text"] = label
    await _tap(state, payload)
    await _tap(state, payload)
    state.broker.handle_inbound.assert_awaited_once()
    assert state.adapter.update_message.call_args.kwargs["blocks"][-1] == _chosen(None)


def test_chosen_renderer_preserves_block_order_and_input():
    original = [{"type": "divider"}, *_blocks(), {"type": "divider"}]
    snapshot = copy.deepcopy(original)
    actual = _required(slack, "chosen_card_blocks")(original, "Approve")
    assert original == snapshot
    assert actual == [snapshot[0], snapshot[1], _chosen(), snapshot[3]]


@pytest.mark.asyncio
async def test_later_click_gets_ephemeral_already_chosen_and_is_not_delivered():
    state = _state()
    await _tap(state)
    await _tap(state, _payload(user="UOTHER", action_ts="1700000002.1"))
    state.broker.handle_inbound.assert_awaited_once()
    state.adapter.update_message.assert_called_once()
    state.adapter.respond_via_url.assert_called_once_with(URL, {
        "response_type": "ephemeral", "replace_original": False, "text": "Already chosen",
    })


@pytest.mark.asyncio
async def test_redelivered_envelope_is_delivered_once(monkeypatch):
    state = _state()
    events = []
    listener = await _socket(state, monkeypatch, events)
    req = SimpleNamespace(type="interactive", envelope_id="same-envelope", payload=_payload())
    await listener(state.poller._client, req)
    await listener(state.poller._client, req)
    await _drain()
    assert events == ["ack", "ack"]
    state.broker.handle_inbound.assert_awaited_once()
    state.adapter.respond_via_url.assert_called_once()


@pytest.mark.asyncio
async def test_two_racing_clicks_deliver_once():
    state = _state()
    entered = threading.Event()
    release = threading.Event()
    second_answered = asyncio.Event()
    loop = asyncio.get_running_loop()

    def blocked_update(*args, **kwargs):
        entered.set()
        assert release.wait(5), "test did not release the card update"

    state.adapter.update_message.side_effect = blocked_update
    state.adapter.respond_via_url.side_effect = lambda *a: loop.call_soon_threadsafe(
        second_answered.set,
    )
    racing = asyncio.gather(_tap(state), _tap(state, _payload(user="UOTHER", action_ts="1700000002.1")))
    try:
        assert await asyncio.to_thread(entered.wait, 5), "first click never updated the card"
        await asyncio.wait_for(second_answered.wait(), 5)
        state.broker.handle_inbound.assert_awaited_once()
        state.adapter.respond_via_url.assert_called_once()
        state.adapter.update_message.assert_called_once()
    finally:
        release.set()
        await racing


def test_claim_is_atomic_across_threads(tmp_path):
    path = str(tmp_path / "claims.db")
    stores = [MessageContextStore(path) for _ in range(8)]
    barrier = threading.Barrier(8, timeout=5)

    def claim(store):
        method = _required(store, "claim_button_choice")
        barrier.wait()
        return method("sample", "slack", "CCHANNEL", CARD)

    try:
        with ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(claim, stores))
        assert results.count(True) == 1 and results.count(False) == 7
        row = stores[0]._db.execute("SELECT * FROM button_choices").fetchone()
        assert set(row.keys()) == {"agent_name", "platform", "chat_id", "message_id", "chosen_at"}
    finally:
        for store in stores:
            store.close()


@pytest.mark.asyncio
async def test_a_chosen_card_stays_chosen_after_restart(tmp_path):
    path = str(tmp_path / "claims.db")
    first_store = MessageContextStore(path)
    try:
        first = _state(store=first_store)
        await _tap(first)
        first.broker.handle_inbound.assert_awaited_once()
    finally:
        first_store.close()
    second_store = MessageContextStore(path)
    try:
        second = _state(store=second_store)
        await _tap(second)
        second.broker.handle_inbound.assert_not_awaited()
        second.adapter.update_message.assert_not_called()
        second.adapter.respond_via_url.assert_called_once_with(URL, {
            "response_type": "ephemeral", "replace_original": False, "text": "Already chosen",
        })
    finally:
        second_store.close()


@pytest.mark.asyncio
async def test_persisted_claim_survives_both_card_updates_failing(tmp_path):
    path = str(tmp_path / "claims.db")
    store = MessageContextStore(path)
    try:
        first = _state(store=store)
        first.adapter.update_message.side_effect = SlackError("synthetic")
        first.adapter.respond_via_url.side_effect = SlackError("synthetic")
        await _tap(first)
        first.broker.handle_inbound.assert_awaited_once()
        assert first.adapter.respond_via_url.call_count == 1
        assert store._db.execute("SELECT count(*) FROM button_choices").fetchone()[0] == 1
    finally:
        store.close()
    store = MessageContextStore(path)
    try:
        restarted = _state(store=store)
        await _tap(restarted, _payload(user="UOTHER", action_ts="1700000002.1"))
        restarted.broker.handle_inbound.assert_not_awaited()
        restarted.adapter.update_message.assert_not_called()
        assert restarted.adapter.respond_via_url.call_args.args[1]["text"] == "Already chosen"
    finally:
        store.close()


def test_claim_identity_scopes_agent_platform_chat_and_message(tmp_path):
    store = MessageContextStore(str(tmp_path / "claims.db"))
    try:
        claim = _required(store, "claim_button_choice")
        original = ["sample", "slack", "CCHANNEL", CARD]
        assert claim(*original)
        for index, value in enumerate(("other", "telegram", "COTHER", "1700000000.2")):
            key = list(original)
            key[index] = value
            assert claim(*key)
            assert not claim(*key)
        assert not claim(*original)
        assert store._db.execute("SELECT count(*) FROM button_choices").fetchone()[0] == 5
    finally:
        store.close()


@pytest.mark.asyncio
async def test_a_marker_on_the_card_is_chosen_without_any_record(tmp_path):
    store = MessageContextStore(str(tmp_path / "claims.db"))
    try:
        state = _state(store=store)
        payload = _payload()
        payload["message"]["blocks"] = [_blocks()[0], _chosen()]
        state.broker.button_tap_allowed = Mock(side_effect=AssertionError("access checked"))
        state.broker.claim_button_card = Mock(side_effect=AssertionError("card claimed"))
        await _tap(state, payload)
        state.broker.handle_inbound.assert_not_awaited()
        state.broker.button_tap_allowed.assert_not_called()
        state.broker.claim_button_card.assert_not_called()
        state.adapter.update_message.assert_not_called()
        state.adapter.respond_via_url.assert_called_once_with(URL, {
            "response_type": "ephemeral", "replace_original": False, "text": "Already chosen",
        })
        assert store._db.execute("SELECT count(*) FROM button_choices").fetchone()[0] == 0
    finally:
        store.close()


def test_claim_rows_expire_with_message_context_retention(tmp_path):
    store = MessageContextStore(str(tmp_path / "claims.db"), retention_days=1)
    now = time.time()
    try:
        claim = _required(store, "claim_button_choice")
        assert claim("sample", "slack", "CCHANNEL", CARD, now=now)
        assert not claim("sample", "slack", "CCHANNEL", CARD, now=now + 86400)
        assert store.sweep_retention(now=now + 86400) == 0
        assert store.sweep_retention(now=now + 86400.001) == 1
        assert claim("sample", "slack", "CCHANNEL", CARD, now=now + 86400.002)
        assert claim("sample", "slack", "COTHER", CARD, now=now + 172800.003)
        assert store._db.execute("SELECT chat_id FROM button_choices").fetchall()[0][0] == "COTHER"
    finally:
        store.close()


@pytest.mark.parametrize("part", range(4))
def test_empty_claim_identity_is_refused_without_a_write(tmp_path, part):
    store = MessageContextStore(str(tmp_path / "claims.db"))
    try:
        key = ["sample", "slack", "CCHANNEL", CARD]
        key[part] = ""
        assert _required(store, "claim_button_choice")(*key) is False
        assert store._db.execute("SELECT count(*) FROM button_choices").fetchone()[0] == 0
    finally:
        store.close()


def test_claim_failure_rolls_back_pruning_and_can_be_retried(tmp_path):
    store = MessageContextStore(str(tmp_path / "claims.db"), retention_days=1)
    now = time.time()
    try:
        claim = _required(store, "claim_button_choice")
        assert claim("sample", "slack", "CCHANNEL", CARD, now=now)
        store._db.executescript("CREATE TRIGGER fail_claim BEFORE INSERT ON button_choices "
                               "BEGIN SELECT RAISE(ABORT, 'synthetic'); END;")
        with pytest.raises(sqlite3.IntegrityError):
            claim("sample", "slack", "COTHER", CARD, now=now + 86401)
        assert store._db.execute("SELECT count(*) FROM button_choices").fetchone()[0] == 1
        store._db.execute("DROP TRIGGER fail_claim")
        store._db.commit()
        assert claim("sample", "slack", "COTHER", CARD, now=now + 86401)
    finally:
        store.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("present", [False, True])
async def test_store_fault_falls_back_to_in_process_claim(present, capsys):
    store = Mock() if present else None
    if store:
        store.claim_button_choice.side_effect = RuntimeError(DATA + URL)
    state = _state(store=store)
    await _tap(state)
    await _tap(state)
    state.broker.handle_inbound.assert_awaited_once()
    state.adapter.update_message.assert_called_once()
    state.adapter.respond_via_url.assert_called_once()
    log = capsys.readouterr().err
    if present:
        assert "button-claim persistence failed: RuntimeError" in log
    assert DATA not in log and URL not in log


@pytest.mark.asyncio
@pytest.mark.parametrize("both_fail", [False, True])
async def test_failed_chat_update_falls_back_and_still_delivers_once(both_fail, capsys):
    state = _state()
    state.adapter.update_message.side_effect = SlackError(DATA + URL)
    if both_fail:
        state.adapter.respond_via_url.side_effect = RuntimeError(DATA + URL)
    await _tap(state)
    await _tap(state)
    state.broker.handle_inbound.assert_awaited_once()
    assert state.adapter.update_message.call_count == 1
    assert state.adapter.respond_via_url.call_count == 2
    assert state.adapter.respond_via_url.call_args_list[0].args == (URL, {
        "replace_original": True, "text": "Selected: Approve", "blocks": [_blocks()[0], _chosen()],
    })
    assert state.adapter.respond_via_url.call_args_list[1].args[1]["text"] == "Already chosen"
    log = capsys.readouterr().err
    assert "SlackError" in log and DATA not in log and URL not in log


@pytest.mark.asyncio
@pytest.mark.parametrize("url", ["http://hooks.slack.com/a", "https://hooks.slack.com.evil/a",
                                 "https://example.invalid/a", "", None])
async def test_response_url_guard_never_posts_to_other_hosts(url):
    state = _state()
    payload = _payload()
    payload["response_url"] = url
    state.adapter.update_message.side_effect = SlackError("synthetic")
    await _tap(state, payload)
    await _tap(state, payload)
    state.broker.handle_inbound.assert_awaited_once()
    state.adapter.respond_via_url.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("other_channel", [False, True])
async def test_different_cards_and_same_ts_in_different_channels_are_independent(other_channel):
    state = _state()
    state.statuses["COTHER"] = "approved"
    await _tap(state)
    await _tap(state, _payload(channel="COTHER" if other_channel else "CCHANNEL",
                               ts=CARD if other_channel else "1700000000.2"))
    assert state.broker.handle_inbound.await_count == 2
    assert state.adapter.update_message.call_count == 2


def test_store_restore_sentinels_unchanged():
    assert STORE_SCHEMA_SENTINELS["message_context"] == ("message_contexts",)


def test_whole_database_backup_keeps_claims_and_old_snapshots_gain_the_table(tmp_path):
    from pinky_daemon.store_restore import _verify_static_store

    path, backup = tmp_path / "claims.db", tmp_path / "snapshot.db"
    store = MessageContextStore(str(path))
    try:
        assert _required(store, "claim_button_choice")("sample", "slack", "CCHANNEL", CARD)
        destination = sqlite3.connect(backup)
        try:
            store._db.backup(destination)
        finally:
            destination.close()
    finally:
        store.close()
    _verify_static_store(backup, ("message_context",))
    restored = MessageContextStore(str(backup))
    try:
        assert not restored.claim_button_choice("sample", "slack", "CCHANNEL", CARD)
        restored._db.execute("DROP TABLE button_choices")
        restored._db.commit()
    finally:
        restored.close()
    _verify_static_store(backup, ("message_context",))
    legacy = MessageContextStore(str(backup))
    try:
        assert legacy.claim_button_choice("sample", "slack", "CCHANNEL", CARD)
    finally:
        legacy.close()


def test_fallback_claims_are_bounded_and_keep_the_newest_card():
    state = _state()
    claim = _required(state.broker, "claim_button_card")
    for i in range(4097):
        assert claim("sample", "slack", "CCHANNEL", str(i))
    assert len(state.broker._button_claims) == 4096
    assert not claim("sample", "slack", "CCHANNEL", "4096")
    assert claim("sample", "slack", "CCHANNEL", "0")
    assert len(state.broker._button_claims) == 4096


def test_broker_fallback_claim_is_atomic_across_threads():
    state = _state()
    claim = _required(state.broker, "claim_button_card")
    barrier = threading.Barrier(8, timeout=5)

    def attempt(_):
        barrier.wait()
        return claim("sample", "slack", "CCHANNEL", CARD)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))
    assert results.count(True) == 1 and results.count(False) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("admitted", [False, True])
async def test_stop_during_resolution_keeps_the_existing_admission_rule(admitted):
    state = _state()

    async def resolve(_):
        state.poller._accepting_deliveries = False
        return "Display"

    state.poller._resolve_user_name.side_effect = resolve
    await state.poller._handle_interactive(_payload(), _admitted=admitted)
    await _drain()
    assert state.broker.handle_inbound.await_count == int(admitted)
    assert state.adapter.update_message.call_count == int(admitted)


@pytest.mark.asyncio
@pytest.mark.parametrize("decision", ["approve", "reject"])
async def test_purchase_click_never_reaches_the_button_path(decision, monkeypatch):
    from tests.test_slack_purchase_approval import _interactive, _make_poller

    poller = _make_poller()
    button = AsyncMock()
    monkeypatch.setattr(poller, "_handle_button_tap", button, raising=False)
    payload = _interactive("pos_purchase_" + decision)
    payload["actions"][0]["block_id"] = "pinky_btn"
    payload["message"]["user"] = "UBOT"
    await poller._handle_interactive(payload)
    button.assert_not_awaited()
    poller._registry.is_purchase_approver.assert_called_once()
    poller._broker.inject_agent_message.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("data", [DATA, "pos_purchase_approve", "pos_purchase_reject"])
async def test_button_click_never_reaches_the_purchase_gate(data, monkeypatch):
    state = _state()
    mint = Mock(side_effect=AssertionError("purchase token minted"))
    monkeypatch.setattr(pollers.purchase_approval, "make_approval_token", mint)
    state.broker.inject_agent_message = AsyncMock()
    await _tap(state, _payload(data=data))
    state.broker.handle_inbound.assert_awaited_once()
    state.registry.is_purchase_approver.assert_not_called()
    state.registry.get_purchase_approval_secret.assert_not_called()
    mint.assert_not_called()
    state.broker.inject_agent_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_slack_tap_routes_while_login_relay_waits(gateway, monkeypatch):
    state = _gateway_incoming(gateway, monkeypatch)
    gateway.agents.set_setting("PINKY_TMUX_AUTH_RELAY", "1")
    relay = AuthRelayCoordinator()
    relay.configure(AsyncMock(return_value={"message_id": CARD}), gateway.agents.get_primary_user)
    monkeypatch.setattr(broker_module, "_auth_relay", relay)
    waiting = asyncio.create_task(relay.open("sample", "https://example.invalid/login", ttl_sec=30))
    try:
        for _ in range(200):
            if relay.has_pending("sample"):
                break
            await asyncio.sleep(0)
        assert relay.has_pending("sample")
        await _tap(state)
        state.session.send.assert_awaited_once()
        assert relay.has_pending("sample") and not waiting.done()
    finally:
        waiting.cancel()
        with suppress(asyncio.CancelledError):
            await waiting


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["selected", "already", "refused", "malformed", "update_error",
                                     "response_error", "callback_error", "delivery_error"])
async def test_tap_logs_carry_ids_only(capsys, outcome):
    state = _state(event_callback=AsyncMock())
    payload = _payload()
    label = "Private label"
    payload["message"]["blocks"][1]["elements"][0]["text"]["text"] = label
    if outcome == "already":
        payload["message"]["blocks"] = [_chosen()]
    elif outcome == "refused":
        state.statuses.clear()
    elif outcome == "malformed":
        payload["actions"][0]["value"] = "mismatch"
    elif outcome in ("update_error", "response_error"):
        state.adapter.update_message.side_effect = RuntimeError(DATA + label + URL)
        if outcome == "response_error":
            state.adapter.respond_via_url.side_effect = RuntimeError(DATA + label + URL)
    elif outcome == "callback_error":
        state.poller._event_callback.side_effect = RuntimeError(DATA + label + URL)
    elif outcome == "delivery_error":
        state.broker.handle_inbound.side_effect = RuntimeError(DATA + label + URL)
    await _tap(state, payload)
    log = capsys.readouterr().err
    assert log and "button tap" in log
    assert DATA not in log and label not in log and URL not in log and CONTENT not in log


def test_warm_claim_timing_receipt(tmp_path, record_property):
    store = MessageContextStore(str(tmp_path / "claims.db"))
    try:
        claim = _required(store, "claim_button_choice")
        assert claim("sample", "slack", "CCHANNEL", "warm")
        durations = []
        for i in range(20):
            start = time.perf_counter()
            assert claim("sample", "slack", "CCHANNEL", str(i))
            durations.append((time.perf_counter() - start) * 1000)
        record_property("warm_claim_ms", json.dumps(durations))
        record_property("warm_claim_max_ms", max(durations))
    finally:
        store.close()
