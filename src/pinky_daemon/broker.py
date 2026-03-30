"""Message Broker — routes platform messages to agent streaming sessions and back.

Pinky becomes the single message broker for all agent <-> platform communication.
Inbound: Platform message → check approved → route to agent streaming session
Outbound: Agent streaming session response → route back to platform

All routing uses persistent streaming sessions (non-blocking). The old query-based
buffer/drain path has been removed.
"""

from __future__ import annotations

import asyncio
import sys
import time
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
    """Routes platform messages to agent streaming sessions and back.

    Flow:
    1. Inbound message arrives from platform poller
    2. Check sender status in approved_users
    3. If denied → silently drop
    4. If unknown → add as pending, store message in pending_messages queue
    5. If approved → route to agent's streaming session (non-blocking)
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
        self._stats = {"routed": 0, "pending": 0, "denied": 0, "errors": 0}

        # Streaming sessions — persistent ClaudeSDKClient connections per agent
        # agent_name -> {label -> StreamingSession}
        self._streaming: dict[str, dict[str, object]] = {}

    async def handle_inbound(self, message: BrokerMessage) -> None:
        """Handle an incoming platform message. Non-blocking."""
        agent_name = message.agent_name

        # 1. Check sender status — always use the individual sender_id for approval,
        #    not the chat_id (which is the group ID for group messages).
        user_id = message.sender_id or message.chat_id
        status = self._registry.get_user_status(agent_name, user_id)

        if status == "denied":
            self._stats["denied"] += 1
            _log(f"broker: denied message from {message.sender_name} ({user_id}) for {agent_name}")
            return

        if status is None or status == "pending":
            # Auto-approve primary user
            primary = self._registry.get_primary_user()
            if primary.get("chat_id") and user_id == primary["chat_id"]:
                self._registry.approve_user(
                    agent_name, user_id,
                    display_name=primary.get("display_name") or message.sender_name,
                    approved_by="primary_user",
                )
                _log(f"broker: auto-approved primary user {user_id} for {agent_name}")
                # Fall through to routing below
            else:
                # Unknown or pending user — queue message
                if status is None:
                    self._registry.add_pending_user(
                        agent_name, user_id,
                        display_name=message.sender_name,
                    )
                self._registry.queue_pending_message(
                    agent_name=agent_name,
                    platform=message.platform,
                    chat_id=user_id,
                    sender_name=message.sender_name,
                    content=message.content,
                )
                self._stats["pending"] += 1
                _log(f"broker: queued message from pending user {message.sender_name} ({user_id}) for {agent_name}")
                return

        # 2. Approved — route via streaming session
        await self._route_streaming(agent_name, message)

    def _format_prompt(self, message: BrokerMessage) -> str:
        """Format a single message as a platform-aware prompt line."""
        from datetime import datetime, timezone as tz

        agent_name = message.agent_name
        tz_str = (
            self._registry.get_user_timezone(agent_name, message.chat_id)
            or self._registry.get_default_timezone()
        )
        try:
            from zoneinfo import ZoneInfo
            dt = datetime.fromtimestamp(message.timestamp, tz=ZoneInfo(tz_str))
            ts = dt.strftime(f"%Y-%m-%d %H:%M:%S {tz_str}")
        except Exception:
            ts = datetime.fromtimestamp(message.timestamp, tz=tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        msg_id = f" | msg_id:{message.message_id}" if message.message_id else ""
        if message.is_group:
            alias = self._registry.get_group_chat_alias(message.agent_name, message.chat_id)
            display = alias or message.chat_title or message.chat_id
            return f"[{message.platform} | group | {display} | {message.sender_name} | {message.chat_id} | {ts}{msg_id}]\n{message.content}"
        else:
            return f"[{message.platform} | dm | {message.sender_name} | {message.chat_id} | {ts}{msg_id}]\n{message.content}"

    async def handle_approval(self, agent_name: str, chat_id: str) -> int:
        """When a pending user is approved, deliver their held messages.

        Returns the number of messages delivered.
        """
        pending = self._registry.get_pending_messages(agent_name, chat_id)
        if not pending:
            _log(f"broker: delivered 0/0 pending messages for {chat_id} to {agent_name}")
            return 0

        # Route pending messages through streaming
        for msg in pending:
            broker_msg = BrokerMessage(
                platform=msg["platform"],
                chat_id=msg["chat_id"],
                sender_name=msg["sender_name"],
                sender_id="",
                content=msg["content"],
                agent_name=agent_name,
                timestamp=msg["created_at"],
            )
            await self._route_streaming(agent_name, broker_msg)

        # Mark as delivered
        self._registry.mark_pending_delivered(agent_name, chat_id)

        _log(f"broker: queued {len(pending)} pending messages for delivery to {agent_name}")
        return len(pending)

    # ── Streaming Session Support ─────────────────────────

    def _get_streaming_session(self, agent_name: str, chat_id: str = ""):
        """Get the streaming session for an agent + channel.

        Looks up the channel→session assignment, falls back to 'main'.
        """
        sessions = self._streaming.get(agent_name, {})
        if not sessions:
            return None
        if chat_id:
            label = self._registry.get_channel_session(agent_name, chat_id)
            session = sessions.get(label)
            if session and session.is_connected:
                return session
        # Fall back to main
        return sessions.get("main")

    async def _route_streaming(self, agent_name: str, message: BrokerMessage) -> None:
        """Route a message via streaming session — non-blocking."""
        streaming = self._get_streaming_session(agent_name, message.chat_id)
        if not streaming or not streaming.is_connected:
            _log(f"broker: streaming session for {agent_name} not connected, dropping message")
            self._stats["errors"] += 1
            if self._send_callback:
                await self._send_callback(
                    agent_name, message.platform, message.chat_id,
                    f"⚠️ {agent_name} is not running right now. Try again later.",
                )
            return

        # Show typing indicator
        if self._typing_callback:
            try:
                await self._typing_callback(agent_name, message.platform, message.chat_id)
            except Exception:
                pass

        # Format and send — non-blocking
        prompt = self._format_prompt(message)
        await streaming.send(prompt, platform=message.platform, chat_id=message.chat_id)
        self._stats["routed"] += 1
        _log(f"broker: streamed message to {agent_name} (non-blocking)")

    async def inject_agent_message(
        self, from_agent: str, to_agent: str, message: str,
    ) -> bool:
        """Inject a message from one agent into another's streaming session."""
        streaming = self._get_streaming_session(to_agent)
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

    # ── Response Routing ───────────────────────────────────

    async def route_response(
        self, agent_name: str, platform: str, chat_id: str, response: str,
    ) -> None:
        """Parse agent response for routing directives and deliver.

        Protocol:
        - '[no reply]' (full response) → suppress
        - '@channel:<alias-or-id>\\n<body>' → resolve and send to that channel
        - '@all\\n<body>' → broadcast to all active channels
        - Plain text → send to (platform, chat_id) that triggered this turn
        """
        stripped = response.strip()
        _log(f"broker: route_response for {agent_name} ({platform}/{chat_id}): {stripped[:80]}...")

        # No-reply signal — anywhere in the response
        if "[no reply]" in stripped.lower() or "[no response]" in stripped.lower():
            _log(f"broker: {agent_name} suppressed reply (no reply)")
            return

        # Explicit channel targeting
        first_line, _, remainder = stripped.partition("\n")
        first_line = first_line.strip()

        if first_line.lower().startswith("@all"):
            body = remainder.strip() if remainder.strip() else first_line[4:].strip()
            if body:
                await self._broadcast(agent_name, body)
            return

        if first_line.lower().startswith("@channel:"):
            after_prefix = first_line[9:].strip()
            # Target can be "chat_id body" on same line, or "chat_id\nbody" on next line
            if " " in after_prefix and not remainder.strip():
                # Same-line: @channel:chat_id body text here
                target, body = after_prefix.split(" ", 1)
            else:
                target = after_prefix
                body = remainder.strip() if remainder.strip() else ""
            if target and body:
                resolved = self._resolve_channel(agent_name, target)
                if resolved:
                    r_platform, r_chat_id = resolved
                    if self._send_callback:
                        await self._send_callback(agent_name, r_platform, r_chat_id, body)
                    _log(f"broker: {agent_name} targeted channel {target} -> {r_chat_id}")
                else:
                    _log(f"broker: {agent_name} targeted unknown channel '{target}', falling back to default")
                    if self._send_callback and chat_id:
                        await self._send_callback(agent_name, platform, chat_id, response)
            return

        # Default: send to the triggering chat
        if self._send_callback and chat_id:
            await self._send_callback(agent_name, platform, chat_id, response)

    def _resolve_channel(self, agent_name: str, target: str) -> tuple[str, str] | None:
        """Resolve a channel alias or chat_id to (platform, chat_id)."""
        target_lower = target.lower()

        # Check group_chats (alias then chat_id)
        groups = self._registry.list_group_chats(agent_name)
        for g in groups:
            if g["alias"] and g["alias"].lower() == target_lower:
                return (g["platform"], g["chat_id"])
            if g["chat_id"] == target:
                return (g["platform"], g["chat_id"])

        # Check approved_users (display_name then chat_id)
        users = self._registry.list_approved_users(agent_name)
        for u in users:
            if u.display_name and u.display_name.lower() == target_lower:
                return ("telegram", u.chat_id)  # TODO: platform from user record
            if u.chat_id == target:
                return ("telegram", u.chat_id)

        return None

    async def _broadcast(self, agent_name: str, body: str) -> None:
        """Send a message to all active channels for an agent."""
        if not self._send_callback:
            return

        # Send to all approved users (DMs)
        users = self._registry.list_approved_users(agent_name)
        for u in users:
            if u.status == "approved":
                try:
                    await self._send_callback(agent_name, "telegram", u.chat_id, body)
                except Exception as e:
                    _log(f"broker: broadcast to user {u.chat_id} failed: {e}")

        # Send to all active groups
        groups = self._registry.list_group_chats(agent_name)
        for g in groups:
            try:
                await self._send_callback(agent_name, g["platform"], g["chat_id"], body)
            except Exception as e:
                _log(f"broker: broadcast to group {g['chat_id']} failed: {e}")

        _log(f"broker: {agent_name} broadcast to {len(users)} users + {len(groups)} groups")

    def build_channel_context(self, agent_name: str) -> str:
        """Build a channel context string for the agent's system prompt / wake context."""
        lines = ["## Active Channels"]

        users = self._registry.list_approved_users(agent_name)
        for u in users:
            if u.status == "approved":
                label = u.display_name or u.chat_id
                lines.append(f"- {label} (dm, {u.chat_id})")

        groups = self._registry.list_group_chats(agent_name)
        for g in groups:
            label = g["alias"] or g["chat_title"] or g["chat_id"]
            lines.append(f"- {label} (group, {g['platform']}, {g['chat_id']})")

        lines.append("")
        lines.append("## Response Routing")
        lines.append("- Default: your response goes to whoever messaged you")
        lines.append("- Target a specific channel: start response with @channel:<name-or-id>")
        lines.append("- Broadcast to all: start response with @all")
        lines.append("- Suppress reply: respond with just [no reply]")

        return "\n".join(lines)

    def register_streaming(self, agent_name: str, session, label: str = "main") -> None:
        """Register a StreamingSession for an agent under a label."""
        if agent_name not in self._streaming:
            self._streaming[agent_name] = {}
        self._streaming[agent_name][label] = session
        _log(f"broker: registered streaming session for {agent_name}/{label}")

    def unregister_streaming(self, agent_name: str, label: str = "") -> None:
        """Unregister a streaming session. If no label, remove all for the agent."""
        if label:
            sessions = self._streaming.get(agent_name, {})
            sessions.pop(label, None)
            if not sessions:
                self._streaming.pop(agent_name, None)
            _log(f"broker: unregistered streaming session for {agent_name}/{label}")
        else:
            self._streaming.pop(agent_name, None)
            _log(f"broker: unregistered all streaming sessions for {agent_name}")

    def list_streaming_sessions(self, agent_name: str) -> list[dict]:
        """List streaming session labels and status for an agent."""
        sessions = self._streaming.get(agent_name, {})
        return [
            {"label": label, "connected": s.is_connected, "stats": s.stats}
            for label, s in sessions.items()
        ]

    @property
    def stats(self) -> dict:
        stats = dict(self._stats)
        stats["streaming"] = {
            name: {label: s.stats for label, s in sessions.items()}
            for name, sessions in self._streaming.items()
        }
        return stats
