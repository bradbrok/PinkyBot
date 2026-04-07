"""Message Broker — routes platform messages to agent streaming sessions and back.

Pinky becomes the single message broker for all agent <-> platform communication.
Inbound: Platform message → check approved → route to agent streaming session
Outbound: Agent streaming session response → route back to platform

All routing uses persistent streaming sessions (non-blocking). The old query-based
buffer/drain path has been removed.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import urllib.request
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
    reply_to: str = ""
    metadata: dict = field(default_factory=dict)
    attachments: list[dict] = field(default_factory=list)


@dataclass
class MessageContext:
    """Resolved routing context for an inbound message."""

    agent_name: str
    message_id: str
    platform: str
    chat_id: str
    timestamp: float
    reply_to: str = ""
    is_group: bool = False
    source_was_voice: bool = False
    attachments: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "agent_name": self.agent_name,
            "message_id": self.message_id,
            "platform": self.platform,
            "chat_id": self.chat_id,
            "timestamp": self.timestamp,
            "reply_to": self.reply_to,
            "is_group": self.is_group,
            "source_was_voice": self.source_was_voice,
            "attachments": list(self.attachments),
            "metadata": dict(self.metadata),
        }


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
        reaction_callback=None,  # async fn(agent_name, platform, chat_id, message_id, emoji)
        typing_callback=None,  # async fn(agent_name, platform, chat_id) → show typing indicator
    ) -> None:
        self._registry = registry
        self._sessions = session_manager
        self._send_callback = send_callback
        self._reaction_callback = reaction_callback
        self._typing_callback = typing_callback
        self._stats = {"routed": 0, "pending": 0, "denied": 0, "errors": 0}

        # Streaming sessions — persistent ClaudeSDKClient connections per agent
        # agent_name -> {label -> StreamingSession}
        self._streaming: dict[str, dict[str, object]] = {}

        # Track voice-pending chats: (agent_name, chat_id) -> True when last inbound was voice
        self._voice_pending: dict[tuple[str, str], bool] = {}
        self._message_contexts: dict[tuple[str, str], MessageContext] = {}
        self._message_context_order: dict[str, list[str]] = {}

        # Active typing indicator tasks: (agent_name, chat_id) -> asyncio.Task
        self._typing_tasks: dict[tuple[str, str], asyncio.Task] = {}

    @property
    def send_callback(self):
        """Expose the send callback for direct use by scheduler etc."""
        return self._send_callback

    async def _typing_loop(
        self,
        agent_name: str,
        platform: str,
        chat_id: str,
    ) -> None:
        """Background task: send native typing action every 4s while agent is working."""
        if platform != "telegram":
            return
        raw_token = self._registry.get_raw_token(agent_name, platform)
        if not raw_token:
            return
        from pinky_outreach.telegram import TelegramAdapter
        adapter = TelegramAdapter(raw_token)
        try:
            while True:
                try:
                    await asyncio.to_thread(adapter.send_chat_action, chat_id, "typing")
                except Exception:
                    pass
                await asyncio.sleep(4)
        except asyncio.CancelledError:
            pass

    async def _start_typing(self, agent_name: str, platform: str, chat_id: str, streaming_session) -> None:
        """Start native typing indicator loop (Telegram header 'typing...' only)."""
        if platform != "telegram":
            return
        key = (agent_name, chat_id)
        existing = self._typing_tasks.pop(key, None)
        if existing and not existing.done():
            existing.cancel()
        task = asyncio.create_task(self._typing_loop(agent_name, platform, chat_id))
        self._typing_tasks[key] = task
        _log(f"broker: typing indicator started for {agent_name}/{chat_id}")

    def _stop_typing(self, agent_name: str, chat_id: str) -> None:
        """Stop the native typing indicator loop."""
        key = (agent_name, chat_id)
        task = self._typing_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
        _log(f"broker: typing indicator stopped for {agent_name}/{chat_id}")

    async def _send_message(self, agent_name: str, platform: str, chat_id: str, content: str) -> None:
        """Send a message if the outbound callback is configured."""
        if self._send_callback:
            await self._send_callback(agent_name, platform, chat_id, content)

    async def _add_reaction(
        self,
        agent_name: str,
        platform: str,
        chat_id: str,
        message_id: str,
        emoji: str,
    ) -> bool:
        """Add a reaction if the outbound callback is configured."""
        if not (self._reaction_callback and message_id and emoji):
            return False
        await self._reaction_callback(agent_name, platform, chat_id, message_id, emoji)
        return True

    def remember_message_context(self, message: BrokerMessage, *, source_was_voice: bool = False) -> None:
        """Store inbound routing context for later reply()/react() resolution."""
        if not message.message_id:
            return
        key = (message.agent_name, message.message_id)
        self._message_contexts[key] = MessageContext(
            agent_name=message.agent_name,
            message_id=message.message_id,
            platform=message.platform,
            chat_id=message.chat_id,
            timestamp=message.timestamp,
            reply_to=message.reply_to,
            is_group=message.is_group,
            source_was_voice=source_was_voice,
            attachments=list(message.attachments or []),
            metadata=dict(message.metadata or {}),
        )
        order = self._message_context_order.setdefault(message.agent_name, [])
        if message.message_id in order:
            order.remove(message.message_id)
        order.append(message.message_id)
        if len(order) > 1000:
            stale_id = order.pop(0)
            self._message_contexts.pop((message.agent_name, stale_id), None)

    def get_message_context(self, agent_name: str, message_id: str) -> MessageContext | None:
        """Resolve an inbound message context by agent and message ID."""
        return self._message_contexts.get((agent_name, message_id))

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
        from datetime import datetime
        from datetime import timezone as tz

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
            header = f"[{message.platform} | group | {display} | {message.sender_name} | {message.chat_id} | {ts}{msg_id}]"
        else:
            header = f"[{message.platform} | dm | {message.sender_name} | {message.chat_id} | {ts}{msg_id}]"

        body = message.content

        # Append attachment info if present
        _IMAGE_TYPES = {"photo", "sticker", "animation"}
        if message.attachments:
            parts = []
            for att in message.attachments:
                att_type = att.get("type", "file")
                file_name = att.get("file_name", "")
                file_id = att.get("file_id", "")
                local_path = att.get("local_path", "")
                if local_path:
                    parts.append(f"{att_type}: {local_path}")
                elif file_name:
                    parts.append(f"{att_type}: {file_name} (file_id: {file_id})")
                else:
                    parts.append(f"{att_type} (file_id: {file_id})")
            body += f"\n\U0001F4CE Attachments: {', '.join(parts)}"
            has_images = any(
                a.get("local_path") and a.get("type") in _IMAGE_TYPES
                for a in message.attachments
            )
            if has_images:
                body += "\n(Use Read to view the image)"

        return f"{header}\n{body}"

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

    def _get_streaming_session(self, agent_name: str, chat_id: str = "", *, label: str = ""):
        """Get the streaming session for an agent + channel.

        Looks up the channel→session assignment, falls back to 'main'.
        """
        sessions = self._streaming.get(agent_name, {})
        if not sessions:
            return None
        if label:
            return sessions.get(label)
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

        # Auto-wake: if session exists but is disconnected (idle sleep), reconnect
        if streaming and not streaming.is_connected and streaming.session_id:
            _log(f"broker: {agent_name} is sleeping — auto-waking for inbound message")
            try:
                await streaming.connect()
                _log(f"broker: {agent_name} auto-woke successfully")
            except Exception as e:
                _log(f"broker: {agent_name} auto-wake failed: {e}")
                streaming = None

        if not streaming or not streaming.is_connected:
            _log(f"broker: streaming session for {agent_name} not connected, dropping message")
            self._stats["errors"] += 1
            await self._send_message(
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

        # Photo handling: pre-download image attachments so the agent can view them
        await self._download_photo_attachments(agent_name, message)

        # Voice handling: transcribe voice attachments before routing
        has_voice = any(
            att.get("type") == "voice"
            for att in (message.attachments or [])
        )
        if has_voice:
            transcript = await self._transcribe_voice(agent_name, message)
            if transcript:
                if message.content:
                    message.content += f"\n\n[Voice transcript]: {transcript}"
                else:
                    message.content = f"[Voice message]: {transcript}"
                self._voice_pending[(agent_name, message.chat_id)] = True
            else:
                # Transcription failed — notify user so the voice isn't silently lost
                _log(f"broker: voice transcription failed for {agent_name}, sending fallback")
                await self._send_message(
                    agent_name, message.platform, message.chat_id,
                    "I received your voice message but couldn't transcribe it — please try again or send text.",
                )
                return
        else:
            self._voice_pending.pop((agent_name, message.chat_id), None)

        self.remember_message_context(message, source_was_voice=has_voice)

        # Format and send — non-blocking
        prompt = self._format_prompt(message)
        await streaming.send(
            prompt,
            platform=message.platform,
            chat_id=message.chat_id,
            message_id=message.message_id,
        )
        # Start typing indicator for Telegram chats
        if message.chat_id:
            await self._start_typing(agent_name, message.platform, message.chat_id, streaming)
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

        from datetime import datetime
        from datetime import timezone as tz
        ts = datetime.now(tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        prompt = f"[agent | {from_agent} | internal | {ts}]\n{message}"
        await streaming.send(prompt)
        self._stats["routed"] += 1
        _log(f"broker: injected agent message {from_agent} -> {to_agent}")
        return True

    # ── Response Routing ───────────────────────────────────

    async def route_response(
        self,
        agent_name: str,
        platform: str,
        chat_id: str,
        response: str,
        *,
        message_id: str = "",
        used_outreach: bool = False,
        fallback_enabled: bool = False,
    ) -> None:
        """Deliver plain-text fallback for a completed turn when appropriate."""
        stripped = response.strip()
        _log(
            f"broker: route_response for {agent_name} ({platform}/{chat_id}): "
            f"outreach={used_outreach} fallback={fallback_enabled} text={stripped[:80]}..."
        )

        # Always stop the typing indicator when a turn completes
        if chat_id:
            self._stop_typing(agent_name, chat_id)

        if used_outreach:
            _log(f"broker: {agent_name} handled turn via outreach tools")
            return
        if not stripped or not fallback_enabled or not chat_id:
            return

        voice_key = (agent_name, chat_id)
        if self._voice_pending.pop(voice_key, False):
            sent_voice = await self._try_voice_reply(agent_name, platform, chat_id, stripped)
            if not sent_voice:
                await self._send_message(agent_name, platform, chat_id, stripped)
        else:
            await self._send_message(agent_name, platform, chat_id, stripped)

    _DOWNLOADABLE_TYPES = {"photo", "document", "video", "animation", "sticker"}

    async def _download_photo_attachments(
        self, agent_name: str, message: BrokerMessage,
    ) -> None:
        """Pre-download image/file attachments so the agent can view them via Read."""
        if not message.attachments:
            return

        downloadable = [
            a for a in message.attachments
            if a.get("type") in self._DOWNLOADABLE_TYPES and a.get("file_id")
        ]
        if not downloadable:
            return

        # Get bot token for this agent's platform
        raw_token = self._registry.get_raw_token(agent_name, message.platform)
        if not raw_token:
            _log(f"broker: no {message.platform} token for {agent_name}, skip attachments")
            return

        # Download into the agent's working directory
        agent = self._registry.get(agent_name)
        if not agent:
            return
        dest_dir = os.path.join(agent.working_dir, "attachments")
        os.makedirs(dest_dir, exist_ok=True)

        from pinky_outreach.telegram import TelegramAdapter
        adapter = TelegramAdapter(raw_token)

        for att in downloadable:
            try:
                local_path = adapter.download_file(att["file_id"], dest_dir=dest_dir)
                att["local_path"] = os.path.abspath(local_path)
                _log(f"broker: downloaded {att['type']} for {agent_name}: {local_path}")
            except Exception as e:
                _log(f"broker: failed to download {att['type']} for {agent_name}: {e}")

    async def _transcribe_voice(self, agent_name: str, message: BrokerMessage) -> str:
        """Download and transcribe a voice attachment. Returns transcript or empty string."""
        agent = self._registry.get(agent_name)
        if not agent:
            return ""

        voice_cfg = agent.voice_config or {}
        provider = voice_cfg.get("transcribe_provider", "openai")

        # Find the voice attachment
        voice_att = next(
            (a for a in (message.attachments or []) if a.get("type") == "voice"),
            None,
        )
        if not voice_att or not voice_att.get("file_id"):
            return ""

        # Download the voice file via Telegram adapter
        file_id = voice_att["file_id"]
        try:
            # Use the send_callback's adapter to download
            from pinky_outreach.telegram import TelegramAdapter
            # Get the bot token for this agent
            raw_token = self._registry.get_raw_token(agent_name, "telegram")
            if not raw_token:
                _log(f"broker: no telegram token for {agent_name}, can't download voice")
                return ""
            adapter = TelegramAdapter(raw_token)
            local_path = adapter.download_file(file_id, dest_dir=tempfile.mkdtemp(prefix="pinky_voice_"))
            _log(f"broker: downloaded voice file for {agent_name}: {local_path}")
        except Exception as e:
            _log(f"broker: failed to download voice for {agent_name}: {e}")
            return ""

        # Transcribe
        try:
            api_key = self._registry.get_setting(f"{provider.upper()}_API_KEY") or os.environ.get(f"{provider.upper()}_API_KEY", "")
            if not api_key and provider == "openai":
                api_key = self._registry.get_setting("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
            if not api_key and provider == "deepgram":
                api_key = self._registry.get_setting("DEEPGRAM_API_KEY") or os.environ.get("DEEPGRAM_API_KEY", "")

            if not api_key:
                _log(f"broker: no API key for {provider} transcription")
                return ""

            if provider == "openai":
                import httpx
                async with httpx.AsyncClient(timeout=30) as client:
                    with open(local_path, "rb") as f:
                        resp = await client.post(
                            "https://api.openai.com/v1/audio/transcriptions",
                            headers={"Authorization": f"Bearer {api_key}"},
                            files={"file": (os.path.basename(local_path), f, "audio/ogg")},
                            data={"model": "whisper-1"},
                        )
                    resp.raise_for_status()
                    transcript = resp.json().get("text", "")

            elif provider == "deepgram":
                with open(local_path, "rb") as f:
                    audio_data = f.read()
                req = urllib.request.Request(
                    "https://api.deepgram.com/v1/listen?model=nova-3&smart_format=true",
                    data=audio_data,
                    method="POST",
                    headers={
                        "Authorization": f"Token {api_key}",
                        "Content-Type": "audio/ogg",
                    },
                )
                with urllib.request.urlopen(req, timeout=30) as resp:
                    result = json.loads(resp.read())
                transcript = (
                    result.get("results", {})
                    .get("channels", [{}])[0]
                    .get("alternatives", [{}])[0]
                    .get("transcript", "")
                )
            else:
                _log(f"broker: unknown transcription provider: {provider}")
                return ""

            _log(f"broker: transcribed voice for {agent_name} ({provider}): {transcript[:80]}...")
            return transcript
        except Exception as e:
            _log(f"broker: transcription failed for {agent_name}: {e}")
            return ""
        finally:
            try:
                os.unlink(local_path)
            except Exception:
                pass

    async def _try_voice_reply(
        self, agent_name: str, platform: str, chat_id: str, text: str,
    ) -> bool:
        """Try to send a voice reply via TTS. Returns True if sent."""
        agent = self._registry.get(agent_name)
        if not agent:
            return False

        voice_cfg = agent.voice_config or {}
        if not voice_cfg.get("voice_reply", False):
            return False

        # Resolve provider/voice — check platform overrides first
        platform_cfg = voice_cfg.get("platforms", {}).get(platform, {})
        provider = platform_cfg.get("tts_provider") or voice_cfg.get("tts_provider", "openai")
        voice = platform_cfg.get("tts_voice") or voice_cfg.get("tts_voice", "")
        model = platform_cfg.get("tts_model") or voice_cfg.get("tts_model", "")

        # Use the broker/send-voice endpoint which handles TTS + send
        try:
            body = json.dumps({
                "agent_name": agent_name,
                "platform": platform,
                "chat_id": chat_id,
                "text": text,
                "provider": provider,
                "voice": voice,
                "model": model,
            }).encode()
            req = urllib.request.Request(
                "http://localhost:8888/broker/send-voice",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )
            with urllib.request.urlopen(req, timeout=60) as resp:
                result = json.loads(resp.read())
            if result.get("sent"):
                _log(f"broker: voice reply sent for {agent_name} ({provider}/{voice})")
                # Also send text version for accessibility
                await self._send_message(agent_name, platform, chat_id, text)
                return True
            else:
                _log(f"broker: voice reply failed: {result}")
                return False
        except Exception as e:
            _log(f"broker: voice reply error for {agent_name}: {e}")
            return False

    async def _broadcast(self, agent_name: str, body: str) -> None:
        """Send a message to all active channels for an agent."""
        if not self._send_callback:
            return

        # Send to all approved users (DMs)
        users = self._registry.list_approved_users(agent_name)
        for u in users:
            if u.status == "approved":
                try:
                    await self._send_message(agent_name, "telegram", u.chat_id, body)
                except Exception as e:
                    _log(f"broker: broadcast to user {u.chat_id} failed: {e}")

        # Send to all active groups
        groups = self._registry.list_group_chats(agent_name)
        for g in groups:
            try:
                await self._send_message(agent_name, g["platform"], g["chat_id"], body)
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
        lines.append("## Messaging Tools (pinky-messaging)")
        lines.append("Use explicit outreach tools for messaging:")
        lines.append("- **send(chat_id, platform, text)**: Default response tool — flat message, no threading")
        lines.append("- **thread(message_id, text)**: Threaded/quoted reply — use when you want to quote a specific message")
        lines.append("- **react(message_id, emoji)**: React to an inbound message")
        lines.append("- **send_gif / send_voice / send_photo / send_document**: Send rich media")
        lines.append("- **broadcast(text)**: Send to every active channel")
        lines.append("")
        lines.append("## Delivery Model")
        lines.append("- If you do not call an outreach tool, Pinky may deliver your plain text automatically")
        lines.append("- `send()` is the default tool for responding to inbound messages")

        return "\n".join(lines)

    def get_live_agents(self) -> list[str]:
        """Return names of agents with connected streaming sessions."""
        return [
            name for name, sessions in self._streaming.items()
            if any(s.is_connected for s in sessions.values())
        ]

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
            {"id": s.id, "label": label, "connected": s.is_connected, "stats": s.stats}
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
