"""Message Broker — routes platform messages to agent sessions and back.

Pinky becomes the single message broker for all agent <-> platform communication.
Inbound: Platform message → check approved → route to agent session
Outbound: Agent session response → route back to platform

Non-blocking: messages buffer per-agent. If the session is busy, new messages
coalesce and get delivered as one combined prompt when the current turn finishes.
"""

from __future__ import annotations

import asyncio
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field

from pinky_daemon.agent_registry import AgentRegistry


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class BrokerMessage:
    """A message flowing through the broker."""
    platform: str  # telegram, discord, slack
    chat_id: str
    sender_name: str
    sender_id: str
    content: str
    agent_name: str
    timestamp: float = field(default_factory=time.time)
    message_id: str = ""
    chat_title: str = ""
    is_group: bool = False
    metadata: dict = field(default_factory=dict)


class MessageBroker:
    """Routes platform messages to agent sessions and back.

    Flow:
    1. Inbound message arrives from platform poller
    2. Check sender status in approved_users
    3. If denied → silently drop
    4. If unknown → add as pending, store message in pending_messages queue
    5. If approved → add to agent's message buffer, kick drain loop

    Non-blocking coalescing:
    - Each agent has a message buffer (list of BrokerMessages)
    - A drain loop per agent processes buffered messages
    - If the session is busy when new messages arrive, they accumulate
    - When the session finishes, all buffered messages are sent as one prompt
    """

    def __init__(
        self,
        registry: AgentRegistry,
        session_manager,  # SessionManager — avoid circular import
        *,
        send_callback=None,  # async fn(agent_name, platform, chat_id, content) → send reply
        typing_callback=None,  # async fn(agent_name, platform, chat_id) → show typing indicator
    ) -> None:
        self._registry = registry
        self._sessions = session_manager
        self._send_callback = send_callback
        self._typing_callback = typing_callback
        self._stats = {"routed": 0, "pending": 0, "denied": 0, "errors": 0, "coalesced": 0}

        # Streaming sessions — persistent ClaudeSDKClient connections per agent
        self._streaming: dict[str, object] = {}  # agent_name -> StreamingSession

        # Per-agent message buffers and drain state (fallback for non-streaming)
        self._buffers: dict[str, list[BrokerMessage]] = defaultdict(list)
        self._draining: set[str] = set()  # Agents currently being drained

    async def handle_inbound(self, message: BrokerMessage) -> None:
        """Handle an incoming platform message. Non-blocking — buffers and returns immediately."""
        agent_name = message.agent_name

        # 1. Check sender status
        status = self._registry.get_user_status(agent_name, message.chat_id)

        if status == "denied":
            self._stats["denied"] += 1
            _log(f"broker: denied message from {message.chat_id} for {agent_name}")
            return

        if status is None or status == "pending":
            # Unknown or already-pending user — add/update as pending, queue message
            if status is None:
                self._registry.add_pending_user(
                    agent_name, message.chat_id,
                    display_name=message.sender_name,
                )
            self._registry.queue_pending_message(
                agent_name=agent_name,
                platform=message.platform,
                chat_id=message.chat_id,
                sender_name=message.sender_name,
                content=message.content,
            )
            self._stats["pending"] += 1
            _log(f"broker: queued message from pending user {message.sender_name} ({message.chat_id}) for {agent_name}")
            return

        # 2. Approved — route via streaming session if available, else buffer
        if agent_name in self._streaming:
            await self._route_streaming(agent_name, message)
            return

        # Fallback: buffer and drain (for non-streaming sessions)
        self._buffers[agent_name].append(message)
        buf_size = len(self._buffers[agent_name])
        _log(f"broker: buffered message for {agent_name} (buffer: {buf_size})")

        # Start drain loop if not already running
        if agent_name not in self._draining:
            asyncio.create_task(self._drain_loop(agent_name))

    async def _drain_loop(self, agent_name: str) -> None:
        """Drain the message buffer for an agent. Runs until buffer is empty."""
        if agent_name in self._draining:
            return  # Already draining
        self._draining.add(agent_name)

        try:
            while self._buffers[agent_name]:
                # Grab all buffered messages at once
                messages = self._buffers[agent_name]
                self._buffers[agent_name] = []

                if len(messages) > 1:
                    self._stats["coalesced"] += len(messages) - 1

                await self._route_batch(agent_name, messages)
        finally:
            self._draining.discard(agent_name)

    def _format_prompt(self, message: BrokerMessage) -> str:
        """Format a single message as a platform-aware prompt line."""
        from datetime import datetime, timezone as tz

        agent_name = message.agent_name
        user_tz_str = self._registry.get_user_timezone(agent_name, message.chat_id)
        if user_tz_str:
            try:
                from zoneinfo import ZoneInfo
                user_tz = ZoneInfo(user_tz_str)
                dt = datetime.fromtimestamp(message.timestamp, tz=user_tz)
                ts = dt.strftime(f"%Y-%m-%d %H:%M:%S {user_tz_str}")
            except Exception:
                ts = datetime.fromtimestamp(message.timestamp, tz=tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        else:
            ts = datetime.fromtimestamp(message.timestamp, tz=tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        msg_id = f" | msg_id:{message.message_id}" if message.message_id else ""
        return f"[{message.platform} | {message.sender_name} | {message.chat_id} | {ts}{msg_id}]\n{message.content}"

    async def _route_batch(self, agent_name: str, messages: list[BrokerMessage]) -> None:
        """Route a batch of messages to the agent's session."""
        session_id = f"{agent_name}-main"
        session = self._sessions.get(session_id)
        if not session:
            _log(f"broker: no session {session_id} found for {agent_name}")
            for msg in messages:
                if self._send_callback:
                    await self._send_callback(
                        agent_name, msg.platform, msg.chat_id,
                        f"⚠️ {agent_name} is not running right now. Try again later."
                    )
            return

        # Build combined prompt from all messages
        prompts = [self._format_prompt(msg) for msg in messages]
        combined = "\n\n".join(prompts)

        if len(messages) > 1:
            _log(f"broker: coalescing {len(messages)} messages for {agent_name}")

        # Collect unique chat_ids for typing indicators and responses
        chat_ids = {}
        for msg in messages:
            key = (msg.platform, msg.chat_id)
            chat_ids[key] = msg

        # Show typing indicator to all senders
        if self._typing_callback:
            for (platform, chat_id) in chat_ids:
                try:
                    await self._typing_callback(agent_name, platform, chat_id)
                except Exception:
                    pass

        try:
            response = await session.send(combined)
            self._stats["routed"] += len(messages)

            # Send response back to all unique senders
            if response.content and self._send_callback:
                for (platform, chat_id) in chat_ids:
                    await self._send_callback(
                        agent_name, platform, chat_id,
                        response.content,
                    )
        except Exception as e:
            self._stats["errors"] += 1
            _log(f"broker: error routing to {session_id}: {e}")
            for (platform, chat_id) in chat_ids:
                if self._send_callback:
                    try:
                        await self._send_callback(
                            agent_name, platform, chat_id,
                            f"⚠️ Error processing your message. Please try again."
                        )
                    except Exception:
                        pass

    async def handle_approval(self, agent_name: str, chat_id: str) -> int:
        """When a pending user is approved, deliver their held messages.

        Returns the number of messages delivered.
        """
        pending = self._registry.get_pending_messages(agent_name, chat_id)
        if not pending:
            _log(f"broker: delivered 0/0 pending messages for {chat_id} to {agent_name}")
            return 0

        # Build broker messages and route as a batch
        broker_msgs = [
            BrokerMessage(
                platform=msg["platform"],
                chat_id=msg["chat_id"],
                sender_name=msg["sender_name"],
                sender_id="",
                content=msg["content"],
                agent_name=agent_name,
                timestamp=msg["created_at"],
            )
            for msg in pending
        ]

        # Buffer and drain (non-blocking)
        self._buffers[agent_name].extend(broker_msgs)
        if agent_name not in self._draining:
            asyncio.create_task(self._drain_loop(agent_name))

        # Mark as delivered
        self._registry.mark_pending_delivered(agent_name, chat_id)

        _log(f"broker: queued {len(pending)} pending messages for delivery to {agent_name}")
        return len(pending)

    # ── Streaming Session Support ─────────────────────────

    async def _route_streaming(self, agent_name: str, message: BrokerMessage) -> None:
        """Route a message via streaming session — non-blocking."""
        streaming = self._streaming.get(agent_name)
        if not streaming or not streaming.is_connected:
            _log(f"broker: streaming session for {agent_name} not connected, falling back to buffer")
            self._buffers[agent_name].append(message)
            if agent_name not in self._draining:
                asyncio.create_task(self._drain_loop(agent_name))
            return

        # Show typing indicator
        if self._typing_callback:
            try:
                await self._typing_callback(agent_name, message.platform, message.chat_id)
            except Exception:
                pass

        # Format and send — non-blocking
        prompt = self._format_prompt(message)
        await streaming.send(prompt, chat_id=message.chat_id)
        self._stats["routed"] += 1
        _log(f"broker: streamed message to {agent_name} (non-blocking)")

    async def inject_agent_message(
        self, from_agent: str, to_agent: str, message: str,
    ) -> bool:
        """Inject a message from one agent into another's streaming session."""
        streaming = self._streaming.get(to_agent)
        if not streaming or not streaming.is_connected:
            _log(f"broker: can't deliver agent message to {to_agent} — not connected")
            return False

        from datetime import datetime, timezone as tz
        ts = datetime.now(tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        prompt = f"[agent | {from_agent} | internal | {ts}]\n{message}"
        await streaming.send(prompt)
        self._stats["routed"] += 1
        _log(f"broker: injected agent message {from_agent} -> {to_agent}")
        return True

    def register_streaming(self, agent_name: str, session) -> None:
        """Register a StreamingSession for an agent."""
        self._streaming[agent_name] = session
        _log(f"broker: registered streaming session for {agent_name}")

    def unregister_streaming(self, agent_name: str) -> None:
        """Unregister a streaming session."""
        self._streaming.pop(agent_name, None)
        _log(f"broker: unregistered streaming session for {agent_name}")

    @property
    def stats(self) -> dict:
        stats = dict(self._stats)
        stats["buffered"] = {k: len(v) for k, v in self._buffers.items() if v}
        stats["draining"] = list(self._draining)
        stats["streaming"] = {
            name: s.stats for name, s in self._streaming.items()
        }
        return stats
