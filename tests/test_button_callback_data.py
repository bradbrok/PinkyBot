"""Button callback data may contain a dot across send and inbound paths."""

from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from pinky_outreach.slack import SlackAdapter
from pinky_outreach.telegram import TelegramAdapter
from tests import test_slack_buttons as slack_buttons
from tests import test_telegram_buttons as telegram_buttons
from tests.test_inbound_message_fields import gateway as gateway
from tests.test_inbound_message_fields import private_home as private_home

pytestmark = pytest.mark.real_auth

DOTTED_DATA = "request:1730312345.123456:count:3"


def _send(gateway, monkeypatch, platform, data):
    gateway.agents.set_token("sample", platform, "fixture")
    sent = Mock(return_value=SimpleNamespace(message_id="500"))
    adapter = TelegramAdapter if platform == "telegram" else SlackAdapter
    monkeypatch.setattr(adapter, "send_message", sent)
    response = telegram_buttons._signed_post(gateway, "/broker/send", {
        "agent_name": "sample", "platform": platform,
        "chat_id": "101" if platform == "telegram" else "CCHANNEL",
        "content": "Choose an option", "buttons": [{"text": "Choose", "data": data}],
    })
    return response, sent


@pytest.mark.parametrize("platform", ["telegram", "slack"])
@pytest.mark.parametrize("data", [DOTTED_DATA, "." * 64], ids=["timestamp", "length-limit"])
def test_send_preserves_dotted_callback_data(gateway, monkeypatch, platform, data):
    response, sent = _send(gateway, monkeypatch, platform, data)
    assert response.status_code == 200, response.text
    sent.assert_called_once()
    kwargs = sent.call_args.kwargs
    if platform == "telegram":
        assert kwargs["reply_markup"]["inline_keyboard"][0][0]["callback_data"] == data
    else:
        button = kwargs["blocks"][1]["elements"][0]
        assert button["action_id"] == "pinky_btn:" + data
        assert button["value"] == data


@pytest.mark.parametrize("platform", ["telegram", "slack"])
@pytest.mark.parametrize("data", ["." * 65, "a b", "a/b", "café"],
                         ids=["too-long", "space", "slash", "non-ascii"])
def test_send_still_refuses_invalid_callback_data(gateway, monkeypatch, platform, data):
    response, sent = _send(gateway, monkeypatch, platform, data)
    assert response.status_code == 400
    assert response.json()["detail"] == "invalid buttons: invalid callback data"
    sent.assert_not_called()


@pytest.mark.asyncio
async def test_slack_tap_delivers_exact_dotted_callback_data():
    state = slack_buttons._state()
    await slack_buttons._tap(state, slack_buttons._payload(data=DOTTED_DATA))
    state.broker.handle_inbound.assert_awaited_once()
    message = state.broker.handle_inbound.call_args.args[0]
    assert message.metadata["button_data"] == DOTTED_DATA
    assert message.content == "Button tapped: " + DOTTED_DATA


def test_telegram_tap_delivers_exact_dotted_callback_data():
    callback = telegram_buttons._callback(data=DOTTED_DATA)
    with telegram_buttons._adapter([telegram_buttons._update(callback)]) as state:
        messages = state.adapter.get_updates()
        assert len(messages) == 1
        assert messages[0].metadata["button_data"] == DOTTED_DATA
        assert messages[0].content == "Button tapped: " + DOTTED_DATA


def test_telegram_callback_id_still_refuses_a_dot():
    callback = telegram_buttons._callback(id="tap.1")
    with telegram_buttons._adapter([telegram_buttons._update(callback)]) as state:
        assert state.adapter.get_updates() == []
