"""Telegram error metadata retains throttling delays and old call signatures."""

import httpx
import pytest

from pinky_outreach.telegram import TelegramAdapter, TelegramError


METHODS = ["request", "send_photo", "send_document", "send_animation", "send_video", "send_voice"]


def _failure(tmp_path, method, payload):
    adapter = TelegramAdapter("fixture")
    adapter._client.close()
    adapter._client = httpx.Client(
        transport=httpx.MockTransport(lambda request: httpx.Response(429, json=payload)),
    )
    attachment = tmp_path / "attachment.bin"
    attachment.write_bytes(b"synthetic attachment")
    try:
        with pytest.raises(TelegramError) as caught:
            if method == "request":
                adapter._request("sendMessage", chat_id="12345", text="hello")
            else:
                getattr(adapter, method)("12345", str(attachment))
        return caught.value
    finally:
        adapter.close()


@pytest.mark.parametrize("method", METHODS)
def test_response_retry_after_parameters_take_precedence_over_description(tmp_path, method):
    error = _failure(tmp_path, method, {
        "ok": False, "error_code": 429, "description": "Too Many Requests: retry after 8",
        "parameters": {"retry_after": 13},
    })
    assert error.retry_after == 13
    assert error.rate_limited is True
    assert error.error_code == 429
    assert error.description == "Too Many Requests: retry after 8"


@pytest.mark.parametrize("method", METHODS)
def test_response_retry_after_is_parsed_from_mixed_case_description(tmp_path, method):
    error = _failure(tmp_path, method, {
        "ok": False, "error_code": 400,
        "description": "Bad Request: too Many Requests: RETRY AFTER 8",
    })
    assert error.retry_after == 8
    assert error.rate_limited is True


@pytest.mark.parametrize("parameters", [
    None, [], "invalid", {}, {"retry_after": 0}, {"retry_after": -1},
    {"retry_after": True}, {"retry_after": False}, {"retry_after": "13"},
    {"retry_after": None}, {"retry_after": 2.5},
])
def test_only_positive_integer_response_parameters_override_description(tmp_path, parameters):
    error = _failure(tmp_path, "request", {
        "ok": False, "error_code": 400, "description": "Too Many Requests: retry after 8",
        "parameters": parameters,
    })
    assert error.retry_after == 8
    assert error.rate_limited is True


def test_keyword_delay_marks_a_non_429_error_as_rate_limited():
    error = TelegramError("limited", 400, retry_after=13)
    assert error.retry_after == 13
    assert error.rate_limited is True


@pytest.mark.parametrize("description,code,limited,delay", [
    ("unclassified failure", 429, True, None),
    ("Bad Request: too Many Requests", 400, True, None),
    ("TOO MANY REQUESTS", 0, True, None),
    ("Bad Request: can't parse entities", 400, False, None),
    ("Bad Request: chat not found", 400, False, None),
    ("Forbidden", 403, False, None),
    ("retry after 8", 400, True, 8),
    ("Too Many Requests: retry after 0", 400, True, None),
])
def test_rate_limited_property_classifies_code_metadata_and_description(description, code, limited, delay):
    error = TelegramError(description, code)
    assert error.rate_limited is limited
    assert error.retry_after == delay


def test_existing_positional_telegram_error_construction_is_unchanged():
    error = TelegramError("description", 400)
    assert error.description == "description"
    assert error.error_code == 400
    assert error.args == ("Telegram API error 400: description",)
    assert str(error) == "Telegram API error 400: description"
    assert error.retry_after is None
    default = TelegramError("description")
    assert default.error_code == 0
    assert str(default) == "Telegram API error 0: description"


@pytest.mark.parametrize("description,code,delay,expected", [
    ("Bad Request: CAN'T PARSE ENTITIES: markup", 400, None, True),
    ("Bad Request: can't parse entities", 429, None, False),
    ("Bad Request: can't parse entities", 400, 8, False),
    ("Bad Request: chat not found", 400, None, False),
])
def test_markdown_rejection_is_specific_to_parse_errors(description, code, delay, expected):
    from pinky_daemon.api import _mdv2_rejected

    error = TelegramError(description, code, retry_after=delay)
    assert _mdv2_rejected(error) is expected

