"""Telegram throttling is one failed send, never a formatting retry."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from pinky_outreach.telegram import TelegramAdapter, TelegramError
from tests.test_api_delivery_unknown import _file, _quote, _text
from tests.test_api_delivery_unknown import sender as sender


def test_text_rate_limit_is_not_retried_plain_and_returns_delay(sender):
    # Keep the RED witness compatible with the old constructor so it reaches
    # the actual fallback, independently of the new keyword-argument tests.
    error = TelegramError("Too Many Requests: retry after 8", 429)
    error.retry_after = 8
    with patch.object(TelegramAdapter, "send_message", side_effect=error) as send:
        response = _text(sender)
    assert send.call_count == 1
    assert response.status_code == 429, response.text
    assert response.headers["Retry-After"] == "8"
    assert response.json()["detail"] == "rate limited by Telegram, retry after 8 s"
    sender.stopped.assert_called_once_with("sender", "12345")


def test_photo_400_rate_limit_is_not_reuploaded_and_returns_delay(sender):
    error = TelegramError("Bad Request: too Many Requests: retry after 8", 400)
    with patch.object(TelegramAdapter, "send_photo", side_effect=error) as send:
        response = _file(sender)
    assert send.call_count == 1
    assert response.status_code == 429, response.text
    assert response.headers["Retry-After"] == "8"
    assert response.json()["detail"] == "rate limited by Telegram, retry after 8 s"
    sender.stopped.assert_called_once_with("sender", "12345")


@pytest.mark.parametrize("media", [False, True], ids=["text", "photo"])
def test_markdown_parse_rejection_still_falls_back(sender, media):
    error = TelegramError("Bad Request: CAN'T PARSE ENTITIES: invalid markup", 400)
    with patch.object(
        TelegramAdapter, "send_photo" if media else "send_message",
        side_effect=[error, SimpleNamespace(message_id="800")],
    ) as send:
        response = _file(sender) if media else _text(sender)
    assert response.status_code == 200, response.text
    assert send.call_count == 2
    key = "caption_parse_mode" if media else "parse_mode"
    assert send.call_args_list[0].kwargs[key] == "MarkdownV2"
    assert send.call_args_list[1].kwargs.get(key) is None


@pytest.mark.parametrize("media", [False, True], ids=["text", "photo"])
@pytest.mark.parametrize("code,description", [(400, "Bad Request: chat not found"), (403, "Forbidden")])
def test_non_parse_telegram_error_is_not_retried(sender, media, code, description):
    with patch.object(
        TelegramAdapter, "send_photo" if media else "send_message",
        side_effect=TelegramError(description, code),
    ) as send:
        response = _file(sender) if media else _text(sender)
    assert send.call_count == 1
    assert response.status_code == 502, response.text


@pytest.mark.parametrize("media", [False, True], ids=["text", "photo"])
def test_rate_limit_without_numeric_delay_still_returns_429_once(sender, media):
    with patch.object(
        TelegramAdapter, "send_photo" if media else "send_message",
        side_effect=TelegramError("Bad Request: too Many Requests", 400),
    ) as send:
        response = _file(sender) if media else _text(sender)
    assert send.call_count == 1
    assert response.status_code == 429, response.text
    assert response.json()["detail"] == "rate limited by Telegram"
    assert "Retry-After" not in response.headers


@pytest.mark.parametrize("kind", ["photo", "document", "video"])
def test_media_rate_limit_never_reuploads(sender, kind):
    error = TelegramError("Too Many Requests", 429, retry_after=13)
    with patch.object(TelegramAdapter, f"send_{kind}", side_effect=error) as send:
        response = _file(sender, kind)
    assert send.call_count == 1
    assert response.status_code == 429
    assert response.headers["Retry-After"] == "13"
    assert response.json()["detail"] == "rate limited by Telegram, retry after 13 s"


@pytest.mark.parametrize("code", [429, 400])
def test_rate_limit_mentioning_quote_never_retries_without_quote(sender, code):
    error = TelegramError("Too Many Requests: retry after 8 for quote delivery", code)
    with patch.object(TelegramAdapter, "send_message", side_effect=error) as send:
        response = _quote(sender)
    assert send.call_count == 1
    assert send.call_args.kwargs["reply_parameters"]["quote"] == "passage"
    assert response.status_code == 429, response.text
    assert response.headers["Retry-After"] == "8"


def test_unquoted_markdown_rejection_still_falls_back_after_quote_rejection(sender):
    with patch.object(
        TelegramAdapter, "send_message",
        side_effect=[
            TelegramError("Bad Request: QUOTE_TEXT_INVALID", 400),
            TelegramError("Bad Request: can't parse entities: invalid markup", 400),
            SimpleNamespace(message_id="801"),
        ],
    ) as send:
        response = _quote(sender)
    assert response.status_code == 200, response.text
    assert send.call_count == 3
    assert send.call_args_list[0].kwargs["reply_parameters"]["quote"] == "passage"
    assert send.call_args_list[1].kwargs["reply_parameters"] is None
    assert send.call_args_list[1].kwargs["parse_mode"] == "MarkdownV2"
    assert send.call_args_list[2].kwargs["reply_parameters"] is None
    assert send.call_args_list[2].kwargs.get("parse_mode") is None
    assert send.call_args_list[2].kwargs["reply_to_message_id"] == 101


@pytest.mark.asyncio
async def test_non_rate_callback_error_is_reraised_without_plain_fallback(sender):
    error = TelegramError("Bad Request: chat not found", 400)
    with patch.object(TelegramAdapter, "send_message", side_effect=error) as send:
        with pytest.raises(TelegramError) as caught:
            await sender.app.state.broker.send_callback("sender", "telegram", "12345", "hello")
    assert caught.value is error
    assert send.call_count == 1
