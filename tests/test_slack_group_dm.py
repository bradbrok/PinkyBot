"""Slack user lists send once to a resolved group conversation."""

from __future__ import annotations

import asyncio
import json
import threading
from collections import deque
from contextlib import contextmanager
from types import SimpleNamespace
from unittest.mock import Mock
from urllib.parse import parse_qs

import httpx
import pytest

from pinky_daemon.auth import build_internal_auth_headers
from pinky_messaging.server import create_server
from pinky_outreach import slack as slack_module
from pinky_outreach.slack import SlackAdapter, SlackError, button_card_blocks
from tests.isolated_policy_support import closure, replace_cell
from tests.test_inbound_message_fields import gateway as gateway
from tests.test_inbound_message_fields import private_home as private_home
from tests.test_messaging_server import _tools

pytestmark = pytest.mark.real_auth

USERS = ["W0BBBB222", "U0AAAA111"]
TARGET = ",".join(USERS)
CANONICAL = ",".join(sorted(USERS))
GROUP = "G0GROUP111"
TEXT = "Choose an option"
BUTTONS = [{"text": "Choose", "data": "request:1730312345.123456:count:3", "style": "primary"}]
INVALID_REASONS = ["empty", "empty", "empty", "unique", "valid", "valid", "valid", "valid", "valid", "8"]
INVALID_LISTS = [
    "U0AAAA111,", ",U0AAAA111", "U0AAAA111,,W0BBBB222",
    "U0AAAA111,U0AAAA111", "u0aaaa111,W0BBBB222", "C0CHANNEL,W0BBBB222",
    "#general,W0BBBB222", "U0AA/111,W0BBBB222", "U0AAé111,W0BBBB222",
    ",".join(f"U0AA{i:04d}" for i in range(9)),
]


@contextmanager
def _adapter():
    calls = []
    outcomes = deque()
    state = SimpleNamespace(calls=calls, outcomes=outcomes, before_open=None)

    def response(request):
        method = request.url.path.rsplit("/", 1)[-1]
        if "application/json" in request.headers.get("content-type", ""):
            body = json.loads(request.content)
        else:
            body = {key: values[0] for key, values in parse_qs(request.content.decode()).items()}
        calls.append((method, body, request.headers.get("content-type", "")))
        if method == "conversations.open":
            if state.before_open:
                state.before_open()
            result = outcomes.popleft() if outcomes else {"ok": True, "channel": {"id": GROUP}}
        elif method == "chat.postMessage":
            result = {"ok": True, "message": {"ts": f"1700000000.{len(calls):06d}"}}
        elif method == "files.getUploadURLExternal":
            result = {"ok": True, "file_id": "F0FILE111", "upload_url": "https://upload.invalid/fixture"}
        elif method == "files.completeUploadExternal":
            result = {"ok": True}
        else:
            raise AssertionError("unexpected fixture method: " + method)
        return httpx.Response(200, json=result)

    adapter = SlackAdapter("xoxb-FAKE")
    adapter._client.close()
    adapter._client = httpx.Client(base_url=adapter.BASE_URL, transport=httpx.MockTransport(response))
    state.adapter = adapter
    try:
        yield state
    finally:
        adapter.close()


@pytest.fixture
def group(gateway, monkeypatch, tmp_path):
    with _adapter() as state:
        select = Mock(return_value=state.adapter)
        replace_cell(monkeypatch, closure(gateway.app, "_send_text_message"),
                     "_get_platform_adapter", select)
        replace_cell(monkeypatch, closure(gateway.app, "_send_file_message"),
                     "_get_platform_adapter", select)
        upload = Mock(return_value=httpx.Response(200))
        monkeypatch.setattr(slack_module.httpx, "post", upload)
        file = tmp_path / "fixture.txt"
        file.write_text("fixture content")
        state.file, state.upload, state.gateway, state.select = file, upload, gateway, select
        yield state


def _calls(state, method):
    return [body for name, body, _ in state.calls if name == method]


def _headers(state, path="/broker/send"):
    return build_internal_auth_headers(state.gateway.agents.get_signing_key("sample"),
                                       agent_name="sample", method="POST", path=path)


def _body(chat_id=TARGET, **extra):
    return {"agent_name": "sample", "platform": "slack", "chat_id": chat_id,
            "content": TEXT, **extra}


def _post(state, chat_id=TARGET, **extra):
    return state.gateway.client.post("/broker/send", json=_body(chat_id, **extra),
                                     headers=_headers(state))


def test_adapter_opens_given_order_and_caches_sorted_set(group):
    first = group.adapter.send_message(TARGET, TEXT, thread_ts="1700000000.000001")
    second = group.adapter.send_message(CANONICAL, "Another message")
    assert _calls(group, "conversations.open") == [{"users": TARGET}]
    assert all("application/x-www-form-urlencoded" in content_type
               for method, _, content_type in group.calls if method == "conversations.open")
    assert [p["channel"] for p in _calls(group, "chat.postMessage")] == [GROUP, GROUP]
    assert first.chat_id == second.chat_id == GROUP
    assert first.metadata.get("group_dm_users") == USERS
    assert second.metadata.get("group_dm_users") == sorted(USERS)
    assert first.metadata["thread_ts"] == "1700000000.000001"


def test_group_cache_is_per_adapter(group):
    group.adapter.send_message(TARGET, TEXT)
    with _adapter() as other:
        other.adapter.send_message(CANONICAL, TEXT)
        assert len(_calls(other, "conversations.open")) == 1
    assert len(_calls(group, "conversations.open")) == 1


def test_spaces_are_stripped(group):
    response = _post(group, " W0BBBB222 , U0AAAA111 ")
    assert response.status_code == 200, response.text
    assert _calls(group, "conversations.open") == [{"users": TARGET}]
    assert response.json()["chat_id"] == GROUP and response.json().get("group_dm_users") == USERS


@pytest.mark.parametrize("code", ["missing_scope", "user_not_found", "user_disabled",
                                  "not_enough_users", "too_many_users", "cannot_dm_bot", "fixture_error"])
def test_open_failures_keep_code_and_never_post(group, code):
    group.outcomes.append({"ok": False, "error": code})
    with pytest.raises(SlackError) as failure:
        group.adapter.send_message(TARGET, TEXT)
    assert code in str(failure.value)
    if code == "missing_scope":
        assert "mpim:write" in str(failure.value) and "reinstall" in str(failure.value).lower()
    assert _calls(group, "chat.postMessage") == []
    assert group.adapter._dm_conversation_ids == {}
    result = group.adapter.send_message(CANONICAL, TEXT)
    assert len(_calls(group, "conversations.open")) == 2
    assert result.chat_id == GROUP


def test_empty_open_id_is_not_cached_or_posted(group):
    group.outcomes.append({"ok": True, "channel": {"id": ""}})
    with pytest.raises(SlackError, match="conversation"):
        group.adapter.send_message(TARGET, TEXT)
    assert _calls(group, "chat.postMessage") == []
    assert group.adapter._dm_conversation_ids == {}
    group.adapter.send_message(TARGET, TEXT)
    assert len(_calls(group, "conversations.open")) == 2


@pytest.mark.parametrize("conversation", ["G0GROUP111", "D0GROUP111", "X0GROUP111"])
def test_group_open_accepts_nonempty_conversation_without_prefix_rule(group, conversation):
    group.outcomes.append({"ok": True, "channel": {"id": conversation}})
    response = _post(group)
    assert response.status_code == 200, response.text
    assert _calls(group, "chat.postMessage")[0]["channel"] == conversation
    assert response.json()["chat_id"] == conversation


@pytest.mark.parametrize("chat_id,reason", list(zip(INVALID_LISTS, INVALID_REASONS)))
def test_validation_refuses_before_http(group, chat_id, reason):
    response = _post(group, chat_id)
    assert response.status_code == 400, response.text
    assert reason in response.json()["detail"].lower()
    assert group.calls == []


@pytest.mark.parametrize("chat_id", INVALID_LISTS)
@pytest.mark.parametrize("upload", [False, True])
def test_validation_refuses_adapter_calls(group, chat_id, upload):
    failure = None
    try:
        if upload:
            group.adapter.upload_file(chat_id, str(group.file))
        else:
            group.adapter.send_message(chat_id, TEXT)
    except (ValueError, SlackError) as error:
        failure = error
    assert isinstance(failure, ValueError), "invalid user lists must fail validation before HTTP"
    assert group.calls == []
    group.upload.assert_not_called()


@pytest.mark.parametrize("chat_id", ["U0AAAA111", "W0BBBB222", "C0CHANNEL", "D0DIRECT11", "G0EXIST11"])
def test_no_comma_preserves_send_call(group, chat_id):
    result = group.adapter.send_message(chat_id, TEXT)
    assert [(method, body) for method, body, _ in group.calls] == [
        ("chat.postMessage", {"channel": chat_id, "text": TEXT}),
    ]
    assert result.chat_id == chat_id and result.metadata == {}


def test_broker_group_send_records_resolved_target_and_users(group):
    response = _post(group)
    assert response.status_code == 200, response.text
    result = response.json()
    assert result["chat_id"] == GROUP and result.get("group_dm_users") == USERS
    assert len(_calls(group, "chat.postMessage")) == 1
    records = group.gateway.app.state.conversation_store.get_history("sample-main")
    assert len(records) == 1
    assert records[0].chat_id == GROUP
    assert records[0].metadata.get("group_dm_users") == USERS
    assert records[0].metadata["delivery"] == result
    remembered = group.gateway.broker.get_message_context("sample", result["message_id"])
    assert remembered.chat_id == GROUP


@pytest.mark.parametrize("platform", ["telegram", "discord"])
def test_other_platform_comma_target_is_unchanged(group, monkeypatch, platform):
    adapter = Mock()
    adapter.send_message.return_value = SimpleNamespace(message_id="200")
    group.select.return_value = adapter
    response = _post(group, TARGET, platform=platform, parse_mode="plain")
    assert response.status_code == 200, response.text
    assert response.json()["chat_id"] == TARGET
    assert "group_dm_users" not in response.json()
    assert adapter.send_message.call_args.args[0] == TARGET
    assert group.calls == []


def test_list_upload_completes_to_group(group):
    result = group.adapter.upload_file(TARGET, str(group.file), thread_ts="1700000000.000001")
    assert _calls(group, "conversations.open") == [{"users": TARGET}]
    completed = _calls(group, "files.completeUploadExternal")
    assert len(completed) == 1 and completed[0]["channel_id"] == GROUP
    assert completed[0]["thread_ts"] == "1700000000.000001"
    assert result.chat_id == GROUP and result.metadata.get("group_dm_users") == USERS
    group.upload.assert_called_once()


@pytest.mark.parametrize("conversation", ["G0GROUP111", "C0CHANNEL", ""])
def test_single_upload_still_requires_d(group, conversation):
    group.outcomes.append({"ok": True, "channel": {"id": conversation}})
    with pytest.raises(SlackError, match="DM conversation"):
        group.adapter.upload_file("U0AAAA111", str(group.file))
    assert _calls(group, "files.getUploadURLExternal") == []
    group.upload.assert_not_called()


@pytest.mark.parametrize("kind", ["document", "photo", "video"])
def test_upload_routes_preserve_group_result(group, kind):
    path = "/broker/send-" + kind
    response = group.gateway.client.post(path, headers=_headers(group, path), json={
        **_body(), "file_path": str(group.file), "caption": "Fixture file",
    })
    assert response.status_code == 200, response.text
    assert response.json()["chat_id"] == GROUP
    assert response.json().get("group_dm_users") == USERS
    record = group.gateway.app.state.conversation_store.get_history("sample-main")[0]
    assert record.chat_id == GROUP and record.metadata.get("group_dm_users") == USERS
    assert record.metadata["delivery"] == response.json()
    assert _calls(group, "files.completeUploadExternal")[0]["channel_id"] == GROUP


@pytest.mark.parametrize("kind", ["document", "photo", "video"])
def test_single_upload_route_result_is_unchanged(group, kind):
    group.outcomes.append({"ok": True, "channel": {"id": "D0DIRECT11"}})
    path = "/broker/send-" + kind
    response = group.gateway.client.post(path, headers=_headers(group, path), json={
        **_body("U0AAAA111"), "file_path": str(group.file),
    })
    assert response.status_code == 200
    assert response.json() == {"sent": True, "message_id": "F0FILE111", "platform": "slack",
                               "chat_id": "U0AAAA111"}
    assert _calls(group, "files.completeUploadExternal")[0]["channel_id"] == "D0DIRECT11"


def test_reordered_completed_sends_are_deduped(group):
    responses = [_post(group, target) for target in (TARGET, TARGET, CANONICAL)]
    assert all(response.status_code == 200 for response in responses)
    assert len(_calls(group, "chat.postMessage")) == 1
    first = responses[0].json()
    assert first["chat_id"] == GROUP and first.get("group_dm_users") == USERS
    assert all(response.json() == {**first, "deduped": True} for response in responses[1:])


@pytest.mark.asyncio
async def test_inflight_placeholder_uses_canonical_list(group):
    entered, release = threading.Event(), threading.Event()

    def blocked_open():
        entered.set()
        assert release.wait(5), "fixture open was not released"

    group.before_open = blocked_open
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=group.gateway.app),
                                base_url="http://fixture") as client:
        first = asyncio.create_task(client.post("/broker/send", json=_body(), headers=_headers(group)))
        try:
            assert await asyncio.to_thread(entered.wait, 2), "group send must open before posting"
            duplicate = await asyncio.wait_for(client.post(
                "/broker/send", json=_body(CANONICAL), headers=_headers(group)), 2)
            assert duplicate.status_code == 200
            assert duplicate.json() == {"sent": True, "deduped": True, "agent": "sample",
                                        "platform": "slack", "chat_id": CANONICAL, "message_id": None}
            assert _calls(group, "chat.postMessage") == []
        finally:
            release.set()
            original = await first
    assert original.status_code == 200
    assert original.json()["chat_id"] == GROUP and original.json().get("group_dm_users") == USERS
    assert len(_calls(group, "chat.postMessage")) == 1


@pytest.mark.parametrize("target", ["U0AAAA111", "C0CHANNEL", "D0DIRECT11"])
def test_single_id_dedupe_key_is_unchanged(group, monkeypatch, target):
    broker = group.gateway.broker
    deliver = broker.deliver_deduped
    observed = []

    async def track(agent, platform, chat_id, content, send, **kwargs):
        observed.append((agent, platform, chat_id, content, kwargs))
        return await deliver(agent, platform, chat_id, content, send, **kwargs)

    monkeypatch.setattr(broker, "deliver_deduped", track)
    assert _post(group, target).status_code == 200
    assert observed == [("sample", "slack", target, TEXT, {"reply_to": "", "key_extra": ""})]
    assert broker._dedupe_key("sample", "slack", target, TEXT) == (
        "sample", "slack", target, "", TEXT, "",
    )


@pytest.mark.parametrize("mode", ["buttons", "blocks"])
def test_group_cards_match_single_id_rendering(group, mode):
    extra = {"buttons": BUTTONS} if mode == "buttons" else {"blocks": [{"type": "divider"}]}
    single = _post(group, "G0CONTROL11", **extra)
    listed = _post(group, **extra)
    assert single.status_code == listed.status_code == 200
    posts = _calls(group, "chat.postMessage")
    assert [post["channel"] for post in posts] == ["G0CONTROL11", GROUP]
    assert posts[0]["blocks"] == posts[1]["blocks"]
    if mode == "buttons":
        assert posts[1]["blocks"] == button_card_blocks(TEXT, [
            {"text": "Choose", "callback_data": BUTTONS[0]["data"], "style": "primary"},
        ])
    assert listed.json()["chat_id"] == GROUP


@pytest.mark.parametrize("changes", [
    {"buttons": BUTTONS, "blocks": [{"type": "divider"}]},
    {"buttons": BUTTONS, "content": "a" * 3001},
    {"buttons": [{"text": "Choose", "data": "__chosen__"}]},
    {"buttons": BUTTONS * 2},
    {"buttons": [{"text": "Choose", "data": "one", "style": "unknown"}]},
])
def test_invalid_group_cards_keep_existing_checks(group, changes):
    response = _post(group, **changes)
    assert response.status_code == 400
    assert group.calls == []


@pytest.mark.parametrize("name", ["send", "send_photo", "send_document", "send_video"])
def test_tool_descriptions_explain_group_dm(name):
    description = _tools(create_server(agent_name="sample"))[name].__doc__
    assert "Slack group DM" in description and "2-8 comma-separated Slack user IDs" in description
    assert "result's chat_id" in description and "conversation's id" in description


def test_scope_documentation_names_mpim_write():
    assert "mpim:write" in slack_module.__doc__


@pytest.mark.parametrize("kind", ["document", "photo", "video"])
def test_validation_upload_routes_refuse_before_http(group, kind):
    path = "/broker/send-" + kind
    response = group.gateway.client.post(path, headers=_headers(group, path), json={
        **_body("u0aaaa111,W0BBBB222"), "file_path": str(group.file),
    })
    assert response.status_code == 400, response.text
    assert "valid" in response.json()["detail"].lower()
    assert group.calls == []
    group.upload.assert_not_called()
