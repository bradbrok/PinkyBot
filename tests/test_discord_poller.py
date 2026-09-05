"""Tests for BrokerDiscordPoller (REST-polling Discord inbound)."""

from __future__ import annotations

import asyncio
from datetime import datetime
from unittest.mock import AsyncMock, MagicMock

import pytest

from pinky_outreach.discord import DiscordError, DiscordRateLimited
from pinky_outreach.types import Chat, Message, Platform
from tests.test_poller_connect_retry import InboundClock, until


@pytest.mark.parametrize("failure", ["api", "transport", "unknown", "rate_limit"])
async def test_discord_connect_retries_transient_then_polls(mock_adapter, mock_broker, failure):
    """T10: transport and unknown failures must survive the connect boundary."""
    import httpx

    from pinky_daemon.pollers import BrokerDiscordPoller

    errors = {
        "api": DiscordError("gateway", 502),
        "transport": httpx.ConnectError("offline"),
        "unknown": RuntimeError("unexpected"),
        "rate_limit": DiscordRateLimited(0.03),
    }
    mock_adapter.get_me.side_effect = [errors[failure], {"id": "test-bot", "username": "test"}]
    poller = BrokerDiscordPoller(mock_adapter, "retry-test", mock_broker)
    poller._backoff_for = lambda _: 0.01
    task = asyncio.create_task(poller.start())
    try:
        async with asyncio.timeout(5):
            while poller.poll_count == 0:
                await asyncio.sleep(0.005)
        assert mock_adapter.get_me.call_count == 2
        mock_adapter.get_me.assert_called_with(http_timeout=10.0)
        assert poller.connect_attempts == 0
        assert poller.is_running
    finally:
        poller.stop()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


async def test_discord_terminal_credential_error_alerts_and_stops(mock_adapter, mock_broker):
    """T10: a bad Discord token alerts immediately without retrying."""
    from pinky_daemon.pollers import BrokerDiscordPoller

    notify = AsyncMock(return_value=True)
    mock_adapter.get_me.side_effect = DiscordError("Unauthorized", 401)
    poller = BrokerDiscordPoller(mock_adapter, "retry-test", mock_broker)
    poller._owner_notify = notify  # T9 separately pins constructor wiring
    try:
        await asyncio.wait_for(poller.start(), 5)
        assert mock_adapter.get_me.call_count == 1
        assert not poller.is_running
        notify.assert_awaited_once()
        assert "retry-test" in notify.call_args.args[1]
        assert "bad token" in notify.call_args.args[1]
    finally:
        poller.stop()


async def test_discord_initial_discovery_transport_failure_retries(
    mock_adapter, mock_broker, monkeypatch
):
    import httpx

    from pinky_daemon import pollers

    mock_adapter.discover_text_channels.side_effect = [httpx.ConnectError("dns"), ["chan-A"]]
    poller = pollers.BrokerDiscordPoller(mock_adapter, "retry-test", mock_broker)
    poller._poll_interval = 0.005
    original_sleep = pollers._sleep_until_stopped
    sweep_finished = asyncio.Event()

    async def fast_sleep(event, delay):
        if poller.poll_count:
            sweep_finished.set()
        await original_sleep(event, min(delay, 0.01))

    monkeypatch.setattr(pollers, "_sleep_until_stopped", fast_sleep)
    task = asyncio.create_task(poller.start())
    try:
        await asyncio.wait_for(sweep_finished.wait(), 5)
        assert mock_adapter.discover_text_channels.call_count == 2
        assert mock_adapter.get_messages.call_count >= 2
        assert poller.is_running
    finally:
        poller.stop()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


@pytest.mark.parametrize("failure", ["api", "transport", "rate_limit"])
async def test_discord_poll_error_stall_alerts_once(
    mock_adapter, mock_broker, monkeypatch, failure
):
    import httpx

    from pinky_daemon import pollers

    errors = {
        "api": DiscordError("gateway", 502),
        "transport": httpx.ConnectError("offline"),
        "rate_limit": DiscordRateLimited(0.01),
    }
    notify = AsyncMock(return_value=True)
    interval = 300
    monkeypatch.setattr(pollers, "_INBOUND_STALL_ALERT_AFTER", interval)
    clock = InboundClock(monkeypatch)
    mock_adapter.get_messages.side_effect = [[], *([errors[failure]] * 1000)]
    poller = pollers.BrokerDiscordPoller(
        mock_adapter, "retry-test", mock_broker, owner_notify=notify
    )
    poller._poll_interval = 0.005
    task = asyncio.create_task(poller.start())
    try:
        await clock.cycles()
        notify.assert_not_awaited()
        clock.advance(interval)
        await until(lambda: poller.stall_alerted)
        assert poller.inbound_stalled_s >= interval
        clock.advance(5 * interval)
        await clock.cycles()
        notify.assert_awaited_once()
        mock_adapter.get_messages.side_effect = None
        await until(lambda: not poller.stall_alerted)
    finally:
        poller.stop()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 5)


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
        await poller._refresh_channels(verbose=True)
        await poller._poll_once()

        assert poller.watched_channels == ["chan-A"]
        assert poller._last_id["chan-A"] == "100"
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_priming_failure_does_not_replay_history(self, mock_adapter, mock_broker):
        """A channel whose priming fetch failed must not be polled floorless.

        Polling with no after-filter would replay the latest 50 historical
        messages as fresh inbound. The channel stays parked until the next
        discovery re-primes it.
        """
        history = [
            _msg(msg_id=str(200 - i), content=f"old-{i}", author_id="user-9")
            for i in range(3)
        ]
        mock_adapter.get_messages.side_effect = [
            DiscordError("transient", status_code=502),  # priming fails
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(verbose=True)
        assert poller.watched_channels == ["chan-A"]
        assert "chan-A" not in poller._last_id

        # The channel is in the watch set, but the poll must skip it
        mock_adapter.get_messages.side_effect = [history]
        await poller._poll_once()
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_not_called()

        # Next discovery re-primes it and polling resumes with a floor
        latest = _msg(msg_id="200", content="latest")
        new_msg = _msg(msg_id="201", content="fresh", author_id="user-9")
        mock_adapter.get_messages.side_effect = [
            [latest],    # re-prime
            [new_msg],   # poll
        ]
        await poller._refresh_channels(verbose=False)
        await poller._poll_once()
        await asyncio.sleep(0)

        assert mock_broker.handle_inbound.await_count == 1
        assert mock_broker.handle_inbound.await_args.args[0].message_id == "201"

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
        await poller._refresh_channels(verbose=True)
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
    async def test_self_bot_filtered_peer_bots_delivered(self, mock_adapter, mock_broker):
        """Only OUR bot's own messages are filtered. Peer agents/bots reach us.

        Cross-fleet messaging requires that other agents (Pulse, Misha, etc.)
        can talk to us. The only message we must drop is one we authored, to
        avoid self-reply loops. is_bot=True alone is NOT a filter criterion.
        """
        baseline = _msg(msg_id="100", content="baseline")
        bot_self = _msg(
            msg_id="101", content="self echo",
            author_id="bot-001", sender="TestBot", is_bot=True,
        )
        bot_peer = _msg(
            msg_id="102", content="hello from pulse",
            author_id="bot-other", sender="Pulse", is_bot=True,
        )
        real_user = _msg(
            msg_id="103", content="real",
            author_id="user-9", sender="ryan",
        )
        # Discord returns newest-first; poller reverses to chronological for delivery.
        mock_adapter.get_messages.side_effect = [
            [baseline],
            [real_user, bot_peer, bot_self],
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        # Set bot id manually so we don't depend on start()
        poller._bot_user_id = "bot-001"
        await poller._refresh_channels(verbose=True)
        await poller._poll_once()

        await asyncio.sleep(0)

        # Both peer-bot and real-user should be delivered; only self-bot dropped.
        assert mock_broker.handle_inbound.await_count == 2
        delivered_ids = {
            call.args[0].message_id
            for call in mock_broker.handle_inbound.await_args_list
        }
        assert delivered_ids == {"102", "103"}
        # last_id advances past all of them so we don't refetch
        assert poller._last_id["chan-A"] == "103"

    @pytest.mark.asyncio
    async def test_bot_message_without_author_id_is_skipped_defensively(
        self, mock_adapter, mock_broker,
    ):
        """is_bot=True with empty author_id can't be deduped vs self → skip."""
        baseline = _msg(msg_id="100", content="baseline")
        # Adapter normally always sets author_id, but if it's missing on a bot
        # message we have no way to tell whether it's us or a peer — be safe.
        no_author_bot = _msg(
            msg_id="101", content="anon bot",
            author_id="", sender="???", is_bot=True,
        )
        mock_adapter.get_messages.side_effect = [
            [baseline],
            [no_author_bot],
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        poller._bot_user_id = "bot-001"
        await poller._refresh_channels(verbose=True)
        await poller._poll_once()
        await asyncio.sleep(0)

        mock_broker.handle_inbound.assert_not_called()
        assert poller._last_id["chan-A"] == "101"

    @pytest.mark.asyncio
    async def test_explicit_watched_channels_override_discovery(self, mock_adapter, mock_broker):
        """If settings.watched_channels is set, discover_text_channels must NOT be called."""
        # Empty channel = "0" sentinel
        mock_adapter.get_messages.return_value = []

        poller = self._make_poller(
            mock_adapter, mock_broker,
            watched_channels=["explicit-1", "explicit-2"],
        )
        await poller._refresh_channels(verbose=True)

        assert sorted(poller.watched_channels) == ["explicit-1", "explicit-2"]
        mock_adapter.discover_text_channels.assert_not_called()

    @pytest.mark.asyncio
    async def test_empty_channel_primes_with_zero_sentinel(self, mock_adapter, mock_broker):
        """A channel with no messages at priming time should record last_id='0'."""
        mock_adapter.get_messages.return_value = []  # priming finds nothing

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(verbose=True)

        assert poller._last_id["chan-A"] == "0"

    @pytest.mark.asyncio
    async def test_priming_delivers_fresh_first_test_message(
        self, mock_adapter, mock_broker,
    ):
        """Fresh (<30s) non-bot message at prime time → backed-off floor so it gets delivered.

        Scenario: bot joins server, user sends test message, then discovery
        sweep runs ~seconds later. Without this fix, the test message becomes
        the floor and is silently dropped.
        """
        from datetime import datetime, timezone
        fresh_msg = Message(
            platform=Platform.discord,
            chat_id="chan-A",
            sender="brad",
            content="hi bot",
            # Brand new — well within the 30s freshness window
            timestamp=datetime.now(timezone.utc),
            message_id="200",
            metadata={"author_id": "user-1", "is_bot": False},
        )
        mock_adapter.get_messages.side_effect = [
            [fresh_msg],  # priming
            [fresh_msg],  # poll picks it up because floor was backed off to "199"
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(verbose=True)

        # Floor must be id-1 so the next poll fetches this message
        assert poller._last_id["chan-A"] == "199"

        await poller._poll_once()
        await asyncio.sleep(0)

        # The fresh test message should have been delivered through the broker
        assert mock_broker.handle_inbound.await_count == 1
        delivered = mock_broker.handle_inbound.await_args.args[0]
        assert delivered.message_id == "200"
        assert delivered.content == "hi bot"

    @pytest.mark.asyncio
    async def test_priming_old_message_uses_message_as_floor(
        self, mock_adapter, mock_broker,
    ):
        """Stale (>30s) most-recent message → use it as floor, NOT delivered."""
        from datetime import datetime, timezone
        old_msg = Message(
            platform=Platform.discord,
            chat_id="chan-A",
            sender="brad",
            content="ancient",
            timestamp=datetime(2020, 1, 1, tzinfo=timezone.utc),
            message_id="100",
            metadata={"author_id": "user-1", "is_bot": False},
        )
        mock_adapter.get_messages.side_effect = [
            [old_msg],  # priming
            [],         # poll finds nothing new
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(verbose=True)
        await poller._poll_once()
        await asyncio.sleep(0)

        # Floor is the message id itself — replay-prevention behavior preserved
        assert poller._last_id["chan-A"] == "100"
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_priming_fresh_peer_bot_backs_off_and_delivers(
        self, mock_adapter, mock_broker,
    ):
        """Fresh peer-bot message at prime time → backed-off floor → delivered.

        Peer agents/bots are first-class senders. If we discover a channel
        right after a peer-bot posts (the cross-fleet first-contact race),
        their message must be delivered, same as a fresh human's would be.
        """
        from datetime import datetime, timezone
        fresh_peer_bot = Message(
            platform=Platform.discord,
            chat_id="chan-A",
            sender="Pulse",
            content="hi from pulse",
            timestamp=datetime.now(timezone.utc),
            message_id="300",
            metadata={"author_id": "bot-other", "is_bot": True},
        )
        mock_adapter.get_messages.side_effect = [
            [fresh_peer_bot],   # priming
            [fresh_peer_bot],   # poll picks it up after floor backed off
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        # Our bot id is different from the peer's
        poller._bot_user_id = "bot-self"
        await poller._refresh_channels(verbose=True)

        # Floor must be backed off so the next poll fetches the peer-bot msg
        assert poller._last_id["chan-A"] == "299"

        await poller._poll_once()
        await asyncio.sleep(0)

        # Peer-bot message delivered through broker
        assert mock_broker.handle_inbound.await_count == 1
        delivered = mock_broker.handle_inbound.await_args.args[0]
        assert delivered.message_id == "300"
        assert delivered.content == "hi from pulse"

    @pytest.mark.asyncio
    async def test_priming_fresh_self_bot_uses_as_floor(
        self, mock_adapter, mock_broker,
    ):
        """Fresh OWN-bot message at prime time → use as floor, not delivered.

        Loop prevention: even if we just posted, we must not re-deliver our
        own message back to ourselves through the broker.
        """
        from datetime import datetime, timezone
        fresh_self = Message(
            platform=Platform.discord,
            chat_id="chan-A",
            sender="Self",
            content="i just said this",
            timestamp=datetime.now(timezone.utc),
            message_id="400",
            metadata={"author_id": "bot-self", "is_bot": True},
        )
        mock_adapter.get_messages.side_effect = [
            [fresh_self],  # priming
            [],            # nothing new on poll
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        poller._bot_user_id = "bot-self"
        await poller._refresh_channels(verbose=True)
        await poller._poll_once()
        await asyncio.sleep(0)

        # Self message used as the floor as-is — not delivered
        assert poller._last_id["chan-A"] == "400"
        mock_broker.handle_inbound.assert_not_called()

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
        await poller._refresh_channels(verbose=True)
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
        await poller._refresh_channels(verbose=True)
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
        await poller._refresh_channels(verbose=True)
        # Now simulate failure on next discovery
        mock_adapter.discover_text_channels.side_effect = DiscordError(
            "transient", status_code=502,
        )

        await poller._refresh_channels(verbose=False)
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
        await poller._refresh_channels(verbose=True)
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

    @pytest.mark.asyncio
    async def test_get_channel_cached_across_burst(self, mock_adapter, mock_broker):
        """A burst of N messages on one channel must trigger only ONE get_channel call.

        Regression for the burst pathology Pushok flagged in PR #383: prior code
        looked up channel metadata inside the per-message loop, fanning out to N
        round-trips for a chatty channel. Now hoisted to once per channel per poll
        sweep with a discovery-cycle cache.
        """
        baseline = _msg(msg_id="100", content="baseline")
        burst = [
            _msg(msg_id=f"{200 + i}", content=f"msg-{i}", author_id="user-9")
            for i in range(5)
        ]
        # Discord returns newest-first
        mock_adapter.get_messages.side_effect = [
            [baseline],          # priming
            list(reversed(burst)),  # poll: newest-first
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(verbose=True)
        await poller._poll_once()

        await asyncio.sleep(0)

        assert mock_broker.handle_inbound.await_count == 5
        # Hoisted lookup: one call per channel per poll, not per message
        assert mock_adapter.get_channel.call_count == 1

    @pytest.mark.asyncio
    async def test_get_channel_cache_reused_across_polls(self, mock_adapter, mock_broker):
        """Cache survives across polls within a discovery cycle."""
        baseline = _msg(msg_id="100", content="baseline")
        first = _msg(msg_id="101", content="first", author_id="user-9")
        second = _msg(msg_id="102", content="second", author_id="user-9")
        mock_adapter.get_messages.side_effect = [
            [baseline],     # priming
            [first],        # poll 1
            [second],       # poll 2
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(verbose=True)
        await poller._poll_once()
        await poller._poll_once()

        await asyncio.sleep(0)

        # Still one get_channel call total — cached after first miss
        assert mock_adapter.get_channel.call_count == 1

    @pytest.mark.asyncio
    async def test_get_channel_cache_invalidated_on_rediscovery(
        self, mock_adapter, mock_broker,
    ):
        """A discovery refresh drops the cache so renames/type-changes are picked up."""
        baseline = _msg(msg_id="100", content="baseline")
        first = _msg(msg_id="101", content="first", author_id="user-9")
        second = _msg(msg_id="102", content="second", author_id="user-9")
        mock_adapter.get_messages.side_effect = [
            [baseline],   # priming (during first refresh)
            [first],      # poll 1
            [second],     # poll 2 (after rediscovery)
        ]

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(verbose=True)
        await poller._poll_once()
        # Force a rediscovery — should invalidate the cache
        await poller._refresh_channels(verbose=False)
        await poller._poll_once()

        await asyncio.sleep(0)

        # First poll → 1 lookup, rediscovery clears cache, second poll → 1 more lookup
        assert mock_adapter.get_channel.call_count == 2

    @pytest.mark.asyncio
    async def test_broker_delivery_failure_logged(self, mock_adapter, mock_broker, capsys):
        """If broker.handle_inbound raises, the failure must be visible in poller logs."""
        baseline = _msg(msg_id="100", content="baseline")
        new_msg = _msg(msg_id="101", content="boom", author_id="user-9")
        mock_adapter.get_messages.side_effect = [[baseline], [new_msg]]

        async def _raise(*_args, **_kwargs):
            raise RuntimeError("broker exploded")

        mock_broker.handle_inbound = AsyncMock(side_effect=_raise)

        poller = self._make_poller(mock_adapter, mock_broker)
        await poller._refresh_channels(verbose=True)
        await poller._poll_once()

        # Let the fire-and-forget task settle and the done_callback fire.
        await asyncio.sleep(0)
        await asyncio.sleep(0)

        captured = capsys.readouterr()
        assert "broker delivery failed" in captured.err
        assert "broker exploded" in captured.err
