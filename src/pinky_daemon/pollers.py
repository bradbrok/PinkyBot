"""Platform pollers — fetch inbound messages from each platform.

Each poller runs as an async task in the daemon's event loop,
periodically checking for new messages and feeding them to the
message handler (legacy) or message broker (new).
"""

from __future__ import annotations

import asyncio
import sys
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pinky_daemon.message_handler import InboundMessage, MessageHandler
from pinky_outreach.discord import DiscordAdapter, DiscordError, DiscordRateLimited
from pinky_outreach.telegram import TelegramAdapter, TelegramError

# Threshold below which a "most recent message" found during channel-priming
# is treated as a real first-test inbound rather than as a replay-prevention
# floor. Discord first-contact UX: if a user sends a test message within this
# window before discovery sweeps, we still want to deliver it.
_PRIME_FRESH_WINDOW_SECONDS = 30.0

if TYPE_CHECKING:
    from pinky_outreach.types import Chat


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
        messages = await asyncio.get_running_loop().run_in_executor(
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
        messages = await asyncio.get_running_loop().run_in_executor(
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
            messages = await asyncio.get_running_loop().run_in_executor(
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


class BrokerDiscordPoller:
    """Polls Discord REST API for a specific agent's bot token, routes through MessageBroker.

    This is the Discord analogue of BrokerTelegramPoller, but using REST polling
    rather than long-polling — the Discord REST API has no equivalent to
    Telegram's getUpdates. A future version may add a Gateway/WebSocket poller
    for true push delivery; this class provides a working inbound channel today.

    Behavior:
      - Discovers watchable channels at startup (settings.watched_channels if set,
        otherwise auto-discovered text channels across all the bot's guilds).
      - Polls each channel every `poll_interval` seconds, fetching only messages
        with id > last seen via the `?after=` parameter.
      - On first sight of a channel, primes `last_id` from the most recent
        message so we don't replay history.
      - Skips messages authored by the bot itself (no self-reply loop).
      - Honors 429 rate limits via DiscordRateLimited (sleeps retry_after).
      - Re-discovers channels every `discovery_interval` seconds so newly-joined
        guilds become reachable without a daemon restart.
    """

    def __init__(
        self,
        adapter: DiscordAdapter,
        agent_name: str,
        broker,  # MessageBroker
        registry=None,  # AgentRegistry — reserved for future guild tracking
        *,
        poll_interval: float = 1.0,
        discovery_interval: float = 60.0,
        watched_channels: list[str] | None = None,
        event_callback=None,
    ) -> None:
        from pinky_daemon.broker import BrokerMessage, MessageBroker
        self._BrokerMessage = BrokerMessage

        self._adapter = adapter
        self._agent_name = agent_name
        self._broker: MessageBroker = broker
        self._registry = registry
        self._poll_interval = max(0.25, float(poll_interval))
        self._discovery_interval = max(10.0, float(discovery_interval))
        self._configured_channels = list(watched_channels or [])
        self._event_callback = event_callback
        self._running = False
        self._poll_count = 0
        self._bot_user_id = ""
        self._bot_username = ""
        # Per-channel state
        self._channels: list[str] = []
        self._last_id: dict[str, str] = {}
        self._last_discovery: float = 0.0
        # Channel metadata cache — invalidated each discovery cycle (~60s by
        # default). Avoids fanning out get_channel() calls across every message
        # in a burst on the same channel.
        self._channel_info_cache: dict[str, Chat] = {}

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def poll_count(self) -> int:
        return self._poll_count

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def watched_channels(self) -> list[str]:
        return list(self._channels)

    async def start(self) -> None:
        """Start the polling loop."""
        self._running = True
        _log(f"discord-poller[{self._agent_name}]: starting")

        # Verify bot connection
        try:
            me = await asyncio.get_running_loop().run_in_executor(
                None, self._adapter.get_me,
            )
            self._bot_user_id = me.get("id", "")
            self._bot_username = me.get("username", "?")
            _log(
                f"discord-poller[{self._agent_name}]: connected as "
                f"{self._bot_username} (id={self._bot_user_id})"
            )
        except DiscordError as e:
            _log(f"discord-poller[{self._agent_name}]: failed to connect: {e}")
            self._running = False
            return

        # Initial channel discovery + last_id priming
        await self._refresh_channels(verbose=True)

        while self._running:
            try:
                # Periodic re-discovery (cheap — one /users/@me/guilds + one
                # /guilds/{id}/channels per guild per discovery_interval).
                import time as _time
                if _time.monotonic() - self._last_discovery >= self._discovery_interval:
                    await self._refresh_channels(verbose=False)

                await self._poll_once()
            except DiscordRateLimited as e:
                _log(
                    f"discord-poller[{self._agent_name}]: rate limited, "
                    f"sleeping {e.retry_after:.2f}s"
                )
                await asyncio.sleep(min(e.retry_after, 30.0))
            except DiscordError as e:
                _log(f"discord-poller[{self._agent_name}]: error: {e}")
                await asyncio.sleep(5)
            except Exception as e:
                _log(f"discord-poller[{self._agent_name}]: unexpected error: {e}")
                await asyncio.sleep(5)

            await asyncio.sleep(self._poll_interval)

    async def _refresh_channels(self, *, verbose: bool) -> None:
        """Refresh the watched-channel set and prime last_id for newcomers.

        `verbose=True` produces a log line on every call (used at startup);
        otherwise we only log on add/remove changes.
        """
        import time as _time
        loop = asyncio.get_running_loop()

        if self._configured_channels:
            new_set = list(self._configured_channels)
        else:
            try:
                new_set = await loop.run_in_executor(
                    None, self._adapter.discover_text_channels,
                )
            except DiscordError as e:
                _log(
                    f"discord-poller[{self._agent_name}]: "
                    f"channel discovery failed: {e}"
                )
                # Keep existing set on transient discovery failure.
                self._last_discovery = _time.monotonic()
                return

        added = [c for c in new_set if c not in self._last_id]
        removed = [c for c in self._channels if c not in new_set]

        # Prime last_id for newly-discovered channels: fetch the most recent
        # message and treat its id as the baseline so we don't replay history.
        # Exception: if that message is a fresh (< _PRIME_FRESH_WINDOW_SECONDS)
        # non-bot message, treat it as a real first-test inbound — back the
        # floor up by 1 snowflake so the next poll picks it up and delivers it.
        # This avoids the "user sends test, discovery silently swallows it"
        # first-contact UX trap. (Discord IDs are monotonic snowflakes;
        # `?after=<id-1>` resolves to "messages with id > id-1", i.e. this
        # message itself.)
        for ch in added:
            try:
                recent = await loop.run_in_executor(
                    None,
                    lambda c=ch: self._adapter.get_messages(c, limit=1),
                )
            except DiscordError:
                # Channel might be unreadable; skip it for now, retry next discovery
                continue
            if not recent:
                # Empty channel — start from "0", which sorts before any real snowflake
                self._last_id[ch] = "0"
                _log(
                    f"discord-poller[{self._agent_name}]: primed channel {ch} "
                    f"(empty — first message will be delivered)"
                )
                continue

            # get_messages returns newest-first
            msg = recent[0]
            now = datetime.now(timezone.utc)
            try:
                age_sec = (now - msg.timestamp).total_seconds()
            except (TypeError, ValueError):
                age_sec = float("inf")
            is_bot = bool((msg.metadata or {}).get("is_bot", False))

            if age_sec < _PRIME_FRESH_WINDOW_SECONDS and not is_bot:
                # Fresh first-contact message — back the floor up by 1 so
                # the next poll fetches and delivers it.
                try:
                    backed_off = str(int(msg.message_id) - 1)
                except (TypeError, ValueError):
                    # Non-numeric id (shouldn't happen on Discord) — fall back
                    # to using the message itself as the floor.
                    backed_off = msg.message_id
                self._last_id[ch] = backed_off
                _log(
                    f"discord-poller[{self._agent_name}]: primed channel {ch} "
                    f"with floor {backed_off} (fresh msg {msg.message_id} from "
                    f"{msg.sender}, age {age_sec:.0f}s — will deliver on next poll)"
                )
            else:
                self._last_id[ch] = msg.message_id
                _log(
                    f"discord-poller[{self._agent_name}]: primed channel {ch} "
                    f"with floor {msg.message_id} (most-recent msg age "
                    f"{age_sec:.0f}s, is_bot={is_bot} — first message AFTER "
                    f"this will be delivered)"
                )

        for ch in removed:
            self._last_id.pop(ch, None)

        self._channels = new_set
        self._last_discovery = _time.monotonic()

        # Drop cached channel metadata so renames / type changes get picked up
        # on the next inbound. Cheap (≤ N discovery_interval-old entries).
        self._channel_info_cache.clear()

        if added or removed or verbose:
            _log(
                f"discord-poller[{self._agent_name}]: watching "
                f"{len(self._channels)} channels (+{len(added)} -{len(removed)})"
            )

    async def _resolve_channel_info(self, channel_id: str):
        """Return cached channel metadata or fetch + cache it on miss."""
        cached = self._channel_info_cache.get(channel_id)
        if cached is not None:
            return cached
        try:
            chat_info = await asyncio.get_running_loop().run_in_executor(
                None, lambda c=channel_id: self._adapter.get_channel(c),
            )
        except DiscordError:
            return None
        self._channel_info_cache[channel_id] = chat_info
        return chat_info

    def _attach_broker_failure_logger(self, task: "asyncio.Task") -> None:
        """Surface unhandled broker delivery exceptions as poller log lines."""
        def _log_failure(t: "asyncio.Task") -> None:
            if t.cancelled():
                return
            exc = t.exception()
            if exc is None:
                return
            _log(
                f"discord-poller[{self._agent_name}]: "
                f"broker delivery failed: {exc!r}"
            )
        task.add_done_callback(_log_failure)

    async def _poll_once(self) -> None:
        """Single sweep across all watched channels."""
        loop = asyncio.get_running_loop()
        self._poll_count += 1

        for channel_id in list(self._channels):
            after = self._last_id.get(channel_id, "")
            if after == "0":
                # Empty-channel sentinel — drop the after filter so the first real
                # message is picked up regardless of snowflake ordering.
                after = ""

            try:
                messages = await loop.run_in_executor(
                    None,
                    lambda c=channel_id, a=after: self._adapter.get_messages(
                        c, limit=50, after=a,
                    ),
                )
            except DiscordRateLimited:
                # Bubble up so the outer loop can sleep retry_after
                raise
            except DiscordError as e:
                _log(
                    f"discord-poller[{self._agent_name}]: "
                    f"channel {channel_id} fetch failed: {e}"
                )
                continue

            if not messages:
                continue

            # Discord returns newest-first; replay in chronological order so
            # the broker sees them as they happened.
            messages = list(reversed(messages))

            # Channel metadata is constant per channel per poll cycle (and
            # essentially constant across the whole discovery_interval —
            # renames are rare). Resolve once per channel per sweep instead
            # of fanning out N get_channel calls in the per-message loop.
            chat_info = await self._resolve_channel_info(channel_id)
            chat_title = chat_info.title if chat_info and chat_info.title else ""
            is_group = bool(chat_info and chat_info.chat_type != "dm")

            for msg in messages:
                # Track high-water mark even for skipped messages so we don't
                # re-fetch them next tick.
                self._last_id[channel_id] = msg.message_id

                meta = msg.metadata or {}
                if meta.get("is_bot"):
                    continue
                if meta.get("author_id") and meta["author_id"] == self._bot_user_id:
                    continue

                broker_msg = self._BrokerMessage(
                    platform="discord",
                    chat_id=channel_id,
                    sender_name=msg.sender,
                    sender_id=meta.get("author_id", ""),
                    content=msg.content,
                    agent_name=self._agent_name,
                    message_id=msg.message_id,
                    chat_title=chat_title,
                    is_group=is_group,
                    reply_to=msg.reply_to,
                    metadata=meta,
                    attachments=meta.get("attachments", []),
                )

                _log(
                    f"discord-poller[{self._agent_name}]: message from "
                    f"{msg.sender} in {channel_id}: {msg.content[:50]}..."
                )

                delivery = asyncio.create_task(
                    self._broker.handle_inbound(broker_msg)
                )
                self._attach_broker_failure_logger(delivery)

                if self._event_callback:
                    try:
                        await self._event_callback(
                            platform="discord",
                            chat_id=str(channel_id),
                            sender=msg.sender,
                            content=msg.content,
                        )
                    except Exception as e:
                        _log(
                            f"discord-poller[{self._agent_name}]: "
                            f"event callback error: {e}"
                        )

    def stop(self) -> None:
        """Stop the polling loop."""
        self._running = False
        _log(f"discord-poller[{self._agent_name}]: stopping")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
