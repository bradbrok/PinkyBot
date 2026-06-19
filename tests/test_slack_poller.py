"""Tests for BrokerSlackPoller (Socket Mode inbound).

These exercise the event-handling/normalization/self-filter logic directly via
``_handle_event`` (the cleanest unit surface) plus the no-app-token start guard.
They intentionally do NOT require ``slack_sdk`` — the SDK import is lazy inside
``start()``, and these paths never reach it.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_broker():
    broker = MagicMock()
    broker.handle_inbound = AsyncMock()
    return broker


def _make_poller(broker, *, app_token="xapp-test", bot_user_id="UBOT"):
    from pinky_daemon.pollers import BrokerSlackPoller

    adapter = MagicMock()
    adapter.get_bot_info.return_value = {"user_id": "UBOT", "user": "testbot", "team": "T1"}
    adapter.bot_token = "xoxb-test"
    poller = BrokerSlackPoller(adapter, "barsik", broker, registry=None, app_token=app_token)
    # Normally set during start() from auth.test; set directly for unit tests.
    poller._bot_user_id = bot_user_id
    return poller


def _event(event_body: dict) -> dict:
    """Wrap a Slack message event in the Socket Mode events_api payload shape."""
    return {"event": event_body}


class TestBrokerSlackPoller:
    @pytest.mark.asyncio
    async def test_user_message_routed_to_broker(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message",
            "user": "U123",
            "text": "hello barsik",
            "channel": "C999",
            "channel_type": "channel",
            "ts": "1718830000.000100",
        }))
        await asyncio.sleep(0)  # let the fire-and-forget delivery task run
        mock_broker.handle_inbound.assert_called_once()
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert bmsg.platform == "slack"
        assert bmsg.chat_id == "C999"
        assert bmsg.sender_id == "U123"
        assert bmsg.content == "hello barsik"
        assert bmsg.message_id == "1718830000.000100"
        assert bmsg.reply_to == ""
        assert bmsg.is_group is True

    @pytest.mark.asyncio
    async def test_own_message_filtered(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message", "user": "UBOT", "text": "my own msg",
            "channel": "C1", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_subtype_filtered(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message", "subtype": "message_changed",
            "user": "U123", "text": "edited", "channel": "C1", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_message_without_user_filtered(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message", "bot_id": "B999", "text": "from a bot",
            "channel": "C1", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_peer_user_message_delivered(self, mock_broker):
        """A different real user (not us) must be delivered (cross-fleet parity)."""
        poller = _make_poller(mock_broker, bot_user_id="UBOT")
        await poller._handle_event(_event({
            "type": "message", "user": "UPEER", "text": "ping",
            "channel": "C1", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_message_event_ignored(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({"type": "reaction_added", "user": "U123"}))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_reply_sets_reply_to(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message", "user": "U123", "text": "in thread",
            "channel": "D1", "ts": "2.2", "thread_ts": "1.1",
            "channel_type": "im",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_called_once()
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert bmsg.reply_to == "1.1"
        assert bmsg.is_group is False  # im == direct message

    @pytest.mark.asyncio
    async def test_file_attachment_normalized(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message", "user": "U123", "text": "see file",
            "channel": "C1", "ts": "3.3",
            "files": [{
                "id": "F1", "name": "doc.pdf",
                "url_private_download": "https://files.slack.com/doc.pdf",
                "mimetype": "application/pdf", "size": 1234,
            }],
        }))
        await asyncio.sleep(0)
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert len(bmsg.attachments) == 1
        att = bmsg.attachments[0]
        assert att["file_id"] == "F1"
        assert att["file_name"] == "doc.pdf"
        assert att["mime_type"] == "application/pdf"
        assert att["file_size"] == 1234

    @pytest.mark.asyncio
    async def test_start_without_app_token_is_noop(self, mock_broker):
        poller = _make_poller(mock_broker, app_token="")
        await poller.start()
        assert poller._client is None
        assert poller._running is False
        mock_broker.handle_inbound.assert_not_called()
