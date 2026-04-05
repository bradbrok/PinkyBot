"""Platform pollers — fetch inbound messages from each platform.

Each poller runs as an async task in the daemon's event loop,
periodically checking for new messages and feeding them to the
message handler (legacy) or message broker (new).
"""

from __future__ import annotations

import asyncio
import sys

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


class BrokerTelegramPoller:
    """Polls Telegram for a specific agent's bot token, routes through MessageBroker.

    Unlike TelegramPoller which uses a single handler, this poller:
    - Is bound to a specific agent (one poller per agent bot token)
    - Routes messages through the MessageBroker for approval checks
    - Tracks group chat join/leave via my_chat_member updates
    """

    def __init__(
        self,
        adapter: TelegramAdapter,
        agent_name: str,
        broker,  # MessageBroker
        registry=None,  # AgentRegistry — for group chat tracking
        *,
        poll_timeout: int = 30,
        poll_interval: float = 1.0,
        event_callback=None,
    ) -> None:
        from pinky_daemon.broker import BrokerMessage, MessageBroker
        self._BrokerMessage = BrokerMessage

        self._adapter = adapter
        self._agent_name = agent_name
        self._broker: MessageBroker = broker
        self._registry = registry
        self._poll_timeout = poll_timeout
        self._poll_interval = poll_interval
        self._event_callback = event_callback
        self._running = False
        self._poll_count = 0
        self._bot_username = ""

    async def start(self) -> None:
        """Start the polling loop."""
        self._running = True
        _log(f"broker-poller[{self._agent_name}]: starting")

        try:
            me = self._adapter.get_me()
            self._bot_username = me.get("username", "?")
            _log(f"broker-poller[{self._agent_name}]: connected as @{self._bot_username}")
        except TelegramError as e:
            _log(f"broker-poller[{self._agent_name}]: failed to connect: {e}")
            return

        while self._running:
            try:
                await self._poll_once()
            except TelegramError as e:
                _log(f"broker-poller[{self._agent_name}]: error: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                _log(f"broker-poller[{self._agent_name}]: unexpected error: {e}")
                await asyncio.sleep(5)

            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        """Single poll iteration — routes messages through broker."""
        messages = await asyncio.get_event_loop().run_in_executor(
            None,
            lambda: self._adapter.get_updates(timeout=self._poll_timeout),
        )

        self._poll_count += 1

        for msg in messages:
            chat_type = msg.metadata.get("chat_type", "")
            is_group = chat_type in ("group", "supergroup")

            # Track group chats
            if is_group and self._registry:
                try:
                    self._registry.upsert_group_chat(
                        agent_name=self._agent_name,
                        chat_id=msg.chat_id,
                        chat_title=msg.metadata.get("chat_title", ""),
                        chat_type=chat_type,
                    )
                except Exception as e:
                    _log(f"broker-poller[{self._agent_name}]: group chat tracking error: {e}")

            # Build broker message
            broker_msg = self._BrokerMessage(
                platform="telegram",
                chat_id=msg.chat_id,
                sender_name=msg.sender,
                sender_id=msg.metadata.get("sender_id", ""),
                content=msg.content,
                agent_name=self._agent_name,
                message_id=msg.message_id,
                chat_title=msg.metadata.get("chat_title", ""),
                is_group=is_group,
                reply_to=msg.reply_to,
                metadata=msg.metadata,
                attachments=msg.metadata.get("attachments", []),
            )

            _log(
                f"broker-poller[{self._agent_name}]: message from {msg.sender} "
                f"in {msg.chat_id}: {msg.content[:50]}..."
            )

            # Route through broker (fire and forget)
            asyncio.create_task(self._broker.handle_inbound(broker_msg))

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
                    _log(f"broker-poller[{self._agent_name}]: event callback error: {e}")

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        _log(f"broker-poller[{self._agent_name}]: stopping")

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def poll_count(self) -> int:
        return self._poll_count

    @property
    def is_running(self) -> bool:
        return self._running


class BrokeriMessagePoller:
    """Polls macOS Messages chat.db for iMessage, routes through MessageBroker.

    Reads ~/Library/Messages/chat.db for new inbound messages.
    Requires Full Disk Access for the Python process.
    """

    def __init__(
        self,
        adapter,  # iMessageAdapter
        agent_name: str,
        broker,  # MessageBroker
        *,
        poll_interval: float = 3.0,
        event_callback=None,
    ) -> None:
        from pinky_daemon.broker import BrokerMessage
        self._BrokerMessage = BrokerMessage

        self._adapter = adapter
        self._agent_name = agent_name
        self._broker = broker
        self._poll_interval = poll_interval
        self._event_callback = event_callback
        self._running = False
        self._poll_count = 0

    async def start(self) -> None:
        """Start the polling loop."""
        self._running = True
        _log(f"imessage-poller[{self._agent_name}]: starting")

        if not self._adapter.can_receive:
            _log(
                f"imessage-poller[{self._agent_name}]: chat.db not accessible. "
                "Grant Full Disk Access to Python in System Settings."
            )
            _log(f"imessage-poller[{self._agent_name}]: send-only mode (no inbound)")
            # Don't return — keep running so outbound still works
            while self._running:
                await asyncio.sleep(self._poll_interval)
            return

        _log(f"imessage-poller[{self._agent_name}]: chat.db connected, polling")

        while self._running:
            try:
                await self._poll_once()
            except Exception as e:
                _log(f"imessage-poller[{self._agent_name}]: error: {e}")
                await asyncio.sleep(5)

            await asyncio.sleep(self._poll_interval)

    async def _poll_once(self) -> None:
        """Single poll iteration."""
        from pinky_outreach.imessage import iMessageError

        try:
            messages = await asyncio.get_event_loop().run_in_executor(
                None, lambda: self._adapter.get_updates(limit=20),
            )
        except iMessageError as e:
            _log(f"imessage-poller[{self._agent_name}]: {e}")
            return

        self._poll_count += 1

        for msg in messages:
            is_group = msg.metadata.get("is_group", False)

            broker_msg = self._BrokerMessage(
                platform="imessage",
                chat_id=msg.chat_id,
                sender_name=msg.sender,
                sender_id=msg.metadata.get("handle_id", msg.sender),
                content=msg.content,
                agent_name=self._agent_name,
                message_id=msg.message_id,
                chat_title=msg.metadata.get("display_name", ""),
                is_group=is_group,
                metadata=msg.metadata,
            )

            _log(
                f"imessage-poller[{self._agent_name}]: message from {msg.sender} "
                f"in {msg.chat_id}: {msg.content[:50]}..."
            )

            asyncio.create_task(self._broker.handle_inbound(broker_msg))

            if self._event_callback:
                try:
                    await self._event_callback(
                        platform="imessage",
                        chat_id=str(msg.chat_id),
                        sender=msg.sender,
                        content=msg.content,
                    )
                except Exception as e:
                    _log(f"imessage-poller[{self._agent_name}]: event callback error: {e}")

    def stop(self) -> None:
        self._running = False
        _log(f"imessage-poller[{self._agent_name}]: stopping")

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def poll_count(self) -> int:
        return self._poll_count

    @property
    def is_running(self) -> bool:
        return self._running


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
