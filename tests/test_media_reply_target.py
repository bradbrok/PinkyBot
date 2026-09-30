"""Media reply targets stay attached to their resolved conversation."""

from __future__ import annotations

import io
import json
import urllib.request
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.broker import BrokerMessage
from pinky_outreach.slack import SlackAdapter
from pinky_outreach.telegram import TelegramAdapter
from tests.isolated_policy_support import closure, replace_cell

ROUTES = ("photo", "document", "video", "animation", "gif", "voice")
CHAT = "1001"
MESSAGE = "101"


@pytest.fixture
def media(tmp_path, monkeypatch):
    app = create_api(db_path=str(tmp_path / "state.db"), default_working_dir=str(tmp_path))
    app.state.agents.set_setting("OPENAI_API_KEY", "fixture-key")
    adapter = Mock()
    for method in ("send_photo", "send_document", "send_video", "send_animation", "send_voice"):
        getattr(adapter, method).return_value = SimpleNamespace(message_id="201")
    select_adapter = Mock(return_value=adapter)
    replace_cell(
        monkeypatch, closure(app, "_send_file_message"), "_get_platform_adapter", select_adapter
    )
    monkeypatch.setattr(app.state.broker, "_stop_typing", Mock())

    def remote(request, **kwargs):
        url = request if isinstance(request, str) else request.full_url
        if url.startswith("https://api.giphy.com/v1/gifs/search?"):
            return io.BytesIO(
                json.dumps(
                    {"data": [{"images": {"original": {"url": "https://fixture.test/media.gif"}}}]}
                ).encode()
            )
        assert url in ("https://fixture.test/media.gif", "https://api.openai.com/v1/audio/speech")
        return io.BytesIO(b"fixture-media")

    network = Mock(side_effect=remote)
    monkeypatch.setattr(urllib.request, "urlopen", network)
    file = tmp_path / "sample.png"
    file.write_bytes(b"\x89PNG\r\n\x1a\nfixture")
    with TestClient(app) as client:
        app.state.agents.register("sender", working_dir=str(tmp_path))
        yield SimpleNamespace(
            app=app,
            client=client,
            adapter=adapter,
            select_adapter=select_adapter,
            network=network,
            file=file,
        )


def remember(media, *, platform="telegram", chat=CHAT, message=MESSAGE, reply_to=""):
    media.app.state.broker.remember_message_context(
        BrokerMessage(
            platform=platform,
            chat_id=chat,
            message_id=message,
            reply_to=reply_to,
            agent_name="sender",
            sender_name="fixture",
            sender_id="fixture-user",
            content="source message",
        )
    )


def payload(media, kind):
    return {
        "agent_name": "sender",
        "platform": "telegram",
        "file_path": str(media.file),
        "query": "fixture",
        "text": "fixture audio",
    }


@pytest.mark.parametrize("kind", ROUTES)
@pytest.mark.parametrize("target", ("message_only", "matching", "mismatched", "chat_only"))
def test_media_reply_target_matrix(media, kind, target):
    remember(media)
    body = payload(media, kind)
    if target != "chat_only":
        body["message_id"] = MESSAGE
    if target == "message_only":
        # A resolved message owns the platform as well as the chat and reply ID.
        body["platform"] = "discord"
    if target != "message_only":
        body["chat_id"] = "1002" if target == "mismatched" else CHAT

    response = media.client.post(f"/broker/send-{kind}", json=body)

    if target == "mismatched":
        assert response.status_code == 400, response.text
        detail = response.json()["detail"]
        assert "chat_id" in detail and "message_id" in detail
        assert "1002" in detail and CHAT in detail
        assert media.adapter.mock_calls == []
        media.select_adapter.assert_not_called()
        media.network.assert_not_called()
        assert media.app.state.broker.get_message_context("sender", "201") is None
        return

    assert response.status_code == 200, response.text
    assert response.json()["chat_id"] == CHAT
    assert response.json()["platform"] == "telegram"
    method = "send_animation" if kind == "gif" else f"send_{kind}"
    send = getattr(media.adapter, method)
    send.assert_called_once()
    assert send.call_args.args[0] == CHAT
    expected_reply = None if target == "chat_only" else int(MESSAGE)
    assert send.call_args.kwargs["reply_to_message_id"] == expected_reply
    context = media.app.state.broker.get_message_context("sender", "201")
    assert context is not None
    assert context.chat_id == CHAT
    assert context.reply_to == ("" if target == "chat_only" else MESSAGE)


@pytest.mark.parametrize("kind", ROUTES)
def test_media_unknown_message_with_chat_is_refused_before_delivery(media, kind):
    body = {**payload(media, kind), "message_id": "missing", "chat_id": CHAT}

    response = media.client.post(f"/broker/send-{kind}", json=body)

    assert response.status_code == 404, response.text
    assert "Message context 'missing' not found" in response.json()["detail"]
    media.select_adapter.assert_not_called()
    assert media.adapter.mock_calls == []
    media.network.assert_not_called()


@pytest.mark.parametrize("kind", ("photo", "document", "video", "animation"))
@pytest.mark.parametrize("include_chat", (False, True), ids=("message_only", "matching_chat"))
@pytest.mark.parametrize("source", ("root", "child", "outbound_file"))
def test_slack_file_upload_uses_thread_root(media, monkeypatch, kind, include_chat, source):
    thread_ts = "1700000000.000100"
    message_id = {"root": thread_ts, "child": "1700000001.000200", "outbound_file": "F456"}[source]
    if source == "outbound_file":
        media.app.state.broker.remember_outbound_message_context(
            "sender", message_id, platform="slack", chat_id="C123", reply_to=thread_ts,
        )
    else:
        remember(
            media, platform="slack", chat="C123", message=message_id,
            reply_to=thread_ts if source == "child" else "",
        )
    adapter = SlackAdapter("xoxb-fixture-token")
    media.select_adapter.return_value = adapter
    upload_url = "https://files.slack.com/upload/fixture"
    post = Mock(
        side_effect=[
            SimpleNamespace(json=lambda: {"ok": True, "upload_url": upload_url, "file_id": "F123"}),
            SimpleNamespace(json=lambda: {"ok": True, "files": [{"id": "F123"}]}),
        ]
    )
    monkeypatch.setattr(adapter._client, "post", post)
    upload = Mock(return_value=SimpleNamespace(status_code=200))
    monkeypatch.setattr("pinky_outreach.slack.httpx.post", upload)
    try:
        body = {**payload(media, kind), "message_id": message_id, "platform": "slack"}
        if include_chat:
            body["chat_id"] = "C123"
        response = media.client.post(f"/broker/send-{kind}", json=body)

        assert response.status_code == 200, response.text
        upload.assert_called_once()
        assert upload.call_args.args == (upload_url,)
        assert post.call_count == 2
        complete = post.call_args_list[1]
        assert complete.args == ("/files.completeUploadExternal",)
        assert complete.kwargs["data"]["channel_id"] == "C123"
        assert complete.kwargs["data"]["thread_ts"] == thread_ts
        assert "json" not in complete.kwargs
        assert response.json()["message_id"] == "F123"
        context = media.app.state.broker.get_message_context("sender", "F123")
        assert context is not None
        assert context.reply_to == thread_ts
    finally:
        adapter.close()


@pytest.mark.parametrize("kind", ("photo", "document", "video", "animation"))
@pytest.mark.parametrize("include_chat", (False, True), ids=("message_only", "matching_chat"))
def test_telegram_file_upload_replies_to_selected_child(media, monkeypatch, kind, include_chat):
    remember(media, reply_to="100")
    adapter = TelegramAdapter("fixture-token")
    media.select_adapter.return_value = adapter
    post = Mock(return_value=SimpleNamespace(json=lambda: {
        "ok": True,
        "result": {"message_id": 201, "chat": {"id": int(CHAT)}, "date": 1700000000},
    }))
    monkeypatch.setattr(adapter._client, "post", post)
    try:
        body = {**payload(media, kind), "message_id": MESSAGE}
        if include_chat:
            body["chat_id"] = CHAT
        response = media.client.post(f"/broker/send-{kind}", json=body)

        assert response.status_code == 200, response.text
        post.assert_called_once()
        assert post.call_args.args == (f"{adapter._base}/send{kind.title()}",)
        assert post.call_args.kwargs["data"]["chat_id"] == CHAT
        assert post.call_args.kwargs["data"]["reply_to_message_id"] == int(MESSAGE)
        assert kind in post.call_args.kwargs["files"]
        assert "json" not in post.call_args.kwargs
        context = media.app.state.broker.get_message_context("sender", "201")
        assert context is not None
        assert context.reply_to == MESSAGE
    finally:
        adapter.close()
