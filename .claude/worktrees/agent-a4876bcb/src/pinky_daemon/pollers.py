"""Platform pollers — fetch inbound messages from each platform.

Each poller runs as an async task in the daemon's event loop,
periodically checking for new messages and feeding them to the
message handler.
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone

from pinky_daemon.message_handler import InboundMessage, MessageHandler
from pinky_outreach.telegram import TelegramAdapter, TelegramError


class TelegramPoller:
    """Polls Telegram Bot API for new messages.

    Uses long polling (getUpdates) to receive messages in near-realtime.
    Each poll blocks for `poll_timeout` seconds waiting for new messages.
    """

    def __init__(
        self,
        adapter: TelegramAdapter,
        handler: MessageHandler,
        *,
        poll_timeout: int = 30,
        poll_interval: float = 1.0,
        allowed_chat_ids: list[str] | None = None,
        event_callback=None,
    ) -> None:
        self._adapter = adapter
        self._handler = handler
        self._poll_timeout = poll_timeout
        self._poll_interval = poll_interval
        self._allowed_chats = set(allowed_chat_ids) if allowed_chat_ids else None
        self._event_callback = event_callback  # async fn(platform, chat_id, sender, content)
        self._running = False
        self._poll_count = 0

    async def start(self) -> None:
        """Start the polling loop."""
        self._running = True
        _log("telegram-poller: starting")

        # Verify bot connection
        try:
            me = self._adapter.get_me()
            _log(f"telegram-poller: connected as @{me.get('username', '?')}")
        except TelegramError as e:
            _log(f"telegram-poller: failed to connect: {e}")
            return

        while self._running:
            try:
                await self._poll_once()
            except TelegramError as e:
                _log(f"telegram-poller: error: {e}")
                await asyncio.sleep(5)  # Back off on error
            except Exception as e:
                _log(f"telegram-poller: unexpected error: {e}")
                await asyncio.sleep(5)

            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        """Single poll iteration."""
        # Run blocking HTTP call in thread pool
        messages = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._adapter.get_updates(timeout=self._poll_timeout),
        )

        self._poll_count += 1

        for msg in messages:
            # Filter by allowed chats
            if self._allowed_chats and msg.chat_id not in self._allowed_chats:
                _log(f"telegram-poller: ignoring message from chat {msg.chat_id}")
                continue

            # Convert to InboundMessage
            inbound = InboundMessage(
                platform="telegram",
                chat_id=msg.chat_id,
                sender_name=msg.sender,
                sender_id=msg.metadata.get("sender_id", ""),
                content=msg.content,
                timestamp=msg.timestamp,
                message_id=msg.message_id,
                chat_title=msg.metadata.get("chat_title", ""),
                is_group=msg.metadata.get("chat_type", "") in ("group", "supergroup"),
                metadata=msg.metadata,
            )

            _log(
                f"telegram-poller: message from {msg.sender} "
                f"in {msg.chat_id}: {msg.content[:50]}..."
            )

            # Fire and forget — handler manages concurrency
            asyncio.create_task(self._handler.handle(inbound))

            # Push event to autonomy engine
            if self._event_callback:
                try:
                    await self._event_callback(
                        platform="telegram",
                        chat_id=str(msg.chat_id),
                        sender=msg.sender,
                        content=msg.content,
                    )
                except Exception as e:
                    _log(f"telegram-poller: event callback error: {e}")

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        _log("telegram-poller: stopping")

    @property
    def poll_count(self) -> int:
        return self._poll_count

    @property
    def is_running(self) -> bool:
        return self._running


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
