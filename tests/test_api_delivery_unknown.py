"""A missing Telegram response must never trigger another delivery attempt."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import Mock, patch

import httpx
import pytest
from fastapi.testclient import TestClient

from pinky_daemon.api import create_api
from pinky_daemon.broker import BrokerMessage
from pinky_outreach.telegram import TelegramAdapter, TelegramError


@pytest.fixture
def sender(tmp_path, monkeypatch):
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "test.db"))
    logs = []
    monkeypatch.setattr("pinky_daemon.api._log", logs.append)
    stopped = Mock(wraps=app.state.broker._stop_typing)
    monkeypatch.setattr(app.state.broker, "_stop_typing", stopped)
    attachment = tmp_path / "photo.png"
    attachment.write_bytes(b"synthetic image")
    with TestClient(app, raise_server_exceptions=False) as client:
        app.state.agents.register("sender", working_dir=str(tmp_path / "sender"))
        app.state.agents.set_token("sender", "telegram", "synthetic-test-token")
        yield SimpleNamespace(
            app=app,
            client=client,
            logs=logs,
            stopped=stopped,
            attachment=str(attachment),
        )


def _text(sender, **extra):
    return sender.client.post(
        "/broker/send",
        json={
            "agent_name": "sender",
            "platform": "telegram",
            "chat_id": "12345",
            "content": "**hello**",
            **extra,
        },
    )


def _file(sender, kind="photo", **extra):
    return sender.client.post(
        f"/broker/send-{kind}",
        json={
            "agent_name": "sender",
            "platform": "telegram",
            "chat_id": "12345",
            "file_path": sender.attachment,
            "caption": "**hello**",
            **extra,
        },
    )


def _assert_unknown(sender, response):
    assert response.status_code == 504, response.text
    detail = response.json()["detail"]
    assert detail.startswith("delivery unknown:")
    assert "may already have been delivered" in detail
    assert "do not resend" in detail
    assert "check the chat first" in detail
    assert any(
        line.startswith("outreach-attempt:")
        and "outcome=error_upstream" in line
        and "error=delivery_unknown:" in line
        for line in sender.logs
    ), sender.logs
    sender.stopped.assert_called_once_with("sender", "12345")


@pytest.mark.parametrize(
    "error",
    [
        httpx.ReadTimeout("slow response"),
        httpx.ConnectError("connection interrupted"),
        json.JSONDecodeError("invalid response", "<html>", 0),
    ],
    ids=["read-timeout", "transport", "non-json"],
)
def test_t1_text_unknown_does_not_retry(sender, error):
    with patch.object(TelegramAdapter, "send_message", side_effect=error) as send:
        response = _text(sender)
    assert send.call_count == 1
    _assert_unknown(sender, response)
    assert type(error).__name__ in response.json()["detail"]


def test_t2_text_rejection_retries_plain(sender):
    with patch.object(
        TelegramAdapter,
        "send_message",
        side_effect=[
            TelegramError("can't parse entities", 400),
            SimpleNamespace(message_id="701"),
        ],
    ) as send:
        response = _text(sender)
    assert response.status_code == 200, response.text
    assert response.json()["message_id"] == "701"
    assert send.call_count == 2
    assert send.call_args_list[0].kwargs["parse_mode"] == "MarkdownV2"
    assert send.call_args_list[1].kwargs.get("parse_mode") is None


@pytest.mark.parametrize("kind", ["photo", "document", "video"])
def test_t3_caption_unknown_does_not_retry(sender, kind):
    with patch.object(
        TelegramAdapter, f"send_{kind}", side_effect=httpx.ReadTimeout("slow caption")
    ) as send:
        response = _file(sender, kind)
    assert send.call_count == 1
    _assert_unknown(sender, response)


def test_t4_caption_rejection_retries_plain(sender):
    with patch.object(
        TelegramAdapter,
        "send_photo",
        side_effect=[
            TelegramError("can't parse entities", 400),
            SimpleNamespace(message_id="702"),
        ],
    ) as send:
        response = _file(sender)
    assert response.status_code == 200, response.text
    assert send.call_count == 2
    assert send.call_args_list[0].kwargs["caption_parse_mode"] == "MarkdownV2"
    assert send.call_args_list[1].kwargs["caption_parse_mode"] is None


def _quote(sender):
    sender.app.state.broker.remember_message_context(
        BrokerMessage(
            platform="telegram",
            chat_id="12345",
            sender_name="User",
            sender_id="user",
            content="original passage",
            agent_name="sender",
            message_id="101",
        )
    )
    return sender.client.post(
        "/broker/thread",
        json={
            "agent_name": "sender",
            "message_id": "101",
            "content": "reply",
            "quote": "passage",
        },
    )


def test_t5_quote_rejection_still_degrades(sender):
    def respond(*args, **kwargs):
        if kwargs.get("reply_parameters") is not None:
            raise TelegramError("QUOTE_TEXT_INVALID", 400)
        return SimpleNamespace(message_id="703")

    with patch.object(TelegramAdapter, "send_message", side_effect=respond) as send:
        response = _quote(sender)
    assert response.status_code == 200, response.text
    assert send.call_count == 2
    assert send.call_args_list[0].kwargs["reply_parameters"]["quote"] == "passage"
    assert send.call_args.kwargs["reply_parameters"] is None
    assert send.call_args.kwargs["reply_to_message_id"] == 101


def test_t5_quote_timeout_never_degrades(sender):
    with patch.object(
        TelegramAdapter, "send_message", side_effect=httpx.ReadTimeout("slow quote")
    ) as send:
        response = _quote(sender)
    assert send.call_count == 1
    assert send.call_args.kwargs["reply_parameters"]["quote"] == "passage"
    _assert_unknown(sender, response)


def test_t7_explicit_parse_mode_unknown(sender):
    with patch.object(
        TelegramAdapter, "send_message", side_effect=httpx.ReadTimeout("slow explicit mode")
    ) as send:
        response = _text(sender, parse_mode="HTML")
    assert send.call_count == 1
    assert send.call_args.kwargs["parse_mode"] == "HTML"
    _assert_unknown(sender, response)


@pytest.mark.parametrize("kind", ["photo", "document", "video"])
def test_t7_no_caption_unknown(sender, kind):
    with patch.object(
        TelegramAdapter, f"send_{kind}", side_effect=httpx.ReadTimeout("slow attachment")
    ) as send:
        response = _file(sender, kind, caption="")
    assert send.call_count == 1
    _assert_unknown(sender, response)


@pytest.mark.parametrize("media", [False, True], ids=["text", "caption"])
def test_plain_fallback_timeout_is_unknown_without_third_attempt(sender, media):
    with patch.object(
        TelegramAdapter,
        "send_photo" if media else "send_message",
        side_effect=[
            TelegramError("can't parse entities", 400),
            httpx.ReadTimeout("slow plain send"),
        ],
    ) as send:
        response = _file(sender) if media else _text(sender)
    assert send.call_count == 2
    _assert_unknown(sender, response)


@pytest.mark.parametrize("caption", ["", "**hello**"])
def test_presend_missing_file_stays_rejected(sender, caption):
    with patch.object(
        TelegramAdapter, "send_photo", side_effect=FileNotFoundError("missing attachment")
    ) as send:
        response = _file(sender, caption=caption)
    assert send.call_count == 1
    assert response.status_code == 400, response.text
    assert "delivery unknown" not in response.text
    assert any(
        "outcome=rejected" in line and "error=FileNotFoundError:" in line for line in sender.logs
    )
    sender.stopped.assert_called_once_with("sender", "12345")


@pytest.mark.parametrize("media", [False, True], ids=["text", "caption"])
@pytest.mark.parametrize("error", [ValueError("invalid parameters"), RuntimeError("unexpected")])
def test_other_errors_do_not_retry_or_claim_unknown(sender, media, error):
    with patch.object(
        TelegramAdapter, "send_photo" if media else "send_message", side_effect=error
    ) as send:
        response = _file(sender) if media else _text(sender)
    assert send.call_count == 1
    assert response.status_code == 502, response.text
    assert "delivery unknown" not in response.text


def test_broadcast_continues_after_unknown_middle_channel(sender, monkeypatch):
    monkeypatch.setattr(
        sender.app.state.agents,
        "list_approved_users",
        lambda _: [
            SimpleNamespace(status="approved", chat_id=chat) for chat in ("11111", "22222", "33333")
        ],
    )
    monkeypatch.setattr(sender.app.state.agents, "list_group_chats", lambda _: [])

    def respond(chat_id, *args, **kwargs):
        if chat_id == "22222":
            raise httpx.ReadTimeout("slow middle channel")
        return SimpleNamespace(message_id="message-" + chat_id)

    with patch.object(TelegramAdapter, "send_message", side_effect=respond) as send:
        response = sender.client.post(
            "/broker/broadcast",
            json={
                "agent_name": "sender",
                "content": "broadcast",
            },
        )
    assert [call.args[0] for call in send.call_args_list] == ["11111", "22222", "33333"]
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["count"] == 2
    assert body["delivery_unknown"] == ["22222"]
    entries = {entry["chat_id"]: entry for entry in body["deliveries"]}
    assert entries["11111"]["sent"] is True
    assert entries["33333"]["sent"] is True
    assert entries["22222"]["delivery"] == "unknown"
    assert entries["22222"]["error"].startswith("delivery unknown:")
    assert "do not resend" in entries["22222"]["error"]
    assert "check the chat first" in entries["22222"]["error"]
    assert [call.args for call in sender.stopped.call_args_list] == [
        ("sender", "11111"),
        ("sender", "22222"),
        ("sender", "33333"),
    ]
