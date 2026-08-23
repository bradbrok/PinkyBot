"""Platform pollers — fetch inbound messages from each platform.

Each poller runs as an async task in the daemon's event loop,
periodically checking for new messages and feeding them to the
message handler (legacy) or message broker (new).
"""

from __future__ import annotations

import asyncio
import re
import sys
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from typing import TYPE_CHECKING

from pinky_daemon import purchase_approval
from pinky_daemon.message_handler import InboundMessage, MessageHandler
from pinky_outreach.discord import DiscordAdapter, DiscordError, DiscordRateLimited
from pinky_outreach.telegram import TelegramAdapter, TelegramError

# Cross-repo contract with pos-spec-purchasing's blocks.py — the action_ids on
# the Approve/Reject buttons of a purchase-proposal card. The Slack interactive
# handler keys on these to route a click to approve_purchase/reject_purchase.
# Changing either requires a coordinated change in pos-spec-purchasing.
_PURCHASE_APPROVE_ACTION_ID = "pos_purchase_approve"
_PURCHASE_REJECT_ACTION_ID = "pos_purchase_reject"

# A purchase pending_id / Slack user id must be a clean opaque token. Validating
# at the boundary (fail-closed) keeps quotes/whitespace/newlines out of the
# instruction we hand the agent — defense-in-depth against prompt-injection,
# even though the values arrive on a Slack-signed envelope.
_APPROVAL_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")


def _strip_emoji(text: str) -> str:
    """Remove emoji/pictographs from shipped-UI text (the NO-EMOJI rule).

    Drops Unicode "symbol, other" (So) code points plus ZWJ / variation
    selectors — that covers emoji and flags while preserving letters and marks,
    so non-Latin names (e.g. Cyrillic) survive intact.
    """
    out = [
        ch
        for ch in text
        if ch not in ("\u200d", "\ufe0f", "\ufe0e")
        and unicodedata.category(ch) != "So"
    ]
    return "".join(out).strip()

# Threshold below which a "most recent message" found during channel-priming
# is treated as a real first-test inbound rather than as a replay-prevention
# floor. Discord first-contact UX: if a user sends a test message within this
# window before discovery sweeps, we still want to deliver it.
_PRIME_FRESH_WINDOW_SECONDS = 30.0

# Strong references to in-flight delivery tasks: the event loop keeps only
# weak references, so a bare fire-and-forget create_task can be garbage
# collected mid-flight and its exception silently dropped.
_DELIVERY_TASKS: set["asyncio.Task"] = set()


def _deliver_in_background(coro, log_prefix: str) -> "asyncio.Task":
    """Schedule an inbound delivery coroutine, surfacing failures in the log."""
    task = asyncio.create_task(coro)
    _DELIVERY_TASKS.add(task)

    def _on_done(t: "asyncio.Task") -> None:
        _DELIVERY_TASKS.discard(t)
        if t.cancelled():
            return
        exc = t.exception()
        if exc is not None:
            _log(f"{log_prefix}: broker delivery failed: {exc!r}")

    task.add_done_callback(_on_done)
    return task

if TYPE_CHECKING:
    from pinky_outreach.slack import SlackAdapter
    from pinky_outreach.types import Chat


# Seconds past poll_timeout before a poll is declared stuck. Telegram's server
# always answers a getUpdates long poll within poll_timeout, so anything this
# far past it means the connection is dead and the read will never return.
_POLL_WATCHDOG_GRACE = 30.0


class _TelegramPollWatchdog:
    """Deadline + recovery for blocking getUpdates calls (#1145).

    The 2026-08-23 incident: a network blip left every Telegram poller's
    long-poll blocked in ssl.read indefinitely — the httpx client-level read
    timeout demonstrably did not fire — and because polls ran on the event
    loop's DEFAULT executor, the stuck threads consumed most of that shared
    pool and starved unrelated daemon work. Three structural changes:

      * every poller owns a DEDICATED single-thread executor, so a stuck
        poll can never starve anything beyond its own poller;
      * each poll runs under an outer asyncio deadline that does not depend
        on the HTTP stack honoring its own timeouts;
      * on deadline the adapter's HTTP client is recycled (closing its pooled
        sockets frees the stuck thread) and the executor is replaced, so
        polling continues even if the old thread never exits.

    Host classes must define ``_poll_timeout``, ``_adapter`` (with
    ``get_updates``/``recycle``) and ``_process_messages`` before calling
    ``_init_watchdog``.

    Tradeoffs, deliberately accepted: a thread the recycle cannot free (e.g.
    wedged in DNS resolution before any pooled socket exists) accumulates
    one non-daemon thread per fire — observable via ``watchdog_fires`` and
    the loud log — and any still-stuck thread is joined at interpreter exit,
    so a truly immortal one can hang daemon shutdown until the supervisor
    kills the process. Both beat the alternative this replaces (silent
    fleet-wide inbound death).
    """

    def _init_watchdog(self, label: str, grace: float) -> None:
        self._watchdog_label = label
        self._watchdog_grace = grace
        self._poll_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=label
        )
        self._watchdog_fires = 0
        self._last_poll_ok = 0.0

    async def _watched_poll(self, poll_fn):
        """Run ``poll_fn`` on the dedicated executor under a hard deadline.

        Returns the poll result, or ``[]`` after firing the watchdog — the
        recovery is loud (log + counter) so the empty result never masks it.

        A poll that completes AT or AFTER the deadline is never discarded:
        its ``get_updates`` already advanced the offset, so the next poll
        would confirm-and-drop those updates server-side — dropping the
        result here would be silent, permanent message loss. Completed-at-
        the-edge results are returned directly; genuinely late completions
        are delivered through ``_process_messages`` when they arrive.
        """
        loop = asyncio.get_running_loop()
        fut = loop.run_in_executor(self._poll_executor, poll_fn)
        deadline = self._poll_timeout + self._watchdog_grace
        try:
            result = await asyncio.wait_for(asyncio.shield(fut), timeout=deadline)
        except TimeoutError:
            if fut.done() and not fut.cancelled() and fut.exception() is None:
                # Completed on the deadline edge (loop scheduled the timeout
                # callback first) — a healthy poll, not a stuck one.
                self._last_poll_ok = time.monotonic()
                return fut.result()
            fut.add_done_callback(self._on_abandoned_poll_done)
            self._recycle_after_stuck_poll(deadline)
            return []
        self._last_poll_ok = time.monotonic()
        return result

    def _on_abandoned_poll_done(self, fut) -> None:
        """An abandoned poll eventually finished (usually because the recycle
        closed its socket). Consume the error quietly; deliver a late RESULT
        — its updates are already offset-confirmed and exist nowhere else."""
        if fut.cancelled():
            return
        if fut.exception() is not None:  # consumed: no "never retrieved" noise
            return
        late = fut.result()
        if not late:
            return
        _log(
            f"{self._watchdog_label}: abandoned poll completed late with "
            f"{len(late)} update(s) — delivering (offset already advanced)"
        )
        _deliver_in_background(
            self._process_messages(late),
            f"{self._watchdog_label} late-delivery",
        )

    def _recycle_after_stuck_poll(self, deadline: float) -> None:
        self._watchdog_fires += 1
        age = time.monotonic() - self._last_poll_ok if self._last_poll_ok else -1.0
        _log(
            f"{self._watchdog_label}: WATCHDOG poll exceeded {deadline:.0f}s hard "
            f"deadline (fire #{self._watchdog_fires}, last successful poll "
            f"{age:.0f}s ago) — recycling HTTP client + poll thread"
        )
        try:
            self._adapter.recycle()
        except Exception as e:
            _log(f"{self._watchdog_label}: adapter recycle failed: {e}")
        old = self._poll_executor
        self._poll_executor = ThreadPoolExecutor(
            max_workers=1, thread_name_prefix=self._watchdog_label
        )
        old.shutdown(wait=False)

    def _shutdown_watchdog(self) -> None:
        self._poll_executor.shutdown(wait=False)

    @property
    def watchdog_fires(self) -> int:
        return self._watchdog_fires

    @property
    def last_poll_ok(self) -> float:
        """monotonic timestamp of the last completed poll (0.0 = never)."""
        return self._last_poll_ok


class TelegramPoller(_TelegramPollWatchdog):
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
        watchdog_grace: float = _POLL_WATCHDOG_GRACE,
    ) -> None:
        self._adapter = adapter
        self._handler = handler
        self._poll_timeout = poll_timeout
        self._poll_interval = poll_interval
        self._allowed_chats = set(allowed_chat_ids) if allowed_chat_ids else None
        self._event_callback = event_callback  # async fn(platform, chat_id, sender, content)
        self._running = False
        self._poll_count = 0
        self._init_watchdog("telegram-poller", watchdog_grace)

    async def start(self) -> None:
        """Start the polling loop."""
        self._running = True
        _log("telegram-poller: starting")

        # Verify bot connection — on the poller's executor, under a deadline,
        # so a wedged network can't block the event loop or hang startup.
        loop = asyncio.get_running_loop()
        try:
            me = await asyncio.wait_for(
                loop.run_in_executor(self._poll_executor, self._adapter.get_me),
                timeout=30,
            )
            _log(f"telegram-poller: connected as @{me.get('username', '?')}")
        except (TelegramError, TimeoutError) as e:
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
        # Blocking HTTP call on the poller's OWN executor, under a hard
        # deadline (#1145) — never the loop default pool.
        messages = await self._watched_poll(
            lambda: self._adapter.get_updates(timeout=self._poll_timeout),
        )

        self._poll_count += 1

        await self._process_messages(messages)

    async def _process_messages(self, messages) -> None:
        """Route polled updates to the handler — also the watchdog's
        late-delivery path for polls that complete after abandonment."""
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
            _deliver_in_background(self._handler.handle(inbound), "telegram-poller")

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
        self._shutdown_watchdog()
        _log("telegram-poller: stopping")

    @property
    def poll_count(self) -> int:
        return self._poll_count

    @property
    def is_running(self) -> bool:
        return self._running


class BrokerTelegramPoller(_TelegramPollWatchdog):
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
        watchdog_grace: float = _POLL_WATCHDOG_GRACE,
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
        self._init_watchdog(f"broker-poller[{agent_name}]", watchdog_grace)

    async def start(self) -> None:
        """Start the polling loop."""
        self._running = True
        _log(f"broker-poller[{self._agent_name}]: starting")

        # On the poller's executor, under a deadline — a wedged network can't
        # block the event loop or hang startup.
        loop = asyncio.get_running_loop()
        try:
            me = await asyncio.wait_for(
                loop.run_in_executor(self._poll_executor, self._adapter.get_me),
                timeout=30,
            )
            self._bot_username = me.get("username", "?")
            _log(f"broker-poller[{self._agent_name}]: connected as @{self._bot_username}")
        except (TelegramError, TimeoutError) as e:
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
        # Blocking HTTP call on the poller's OWN executor, under a hard
        # deadline (#1145) — never the loop default pool.
        messages = await self._watched_poll(
            lambda: self._adapter.get_updates(timeout=self._poll_timeout),
        )

        self._poll_count += 1

        await self._process_messages(messages)

    async def _process_messages(self, messages) -> None:
        """Route polled updates through the broker — also the watchdog's
        late-delivery path for polls that complete after abandonment."""
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
            _deliver_in_background(
                self._broker.handle_inbound(broker_msg),
                f"broker-poller[{self._agent_name}]",
            )

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
        self._shutdown_watchdog()
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

            _deliver_in_background(
                self._broker.handle_inbound(broker_msg),
                f"imessage-poller[{self._agent_name}]",
            )

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
            # Discriminate self vs peer: only OUR bot's messages are skipped at
            # priming. Peer agents/bots are first-class senders and their fresh
            # messages should be delivered on next poll (cross-fleet support).
            author_id = (msg.metadata or {}).get("author_id", "")
            is_self = bool(self._bot_user_id) and author_id == self._bot_user_id

            if age_sec < _PRIME_FRESH_WINDOW_SECONDS and not is_self:
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
                    f"{age_sec:.0f}s, is_self={is_self} — first message AFTER "
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

    async def _poll_once(self) -> None:
        """Single sweep across all watched channels."""
        loop = asyncio.get_running_loop()
        self._poll_count += 1

        for channel_id in list(self._channels):
            after = self._last_id.get(channel_id)
            if after is None:
                # Priming failed during discovery; polling with no floor would
                # replay up to 50 historical messages. Skip until the next
                # discovery re-primes it (`added` is computed from _last_id).
                continue
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
                # Skip our own messages to avoid self-reply loops. We do NOT
                # filter on is_bot — peer agents and other bots must reach us
                # for cross-fleet messaging (Pulse, Misha, etc.). Each agent
                # is responsible for its own conversational rate-limiting.
                author_id = meta.get("author_id", "")
                if author_id and author_id == self._bot_user_id:
                    continue
                # Defensive: a bot-authored message with no author_id can't be
                # deduped against ourselves, so skip it. The Discord adapter
                # always sets author_id, so this is a belt-and-suspenders guard.
                if meta.get("is_bot") and not author_id:
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

                _deliver_in_background(
                    self._broker.handle_inbound(broker_msg),
                    f"discord-poller[{self._agent_name}]",
                )

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


# Slack mrkdwn ref tokens: <@U…>, <#C…|name>, <!here>, <https://…|label>, …
# (Slack never nests these and escapes literal </>/& as entities, so a simple
# non-greedy, no-angle-bracket body match is safe.)
_SLACK_REF_RE = re.compile(r"<([^<>]+)>")


async def _resolve_slack_ref(body: str, user_namer, channel_namer) -> str:
    """Resolve a single Slack ref token body (the text between < and >)."""
    raw = f"<{body}>"
    if body.startswith("@"):  # user mention: <@U123> or <@U123|label>
        uid, _, label = body[1:].partition("|")
        if label:
            return "@" + label
        name = await user_namer(uid)
        return "@" + (name or uid)
    if body.startswith("#"):  # channel mention: <#C123|name> or <#C123>
        cid, _, label = body[1:].partition("|")
        if label:
            return "#" + label
        name = await channel_namer(cid)
        return "#" + (name or cid)
    if body.startswith("!"):  # special: <!here>, <!subteam^S1|@grp>, <!date^…|fb>
        cmd, _, label = body[1:].partition("|")
        if cmd in ("here", "channel", "everyone"):
            return "@" + cmd
        if cmd.startswith("subteam^"):
            return "@" + (label.lstrip("@") if label else "group")
        if cmd.startswith("date^"):
            return label or raw
        return label or raw
    # link: <https://x|label> or <https://x> or <mailto:a@b|a@b>
    url, _, label = body.partition("|")
    return label or url


async def resolve_slack_text_refs(text: str, *, user_namer, channel_namer) -> str:
    """Rewrite Slack mrkdwn ref tokens in inbound text to human-readable form.

    `<@U0EXAMPLE1>` → `@Alex Rivera`, `<#C1|orders>` → `#orders`, `<!here>` →
    `@here`, `<https://x|click>` → `click`. ``user_namer``/``channel_namer`` are
    async callables (id → name) returning "" on failure, in which case the raw
    id is kept (no regression). After ref substitution, Slack HTML entities
    (&lt; &gt; &amp;) are unescaped so the agent reads natural text.
    """
    if text and "<" in text:
        parts: list[str] = []
        last = 0
        for m in _SLACK_REF_RE.finditer(text):
            parts.append(text[last : m.start()])
            parts.append(await _resolve_slack_ref(m.group(1), user_namer, channel_namer))
            last = m.end()
        parts.append(text[last:])
        text = "".join(parts)
    if text:
        text = text.replace("&lt;", "<").replace("&gt;", ">").replace("&amp;", "&")
    return text


class BrokerSlackPoller:
    """Slack Socket Mode listener for one agent's bot; routes to MessageBroker.

    Unlike the Telegram/Discord pollers (long-poll / REST poll), Slack uses
    Socket Mode: a persistent OUTBOUND WebSocket over which Slack pushes events,
    so no public ingress is needed. First true push listener in the fleet.
    Requires a bot token (xoxb-, identity + Web API) AND an app-level token
    (xapp-, the Socket Mode connection). slack_sdk's SocketModeClient handles
    reconnect, ping/pong, and the 3s envelope ACK.

    Behaviour:
      - ACKs every Socket Mode envelope (events_api / slash_commands /
        interactive) immediately, then processes async; non-message envelopes
        are acked and dropped (only message events are wired up for #224).
      - Delivers `message` events to broker.handle_inbound(): plain user text
        AND bot-authored text (the `bot_message` subtype), so peer agents/bots
        reach us (parity with the Discord poller). Our own messages are
        self-filtered by user_id and bot_id (no self-reply loop); all other
        subtypes (edits/joins/topic/…) are skipped. Fail-closed: a bot message
        we can't distinguish from our own (bot_id present, our bot_id
        unresolved) is dropped.
      - Channel-agnostic: receives events for every channel/IM the app is
        subscribed to (no per-channel discovery/polling).
    """

    def __init__(
        self,
        adapter: "SlackAdapter",
        agent_name: str,
        broker,  # MessageBroker
        registry=None,  # AgentRegistry — reserved
        *,
        app_token: str,
        event_callback=None,
    ) -> None:
        from pinky_daemon.broker import BrokerMessage, MessageBroker
        self._BrokerMessage = BrokerMessage

        self._adapter = adapter
        self._agent_name = agent_name
        self._broker: MessageBroker = broker
        self._registry = registry
        self._app_token = app_token
        self._event_callback = event_callback
        self._running = False
        self._bot_user_id = ""
        self._bot_id = ""  # our own bot_id (from auth.test) — filters our bot_message echoes
        self._client = None  # slack_sdk SocketModeClient, created in start()
        # Cosmetic identity resolution (users:read / channels:read). sender_id and
        # approval routing always key on the raw id; only the display name and
        # chat_title are enriched. Cached for the poller's lifetime (renames are
        # rare); a missing users:read scope disables user resolution after the
        # first failure so we don't hammer the API.
        self._user_name_cache: dict[str, str] = {}
        self._channel_title_cache: dict[str, str] = {}
        self._users_read_ok = True

    @property
    def agent_name(self) -> str:
        return self._agent_name

    @property
    def is_running(self) -> bool:
        return self._running

    async def start(self) -> None:
        """Connect to Slack Socket Mode and stream events until stopped.

        slack_sdk's ``connect()`` establishes the websocket and spawns its own
        background tasks, then returns — the connection stays alive as long as
        this poller (and thus ``self._client``) is referenced by the daemon.
        """
        if self._running:
            _log(f"slack-poller[{self._agent_name}]: already running, ignoring start()")
            return
        if not self._app_token:
            _log(f"slack-poller[{self._agent_name}]: no app_token set, not starting")
            return
        try:
            from slack_sdk.socket_mode.aiohttp import SocketModeClient
            from slack_sdk.socket_mode.response import SocketModeResponse
            from slack_sdk.web.async_client import AsyncWebClient
        except ImportError as e:
            _log(
                f"slack-poller[{self._agent_name}]: slack_sdk unavailable ({e}); "
                f"install the 'slack' extra"
            )
            return

        loop = asyncio.get_running_loop()
        # Verify bot identity (auth.test via the sync httpx adapter) for self-filter.
        try:
            info = await loop.run_in_executor(None, self._adapter.get_bot_info)
        except Exception as e:
            _log(f"slack-poller[{self._agent_name}]: auth.test failed: {e}")
            return
        self._bot_user_id = info.get("user_id", "") or ""
        if not self._bot_user_id:
            # Fail closed: without our own user id the self-filter is disarmed
            # (we couldn't distinguish our own messages → self-reply loop), so
            # refuse to start rather than run with the guard defeated.
            _log(
                f"slack-poller[{self._agent_name}]: auth.test returned no user_id; "
                f"refusing to start (self-filter would be disarmed)"
            )
            return
        self._bot_id = info.get("bot_id", "") or ""
        _log(
            f"slack-poller[{self._agent_name}]: connected as {info.get('user', '?')} "
            f"(id={self._bot_user_id}, bot_id={self._bot_id or '?'}, team={info.get('team', '?')})"
        )

        client = SocketModeClient(
            app_token=self._app_token,
            web_client=AsyncWebClient(token=self._adapter.bot_token),
        )
        self._client = client

        async def _on_request(smc, req) -> None:
            # ACK FIRST — every Socket Mode request envelope (events_api,
            # slash_commands, interactive, …) carries an envelope_id and must be
            # acked within 3s or Slack redelivers it. (Control frames like
            # hello/disconnect have no envelope_id and don't reach this listener.)
            envelope_id = getattr(req, "envelope_id", None)
            if envelope_id:
                try:
                    await smc.send_socket_mode_response(
                        SocketModeResponse(envelope_id=envelope_id)
                    )
                except Exception as e:
                    _log(f"slack-poller[{self._agent_name}]: ack failed: {e}")
            # Interactive envelopes (block_actions button clicks) carry the
            # purchase Approve/Reject buttons (#249). Handle them here; every
            # other non-events_api envelope is dropped (already acked) so Slack
            # doesn't keep retrying.
            if req.type == "interactive":
                try:
                    await self._handle_interactive(req.payload or {})
                except Exception as e:
                    _log(f"slack-poller[{self._agent_name}]: handle_interactive error: {e!r}")
                return
            # Only message events are wired up for #224; other (already-acked)
            # envelope types are dropped so Slack doesn't keep retrying them.
            if req.type != "events_api":
                if req.type:
                    _log(
                        f"slack-poller[{self._agent_name}]: dropping non-events_api "
                        f"envelope ({req.type})"
                    )
                return
            try:
                await self._handle_event(req.payload or {})
            except Exception as e:
                _log(f"slack-poller[{self._agent_name}]: handle_event error: {e!r}")

        client.socket_mode_request_listeners.append(_on_request)

        self._running = True
        try:
            await client.connect()
            _log(f"slack-poller[{self._agent_name}]: socket mode connected, listening")
        except Exception as e:
            _log(f"slack-poller[{self._agent_name}]: connect failed: {e}")
            self._running = False

    async def _handle_interactive(self, payload: dict) -> None:
        """Handle a Slack interactive (block_actions) envelope — purchase buttons (#249).

        SECURITY (financial boundary): the approver identity is taken from
        ``payload['user']['id']`` — set and signed by Slack, NOT anything an LLM
        provided. Only VERIFIED ids in the daemon-side purchase-approver
        allowlist may approve/reject a spend; unauthorized clicks are refused
        here and never reach the agent. The agent is told to act ONLY after this
        gate passes, so the LLM is not the security boundary.
        """
        if (payload or {}).get("type") != "block_actions":
            return
        actions = payload.get("actions") or []
        if not actions:
            return
        action = actions[0] or {}
        action_id = action.get("action_id", "") or ""
        if action_id == _PURCHASE_APPROVE_ACTION_ID:
            decision = "approve"
        elif action_id == _PURCHASE_REJECT_ACTION_ID:
            decision = "reject"
        else:
            # Not a purchase button — nothing else is wired up. Already acked.
            return

        pending_id = (action.get("value") or "").strip()
        user = payload.get("user") or {}
        clicker_id = (user.get("id") or "").strip()
        # Cosmetic only; sanitized so an emoji in a Slack display name can't leak
        # into the shipped decided-card (NO-EMOJI rule). The id, not the name, is
        # what the gate and the MCP key on.
        clicker_name = _strip_emoji(user.get("username") or user.get("name") or "") or clicker_id
        channel = ((payload.get("channel") or {}).get("id")) or ""
        response_url = payload.get("response_url") or ""

        # Fail-closed boundary validation: pending_id and clicker_id must be clean
        # opaque tokens. This both drops malformed clicks and guarantees the
        # values are injection-safe before they go into the agent instruction.
        if not _APPROVAL_TOKEN_RE.match(pending_id) or not _APPROVAL_TOKEN_RE.match(clicker_id):
            _log(
                f"slack-poller[{self._agent_name}]: interactive {decision} malformed "
                f"pending_id/clicker — dropping"
            )
            return

        # ── GATE: the verified clicker must be an allowlisted purchase approver ──
        try:
            authorized = bool(
                self._registry and self._registry.is_purchase_approver(clicker_id)
            )
        except Exception as e:
            _log(f"slack-poller[{self._agent_name}]: approver check failed ({e}); denying")
            authorized = False  # fail-closed

        if not authorized:
            _log(
                f"slack-poller[{self._agent_name}]: UNAUTHORIZED purchase {decision} "
                f"by {clicker_id} on {pending_id} — refused"
            )
            await self._respond_url(
                response_url,
                {
                    "response_type": "ephemeral",
                    "replace_original": False,
                    "text": (
                        f"You are not authorized to {decision} purchases. Ask Brad to add "
                        "your Slack user id to the purchase approver allowlist."
                    ),
                },
            )
            return

        # Authorized — mint a daemon-signed token so the purchasing MCP can prove
        # this call came from a verified Slack click (the agent cannot forge it).
        # Fail-closed if the shared secret isn't configured.
        try:
            secret = self._registry.get_purchase_approval_secret() if self._registry else ""
        except Exception as e:
            _log(f"slack-poller[{self._agent_name}]: reading approval secret failed ({e})")
            secret = ""
        if not secret:
            _log(
                f"slack-poller[{self._agent_name}]: purchase_approval_secret not configured "
                f"— refusing {decision} on {pending_id} (fail-closed)"
            )
            await self._respond_url(
                response_url,
                {
                    "response_type": "ephemeral",
                    "replace_original": False,
                    "text": (
                        "Purchase approvals aren't configured yet (missing approval secret). "
                        "Ask Brad to finish the deploy step."
                    ),
                },
            )
            return

        token_decision = (
            purchase_approval.APPROVE if decision == "approve" else purchase_approval.REJECT
        )
        token_expires = int(time.time()) + purchase_approval.TOKEN_TTL_SECONDS
        token = purchase_approval.make_approval_token(
            secret,
            decision=token_decision,
            pending_id=pending_id,
            requester=self._agent_name,
            approver=clicker_id,
            expires=token_expires,
        )

        # Route to the owning agent to execute the state change via the purchasing
        # MCP, passing the VERIFIED approver id + daemon token straight through.
        if decision == "approve":
            instruction = (
                f"Call approve_purchase(pending_id='{pending_id}', approver='{clicker_id}', "
                f"approval_token='{token}', token_expires={token_expires}). "
                f"If it returns ok, post the returned purchase_links and instruction to "
                f"channel {channel} so the human can verify live prices and complete the "
                f"vendor checkout or quote. Approve ONLY this pending_id; do not approve "
                "anything else."
            )
        else:
            instruction = (
                f"Call reject_purchase(pending_id='{pending_id}', approver='{clicker_id}', "
                f"approval_token='{token}', token_expires={token_expires}). "
                f"Then post a one-line confirmation to channel {channel}. Reject ONLY this "
                "pending_id."
            )
        msg = (
            f"[purchase-approval] VERIFIED Slack approver {clicker_name} (id={clicker_id}) "
            f"clicked {decision.upper()} on purchase pending_id=`{pending_id}` in channel "
            f"{channel}. Requester identity is infrastructure-bound to `{self._agent_name}` "
            "and included in the signed token. The daemon has already verified the clicker's "
            f"identity and confirmed they are an allowlisted purchase approver, so this "
            f"{decision} is authorized.\n"
            + instruction
        )

        try:
            # InjectResult: attempt-level ``delivered`` is the right signal
            # here (the Slack card feedback means "routed to agent");
            # ``confirmed`` is transport-consumption observability and is not
            # required for this routing acknowledgement.
            delivered, _confirmed = await self._broker.inject_agent_message(
                "slack-approval", self._agent_name, msg
            )
        except Exception as e:
            _log(f"slack-poller[{self._agent_name}]: inject approval to agent failed: {e}")
            delivered = False

        if delivered:
            verb = "Approved" if decision == "approve" else "Rejected"
            _log(
                f"slack-poller[{self._agent_name}]: purchase {pending_id} "
                f"{verb.lower()} by {clicker_id} — routed to agent"
            )
            await self._respond_url(
                response_url,
                {
                    "replace_original": True,
                    "text": f"{verb} by {clicker_name}",
                    "blocks": self._decided_blocks(verb, clicker_name, pending_id),
                },
            )
        else:
            await self._respond_url(
                response_url,
                {
                    "response_type": "ephemeral",
                    "replace_original": False,
                    "text": (
                        "Couldn't reach the assistant to record this decision. The buttons "
                        "are still active — please try again shortly."
                    ),
                },
            )

    @staticmethod
    def _decided_blocks(verb: str, approver_name: str, pending_id: str) -> list[dict]:
        """Replacement card shown after a decision — emoji-free, buttons removed."""
        return [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": f"*Purchase `{pending_id}`* — {verb} by {approver_name}",
                },
            }
        ]

    async def _respond_url(self, response_url: str, payload: dict) -> None:
        """POST to a Slack interaction ``response_url`` off the event loop."""
        if not response_url:
            return
        loop = asyncio.get_running_loop()
        try:
            await loop.run_in_executor(
                None, lambda: self._adapter.respond_via_url(response_url, payload)
            )
        except Exception as e:
            _log(f"slack-poller[{self._agent_name}]: response_url post failed: {e}")

    async def _resolve_user_name(self, user_id: str) -> str:
        """Best-effort display name for a human Slack user id (users:read).

        Cached; returns "" on any failure so the caller keeps the raw id.
        A missing_scope failure disables further lookups (no API hammering).
        Defensive about the return shape so a mocked/odd response degrades to "".
        """
        if not user_id or not self._users_read_ok:
            return ""
        cached = self._user_name_cache.get(user_id)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        try:
            info = await loop.run_in_executor(
                None, self._adapter.get_user_info, user_id
            )
        except Exception as e:
            if "missing_scope" in str(e):
                self._users_read_ok = False
                _log(
                    f"slack-poller[{self._agent_name}]: users:read scope missing; "
                    f"disabling user-name resolution"
                )
            else:
                _log(
                    f"slack-poller[{self._agent_name}]: users.info({user_id}) "
                    f"failed: {e}"
                )
            return ""  # transient: not cached, retried on the next message
        name = info.get("display_name", "") if isinstance(info, dict) else ""
        if not isinstance(name, str):
            name = ""
        self._user_name_cache[user_id] = name
        return name

    async def _resolve_channel_title(self, channel_id: str) -> str:
        """Best-effort channel name (channels:read via conversations.info).

        Cached per channel; returns "" on failure. A scope failure for a given
        channel (e.g. a DM with no im:read) is cached as "" for that channel
        only — it won't change — so other channels still resolve. Transient
        failures are not cached, so they retry.
        """
        if not channel_id:
            return ""
        cached = self._channel_title_cache.get(channel_id)
        if cached is not None:
            return cached
        loop = asyncio.get_running_loop()
        try:
            chat = await loop.run_in_executor(
                None, self._adapter.get_channel_info, channel_id
            )
        except Exception as e:
            if "missing_scope" in str(e):
                self._channel_title_cache[channel_id] = ""  # permanent for this channel
            _log(
                f"slack-poller[{self._agent_name}]: conversations.info({channel_id}) "
                f"failed: {e}"
            )
            return ""
        title = getattr(chat, "title", "") if chat is not None else ""
        if not isinstance(title, str):
            title = ""
        self._channel_title_cache[channel_id] = title
        return title

    async def _resolve_text_refs(self, text: str) -> str:
        """Resolve in-text Slack mentions/links (<@U…>, <#C…>, <!here>, links)
        to readable form, reusing the cached user/channel resolvers."""
        return await resolve_slack_text_refs(
            text,
            user_namer=self._resolve_user_name,
            channel_namer=self._resolve_channel_title,
        )

    async def _handle_event(self, payload: dict) -> None:
        event = (payload or {}).get("event") or {}
        if event.get("type") != "message":
            return
        # Keep fresh inbound only: a normal message (incl. a thread reply) has no
        # subtype; bot-authored messages carry `bot_message`; and a file upload
        # (a shared screenshot/document) carries `file_share` and brings the
        # `files` we download below. All three are real inbound and must reach us
        # (peer-agent parity with the Discord poller). Every other subtype
        # (edits/deletes/joins/topic/…) is non-fresh → drop. NOTE: without
        # `file_share` here, image/file messages are dropped *before* the
        # attachment handling below ever runs — i.e. screenshots never arrive.
        subtype = event.get("subtype") or ""
        if subtype and subtype not in ("bot_message", "file_share"):
            return
        user_id = event.get("user", "") or ""
        bot_id = event.get("bot_id", "") or ""
        # Self-filter: never act on our own bot's messages (no self-reply loop).
        # Our own text surfaces as our user_id (rare) or — for bot-posted text —
        # our bot_id; drop on either match.
        if user_id and self._bot_user_id and user_id == self._bot_user_id:
            return
        if bot_id and self._bot_id and bot_id == self._bot_id:
            return
        # Fail closed: a bot-authored message we can't distinguish from our own
        # (it has a bot_id but we never resolved our own bot_id) could be our own
        # echo → drop rather than risk a self-reply loop.
        if bot_id and not self._bot_id:
            return

        channel = event.get("channel", "") or ""
        text = event.get("text", "") or ""
        ts = event.get("ts", "") or ""
        thread_ts = event.get("thread_ts", "") or ""
        channel_type = event.get("channel_type", "")  # im | channel | group | mpim
        is_group = channel_type != "im"
        # Identity: humans carry `user`; bot-authored messages carry `bot_id`
        # (and often `username`). sender_id is what approval keys on and ALWAYS
        # stays the raw id; sender_name / chat_title are cosmetic and enriched
        # best-effort via users:read / channels:read (cached, fall back to id).
        sender_id = user_id or bot_id
        sender_name = user_id or event.get("username", "") or bot_id or "unknown"
        if user_id:
            resolved = await self._resolve_user_name(user_id)
            if resolved:
                sender_name = resolved
        chat_title = await self._resolve_channel_title(channel)
        text = await self._resolve_text_refs(text)

        attachments = [
            {
                "type": "file",
                "file_id": f.get("id", ""),
                "file_name": f.get("name", ""),
                "url": f.get("url_private_download", ""),
                "mime_type": f.get("mimetype", ""),
                "file_size": f.get("size", 0),
            }
            for f in (event.get("files") or [])
        ]

        meta = {
            "user_id": user_id,
            "bot_id": bot_id,
            "channel_type": channel_type,
            "thread_ts": thread_ts,
        }

        broker_msg = self._BrokerMessage(
            platform="slack",
            chat_id=channel,
            sender_name=sender_name,
            sender_id=sender_id,
            content=text,
            agent_name=self._agent_name,
            message_id=ts,
            chat_title=chat_title,
            is_group=is_group,
            reply_to=thread_ts,
            metadata=meta,
            attachments=attachments,
        )

        _log(
            f"slack-poller[{self._agent_name}]: message from {sender_name} "
            f"in {channel}: {text[:50]}"
        )

        _deliver_in_background(
            self._broker.handle_inbound(broker_msg),
            f"slack-poller[{self._agent_name}]",
        )

        if self._event_callback:
            try:
                await self._event_callback(
                    platform="slack",
                    chat_id=str(channel),
                    sender=sender_name,
                    content=text,
                )
            except Exception as e:
                _log(f"slack-poller[{self._agent_name}]: event callback error: {e}")

    def stop(self) -> None:
        """Stop listening; schedule a best-effort close of the websocket.

        Sync to match the generic shutdown loop (``for p in _broker_pollers:
        p.stop()``). The close is scheduled via ``_deliver_in_background`` so the
        task is strongly referenced (not GC'd mid-flight) and any close error is
        logged rather than silently dropped.
        """
        self._running = False
        _log(f"slack-poller[{self._agent_name}]: stopping")
        client = self._client
        if client is not None:
            try:
                _deliver_in_background(
                    client.close(), f"slack-poller[{self._agent_name}] close"
                )
            except RuntimeError:
                # Fallback only: stop() called from a sync/no-loop context — no
                # loop to schedule the close on; the transport is reaped at exit.
                pass


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
