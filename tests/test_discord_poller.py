"""Tests for BrokerDiscordPoller (REST-polling Discord inbound)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_outreach.discord import DiscordError, DiscordRateLimited
from pinky_outreach.types import Chat, Message, Platform


@pytest.fixture
def mock_adapter():
    """A mock DiscordAdapter with sensible defaults."""
    adapter = MagicMock()
    adapter.get_me.return_value = {"id": "bot-001", "username": "TestBot"}
    adapter.discover_text_channels.return_value = ["chan-A"]
    adapter.get_messages.return_value = []
    adapter.get_channel.return_value = Chat(
        platform=Platform.discord,
        chat_id="chan-A",
        title="general",
        chat_type="text",
    )
    return adapter


@pytest.fixture
def mock_broker():
    broker = MagicMock()
    broker.handle_inbound = AsyncMock()
    return broker


def _msg(
    *,
    msg_id: str,
    content: str,
    author_id: str = "user-1",
    sender: str = "brad",
    is_bot: bool = False,
    reply_to: str = "",
) -> Message:
    return Message(
        platform=Platform.discord,
        chat_id="chan-A",
        sender=sender,
        content=content,
        timestamp=datetime.fromisoformat("2026-05-05T12:00:00+00:00"),
        message_id=msg_id,
        reply_to=reply_to,
        is_outbound=is_bot,
        metadata={"author_id": author_id, "is_bot": is_bot},
    )


class TestBrokerDiscordPoller:
    """Mocked-adapter tests for the inbound Discord poller."""

    def _make_poller(self, mock_adapter, mock_broker, **kwargs):
        from pinky_daemon.pollers import BrokerDiscordPoller
        return BrokerDiscordPoller(
            mock_adapter, "barsik", mock_broker, registry=None,
            **kwargs,
        )

    @pytest.mark.asyncio
    async def test_priming_skips_history_replay(self, mock_adapter, mock_broker):
        """First sweep should treat existing latest message as the baseline (no replay)."""
        existing = _msg(msg_id="100", content="ancient")
        # Adapter returns the most recent message during priming, then nothing on poll
        mock_adapter.get_messages.side_effect = [
            [existing],  # priming call (limit=1)
            [],          # _poll_once call
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(prime_last_ids=True)
        await poller._poll_once()

        assert poller.watched_channels == ["chan-A"]
        assert poller._last_id["chan-A"] == "100"
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_new_message_routed_through_broker(self, mock_adapter, mock_broker):
        """Real inbound user message should produce a BrokerMessage with platform=discord."""
        baseline = _msg(msg_id="100", content="baseline")
        new_msg = _msg(msg_id="101", content="hello", author_id="user-9", sender="ryan")
        mock_adapter.get_messages.side_effect = [
            [baseline],     # priming
            [new_msg],      # poll
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(prime_last_ids=True)
        await poller._poll_once()

        # Allow the fire-and-forget task to settle
        await asyncio.sleep(0)

        assert mock_broker.handle_inbound.await_count == 1
        delivered = mock_broker.handle_inbound.await_args.args[0]
        assert delivered.platform == "discord"
        assert delivered.chat_id == "chan-A"
        assert delivered.message_id == "101"
        assert delivered.content == "hello"
        assert delivered.sender_name == "ryan"
        assert delivered.sender_id == "user-9"
        assert delivered.agent_name == "barsik"
        assert poller._last_id["chan-A"] == "101"

    @pytest.mark.asyncio
    async def test_bot_messages_skipped(self, mock_adapter, mock_broker):
        """is_bot=True or author_id matching the bot's own id must be filtered out."""
        baseline = _msg(msg_id="100", content="baseline")
        bot_self = _msg(
            msg_id="101", content="self echo",
            author_id="bot-001", sender="TestBot", is_bot=False,
        )
        bot_other = _msg(
            msg_id="102", content="other bot",
            author_id="bot-other", sender="OtherBot", is_bot=True,
        )
        real_user = _msg(
            msg_id="103", content="real",
            author_id="user-9", sender="ryan",
        )
        # Discord returns newest-first; poller reverses to chronological for delivery.
        mock_adapter.get_messages.side_effect = [
            [baseline],
            [real_user, bot_other, bot_self],
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        # Set bot id manually so we don't depend on start()
        poller._bot_user_id = "bot-001"
        await poller._refresh_channels(prime_last_ids=True)
        await poller._poll_once()

        await asyncio.sleep(0)

        # Only the real-user message should be delivered
        assert mock_broker.handle_inbound.await_count == 1
        delivered = mock_broker.handle_inbound.await_args.args[0]
        assert delivered.message_id == "103"
        # last_id advances past all of them so we don't refetch
        assert poller._last_id["chan-A"] == "103"

    @pytest.mark.asyncio
    async def test_explicit_watched_channels_override_discovery(self, mock_adapter, mock_broker):
        """If settings.watched_channels is set, discover_text_channels must NOT be called."""
        # Empty channel = "0" sentinel
        mock_adapter.get_messages.return_value = []

        poller = self._make_poller(
            mock_adapter, mock_broker,
            watched_channels=["explicit-1", "explicit-2"],
        )
        await poller._refresh_channels(prime_last_ids=True)

        assert sorted(poller.watched_channels) == ["explicit-1", "explicit-2"]
        mock_adapter.discover_text_channels.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_channel_primes_with_zero_sentinel(self, mock_adapter, mock_broker):
        """A channel with no messages at priming time should record last_id='0'."""
        mock_adapter.get_messages.return_value = []  # priming finds nothing

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(prime_last_ids=True)

        assert poller._last_id["chan-A"] == "0"

    @pytest.mark.asyncio
    async def test_poll_drops_after_filter_for_zero_sentinel(self, mock_adapter, mock_broker):
        """When last_id='0' (empty channel), the poll must NOT pass after=0 to the adapter."""
        # Priming: empty
        # Poll: a real first message arrives
        first = _msg(msg_id="500", content="first ever", author_id="user-9")
        mock_adapter.get_messages.side_effect = [
            [],          # priming
            [first],     # poll
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(prime_last_ids=True)
        await poller._poll_once()

        await asyncio.sleep(0)

        # Inspect the second call (the actual poll)
        poll_call = mock_adapter.get_messages.call_args_list[1]
        kwargs = poll_call.kwargs
        # `after` should be empty (sentinel dropped), not "0"
        assert kwargs.get("after", "") == ""
        # And the first message did get delivered
        assert mock_broker.handle_inbound.await_count == 1

    @pytest.mark.asyncio
    async def test_messages_routed_in_chronological_order(self, mock_adapter, mock_broker):
        """Discord returns newest-first; poller must reverse so broker sees chronological order."""
        baseline = _msg(msg_id="100", content="baseline")
        # Discord returns newest-first
        newer = _msg(msg_id="103", content="third")
        middle = _msg(msg_id="102", content="second")
        older = _msg(msg_id="101", content="first")
        mock_adapter.get_messages.side_effect = [
            [baseline],
            [newer, middle, older],
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(prime_last_ids=True)
        await poller._poll_once()

        await asyncio.sleep(0)

        delivered_ids = [
            call.args[0].message_id
            for call in mock_broker.handle_inbound.await_args_list
        ]
        assert delivered_ids == ["101", "102", "103"]

    @pytest.mark.asyncio
    async def test_discovery_failure_keeps_existing_set(self, mock_adapter, mock_broker):
        """A transient discovery failure must not blank the watch list."""
        mock_adapter.get_messages.return_value = []

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(prime_last_ids=True)
        # Now simulate failure on next discovery
        mock_adapter.discover_text_channels.side_effect = DiscordError(
            "transient", status_code=502,
        )

        await poller._refresh_channels(prime_last_ids=False)
        assert poller.watched_channels == ["chan-A"]  # unchanged

    @pytest.mark.asyncio
    async def test_rate_limit_propagates_for_outer_sleep(self, mock_adapter, mock_broker):
        """A 429 mid-poll must propagate so the outer loop can sleep retry_after."""
        baseline = _msg(msg_id="100", content="baseline")
        mock_adapter.get_messages.side_effect = [
            [baseline],
            DiscordRateLimited(retry_after=0.5),
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(prime_last_ids=True)
        with pytest.raises(DiscordRateLimited):
            await poller._poll_once()

    def test_poll_interval_floor(self, mock_adapter, mock_broker):
        """Sub-quarter-second intervals must be clamped to keep us under budget."""
        poller = self._make_poller(
            mock_adapter, mock_broker, poll_interval=0.05,
        )
        assert poller._poll_interval == 0.25

    def test_stop_clears_running_flag(self, mock_adapter, mock_broker):
        poller = self._make_poller(mock_adapter, mock_broker)
        poller._running = True
        poller.stop()
        assert poller.is_running is False
