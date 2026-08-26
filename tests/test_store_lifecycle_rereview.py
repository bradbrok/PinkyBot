"""Review-only probes for the Lane A remediation head."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pinky_daemon import pollers


@pytest.mark.asyncio
async def test_slack_listener_cannot_publish_after_delivery_quiescence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A Slack listener admitted before stop must be part of the writer drain."""
    listener_entered = asyncio.Event()
    release_listener = asyncio.Event()
    client_closed = asyncio.Event()
    delivered = asyncio.Event()

    class Adapter:
        bot_token = "xoxb-review"

    class Broker:
        @staticmethod
        async def handle_inbound(_message) -> None:
            delivered.set()

    class Client:
        @staticmethod
        async def close() -> None:
            client_closed.set()

    poller = pollers.BrokerSlackPoller(
        Adapter(),
        "review-agent",
        Broker(),
        app_token="xapp-review",
    )
    poller._running = True
    poller._bot_user_id = "U_SELF"
    poller._client = Client()

    async def blocked_user_name(_user_id: str) -> str:
        listener_entered.set()
        await release_listener.wait()
        return "peer"

    async def empty_channel_title(_channel_id: str) -> str:
        return ""

    async def unchanged_text(text: str) -> str:
        return text

    monkeypatch.setattr(poller, "_resolve_user_name", blocked_user_name)
    monkeypatch.setattr(poller, "_resolve_channel_title", empty_channel_title)
    monkeypatch.setattr(poller, "_resolve_text_refs", unchanged_text)

    listener_task = asyncio.create_task(
        poller._handle_event(
            {
                "event": {
                    "type": "message",
                    "user": "U_PEER",
                    "channel": "C_REVIEW",
                    "channel_type": "im",
                    "text": "late",
                    "ts": "1.0",
                }
            }
        )
    )
    try:
        await asyncio.wait_for(listener_entered.wait(), timeout=1)
        poller.stop()

        await asyncio.wait_for(pollers.quiesce_delivery_tasks(), timeout=1)
        assert client_closed.is_set()

        release_listener.set()
        await asyncio.wait_for(listener_task, timeout=1)
        await asyncio.sleep(0)

        assert not delivered.is_set()
    finally:
        release_listener.set()
        await asyncio.gather(listener_task, return_exceptions=True)
        await pollers.quiesce_delivery_tasks()


@pytest.mark.asyncio
async def test_discord_stop_before_scheduled_start_keeps_ingress_closed() -> None:
    """A stop issued before the start task's first turn must not be overwritten."""
    delivered = asyncio.Event()
    fetch_count = 0

    class Adapter:
        @staticmethod
        def get_me() -> dict[str, str]:
            return {"id": "BOT", "username": "review"}

        @staticmethod
        def get_channel(_channel_id: str):
            return SimpleNamespace(title="", chat_type="dm")

        @staticmethod
        def get_messages(_channel_id: str, *, limit: int, after: str = ""):
            nonlocal fetch_count
            if limit == 1:
                return []
            fetch_count += 1
            if fetch_count > 1:
                return []
            return [
                SimpleNamespace(
                    sender="peer",
                    content="late",
                    timestamp=datetime.now(timezone.utc),
                    message_id="1",
                    reply_to="",
                    metadata={"author_id": "PEER", "is_bot": False},
                )
            ]

    class Broker:
        @staticmethod
        async def handle_inbound(_message) -> None:
            delivered.set()

    poller = pollers.BrokerDiscordPoller(
        Adapter(),
        "review-agent",
        Broker(),
        watched_channels=["C_REVIEW"],
    )
    pollers.start_poller(poller)
    poller.stop()

    try:
        try:
            await asyncio.wait_for(delivered.wait(), timeout=0.5)
        except TimeoutError:
            pass
        assert not delivered.is_set()
    finally:
        poller.stop()
        await asyncio.wait_for(pollers.quiesce_delivery_tasks(), timeout=1)
