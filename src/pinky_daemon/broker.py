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
from pinky_daemon.transport_state import SessionState

# Bounded wait for an in-flight reconnect/restart to complete before we drop an
# inbound message. Covers the context_restart window where the streaming session
# object exists but ``state`` is briefly != CONNECTED between
# ``disconnect()`` → ``connect()``. Without this, messages arriving during a
# restart get silently dropped and the user sees the "not running" fallback
# even though the session is about to come back up. See issue tracker bug
# reported 2026-05-13 by bradbrok.
_INBOUND_RECONNECT_WAIT_SEC = 20.0
_INBOUND_RECONNECT_POLL_SEC = 0.25


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _make_gif_preview(src_path: str) -> str | None:
    """Extract 4 evenly-spaced frames from a GIF or video and composite into a 2×2 grid.

    Returns the path to the saved preview image, or None on failure.
    Uses ffmpeg for frame extraction (handles MP4 animations and GIFs),
    then PIL to composite the grid.
    """
    import shutil
    import subprocess
    import tempfile

    from PIL import Image

    ffmpeg = shutil.which("ffmpeg")
    ffprobe = shutil.which("ffprobe")
    if not ffmpeg or not ffprobe:
        return None

    # Get duration via ffprobe
    try:
        result = subprocess.run(
            [ffprobe, "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", src_path],
            capture_output=True, text=True, timeout=10,
        )
        duration = float(result.stdout.strip())
    except Exception:
        duration = 1.0

    # Extract 4 frames at 12.5%, 37.5%, 62.5%, 87.5% of total duration
    offsets = [duration * f for f in (0.125, 0.375, 0.625, 0.875)]
    frames: list[Image.Image] = []
    with tempfile.TemporaryDirectory(prefix="pinky_gif_") as tmpdir:
        for i, t in enumerate(offsets):
            out = os.path.join(tmpdir, f"frame_{i}.jpg")
            try:
                subprocess.run(
                    [ffmpeg, "-ss", str(t), "-i", src_path, "-vframes", "1",
                     "-q:v", "2", out, "-y"],
                    capture_output=True, timeout=15,
                )
                if os.path.exists(out):
                    frames.append(Image.open(out).convert("RGB"))
            except Exception:
                pass

        if len(frames) < 2:
            return None

        # Pad to exactly 4 frames (duplicate last if needed)
        while len(frames) < 4:
            frames.append(frames[-1].copy())

        # Resize all frames to the same size (smallest common size)
        w = min(f.width for f in frames)
        h = min(f.height for f in frames)
        # Cap at 640px wide per frame so composite isn't huge
        max_w = 640
        if w > max_w:
            scale = max_w / w
            w, h = int(w * scale), int(h * scale)
        frames = [f.resize((w, h), Image.LANCZOS) for f in frames]

        # Composite into 2×2 grid
        grid = Image.new("RGB", (w * 2, h * 2))
        grid.paste(frames[0], (0, 0))
        grid.paste(frames[1], (w, 0))
        grid.paste(frames[2], (0, h))
        grid.paste(frames[3], (w, h))

        # Save next to the original
        preview_path = os.path.splitext(src_path)[0] + "_preview.jpg"
        grid.save(preview_path, "JPEG", quality=85)
        return preview_path


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
        stop_callback=None,  # async fn(agent_name) → force-stop agent
        stop_all_callback=None,  # async fn() → force-stop all agents
        activity_store=None,  # ActivityStore — for logging message events
    ) -> None:
        self._registry = registry
        self._sessions = session_manager
        self._send_callback = send_callback
        self._reaction_callback = reaction_callback
        self._typing_callback = typing_callback
        self._activity = activity_store
        self._stop_callback = stop_callback
        self._stop_all_callback = stop_all_callback
        self._stats = {"routed": 0, "pending": 0, "denied": 0, "errors": 0, "deduped": 0}

        # Outbound dedupe — suppress accidental duplicate sends. The usual
        # trigger (issue #113): a slow platform leg makes the messaging tool
        # bail on its own timeout *while the original delivery is still in
        # flight*, so the agent retries and the user receives the message
        # twice. We reserve a key per (agent, platform, chat, reply_to,
        # content) on the way in; an identical send within the window is
        # suppressed and the original delivery result is returned instead.
        # Window <= 0 disables the guard. Tunable via PINKY_SEND_DEDUPE_WINDOW.
        # Default 45s: comfortably covers the 30s tool-timeout retry while
        # keeping the blast radius on legitimate identical sends (e.g. a second
        # "ok") small.
        try:
            self._dedupe_window = float(os.environ.get("PINKY_SEND_DEDUPE_WINDOW", "45"))
        except (TypeError, ValueError):
            self._dedupe_window = 45.0
        self._recent_sends: dict[tuple, dict] = {}

        # Optional callback to ensure (start/resume) a streaming session on
        # demand. Wired by api.py after `_ensure_streaming_session` is
        # defined; left None in tests that don't drive the full daemon.
        # Signature: async fn(agent_name: str, *, label: str) -> StreamingSession | None
        # See `set_ensure_session_callback` and `_route_streaming` for the
        # cold-wake path that uses this.
        self._ensure_session_callback = None

        # Optional #149 isolation-mode respawn guard. Wired by api.py.
        # Signature: ``fn(agent_name) -> (status:int, detail:str) | None``.
        # A non-None return means the agent's isolation_mode has no runnable
        # provisioner, so idle auto-wake must NOT relaunch its transport under
        # the daemon uid. None = no guard wired (tests). (Murzik #642 P1.)
        self._isolation_guard = None

        # Streaming sessions — persistent ClaudeSDKClient connections per agent
        # agent_name -> {label -> StreamingSession}
        self._streaming: dict[str, dict[str, object]] = {}

        # Track voice-pending chats: (agent_name, chat_id) -> True when last inbound was voice
        self._voice_pending: dict[tuple[str, str], bool] = {}
        self._message_contexts: dict[tuple[str, str], MessageContext] = {}
        self._message_context_order: dict[str, list[str]] = {}

        # Active typing indicator tasks: (agent_name, chat_id) -> asyncio.Task
        self._typing_tasks: dict[tuple[str, str], asyncio.Task] = {}

        # Strong refs to background cleanup tasks (e.g. disconnecting a
        # displaced streaming session) so the GC can't collect them mid-flight.
        self._background_tasks: set[asyncio.Task] = set()

    @property
    def send_callback(self):
        """Expose the send callback for direct use by scheduler etc."""
        return self._send_callback

    def set_ensure_session_callback(self, callback) -> None:
        """Register the on-demand streaming-session ensurer.

        Used by ``_route_streaming`` to cold-start a session when an
        inbound message arrives for an agent whose session was never
        created (sibling boot policy) or was disconnected after auto-sleep.

        Wired post-construction because ``_ensure_streaming_session`` is
        defined inside ``api.create_app`` and isn't available when the
        broker is built (api.py:1291 vs api.py:2106).

        Signature: ``async fn(agent_name, *, label) -> StreamingSession | None``.
        """
        self._ensure_session_callback = callback

    def set_isolation_guard(self, callback) -> None:
        """Register the #149 isolation-mode respawn guard.

        ``callback(agent_name) -> (status, detail) | None``. Consulted before
        idle auto-wake relaunches a transport via ``connect()``; a non-None
        return means the agent's ``isolation_mode`` isn't runnable yet (no
        implemented provisioner), so the wake is skipped — logged, not raised,
        since the broker has no HTTP context. Cold-start through the ensure
        callback is guarded separately on the api side. (Murzik #642 P1.)
        """
        self._isolation_guard = callback

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
        """Stop the native typing indicator loop.

        Safe to call defensively — no-op (and silent) when no task is active.
        """
        key = (agent_name, chat_id)
        task = self._typing_tasks.pop(key, None)
        if task and not task.done():
            task.cancel()
            _log(f"broker: typing indicator stopped for {agent_name}/{chat_id}")

    def _stop_all_typing(self, agent_name: str) -> None:
        """Stop ALL typing indicator loops for an agent (used on disconnect/stop)."""
        keys = [k for k in self._typing_tasks if k[0] == agent_name]
        for key in keys:
            task = self._typing_tasks.pop(key, None)
            if task and not task.done():
                task.cancel()
        if keys:
            _log(f"broker: stopped {len(keys)} typing indicator(s) for {agent_name}")

    # ------------------------------------------------------------------
    # Outbound dedupe (issue #113)
    # ------------------------------------------------------------------
    @staticmethod
    def _dedupe_key(
        agent_name: str,
        platform: str,
        chat_id: str,
        content: str,
        reply_to: str = "",
    ) -> tuple:
        return (agent_name, platform, str(chat_id), reply_to or "", content)

    def _prune_recent_sends(self, now: float | None = None) -> None:
        if now is None:
            now = time.monotonic()
        window = self._dedupe_window
        expired = [k for k, v in self._recent_sends.items() if now - v["ts"] > window]
        for k in expired:
            del self._recent_sends[k]

    def register_outbound(
        self,
        agent_name: str,
        platform: str,
        chat_id: str,
        content: str,
        *,
        reply_to: str = "",
    ) -> dict | None:
        """Reserve an outbound send for dedupe.

        Returns ``None`` when this is a fresh send — the caller should
        proceed to deliver and then call :meth:`finalize_outbound` (or
        :meth:`clear_outbound` on failure). Returns a delivery-result dict
        when an identical send is already in flight or completed within the
        dedupe window — in that case the caller MUST skip delivery and return
        the dict as-is (it carries ``"deduped": True``).
        """
        if self._dedupe_window <= 0:
            return None
        now = time.monotonic()
        self._prune_recent_sends(now)
        key = self._dedupe_key(agent_name, platform, chat_id, content, reply_to)
        existing = self._recent_sends.get(key)
        if existing is not None:
            self._stats["deduped"] = self._stats.get("deduped", 0) + 1
            _log(
                f"broker: suppressed duplicate send for "
                f"{agent_name} -> {platform}:{chat_id}"
            )
            result = existing.get("result")
            if result is not None:
                # Original already delivered — hand back its message_id so the
                # retrying caller sees a clean, idempotent success.
                return {**result, "deduped": True}
            # Original still in flight — report success so the caller stops
            # retrying. The real message_id lands on the first call's return.
            return {
                "sent": True,
                "deduped": True,
                "agent": agent_name,
                "platform": platform,
                "chat_id": chat_id,
                "message_id": None,
            }
        self._recent_sends[key] = {"ts": now, "result": None}
        return None

    def finalize_outbound(
        self,
        agent_name: str,
        platform: str,
        chat_id: str,
        content: str,
        result: dict,
        *,
        reply_to: str = "",
    ) -> None:
        """Attach the delivery result to a reserved outbound entry so a later
        duplicate within the window can return the original message_id."""
        if self._dedupe_window <= 0:
            return
        key = self._dedupe_key(agent_name, platform, chat_id, content, reply_to)
        entry = self._recent_sends.get(key)
        if entry is not None:
            entry["result"] = result

    def clear_outbound(
        self,
        agent_name: str,
        platform: str,
        chat_id: str,
        content: str,
        *,
        reply_to: str = "",
    ) -> None:
        """Release a reserved outbound entry — call when delivery fails so a
        legitimate retry is not mistaken for a duplicate and silently dropped."""
        key = self._dedupe_key(agent_name, platform, chat_id, content, reply_to)
        self._recent_sends.pop(key, None)

    async def deliver_deduped(
        self,
        agent_name: str,
        platform: str,
        chat_id: str,
        content: str,
        deliver,
        *,
        reply_to: str = "",
    ) -> dict:
        """Run ``deliver`` at most once per dedupe window.

        ``deliver`` is a zero-arg callable returning an awaitable that performs
        the actual platform send and resolves to a result dict. Identical sends
        within the window are suppressed and the original result is returned
        (carrying ``"deduped": True``).

        Failure handling is deliberate (issue #113):

        * A delivery *failure* (``Exception``) releases the reservation so a
          genuine retry is not mistaken for a duplicate and silently dropped.
        * ``asyncio.CancelledError`` (a ``BaseException``, not an ``Exception``)
          is allowed to propagate **without** releasing the reservation: the
          underlying ``run_in_executor`` worker cannot be cancelled, so the
          platform delivery may still be in flight, and clearing the
          reservation would reopen the duplicate window this guard closes.
        """
        dup = self.register_outbound(
            agent_name, platform, chat_id, content, reply_to=reply_to
        )
        if dup is not None:
            return dup
        try:
            result = await deliver()
        except Exception:
            self.clear_outbound(
                agent_name, platform, chat_id, content, reply_to=reply_to
            )
            raise
        self.finalize_outbound(
            agent_name, platform, chat_id, content, result, reply_to=reply_to
        )
        return result

    async def _send_message(self, agent_name: str, platform: str, chat_id: str, content: str) -> None:
        """Send a message if the outbound callback is configured."""
        if self._send_callback:
            await self._send_callback(agent_name, platform, chat_id, content)
            if self._activity:
                try:
                    preview = (content or "")[:80]
                    if len(content or "") > 80:
                        preview += "..."
                    self._activity.log(
                        agent_name, "message_sent",
                        f"{agent_name} sent a message on {platform}",
                        description=preview,
                    )
                except Exception:
                    pass

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

    async def _handle_stop_command(self, message: BrokerMessage) -> bool:
        """Intercept /stop commands from the owner. Returns True if handled."""
        # Only the primary user can issue stop commands
        primary = self._registry.get_primary_user()
        sender_id = message.sender_id or message.chat_id
        if not primary.get("chat_id") or sender_id != primary["chat_id"]:
            return False

        text = message.content.strip()
        parts = text.split(maxsplit=1)
        cmd = parts[0].lower()

        if cmd != "/stop":
            return False

        arg = parts[1].strip().lower() if len(parts) > 1 else ""

        if arg == "all":
            if self._stop_all_callback:
                result = await self._stop_all_callback()
                reply = f"Stopped all agents: {result.get('total_agents', 0)} agent(s) killed."
            else:
                reply = "Stop-all not configured."
        elif arg:
            target = arg
            if self._stop_callback:
                try:
                    result = await self._stop_callback(target)
                    closed = result.get("sessions_closed", 0)
                    reply = f"Stopped {target}: {closed} session(s) closed."
                except Exception as e:
                    reply = f"Failed to stop {target}: {e}"
            else:
                reply = "Stop not configured."
        else:
            # /stop with no args — stop the agent this message is routed to
            if self._stop_callback:
                try:
                    result = await self._stop_callback(message.agent_name)
                    closed = result.get("sessions_closed", 0)
                    reply = f"Stopped {message.agent_name}: {closed} session(s) closed."
                except Exception as e:
                    reply = f"Failed to stop {message.agent_name}: {e}"
            else:
                reply = "Stop not configured."

        if self._send_callback:
            await self._send_callback(
                message.agent_name, message.platform, message.chat_id, reply,
            )
        return True

    async def _handle_voice_approval_command(self, message: BrokerMessage) -> bool:
        """Intercept /approve_voice_<id> and /deny_voice_<id> from the owner."""
        primary = self._registry.get_primary_user()
        sender_id = message.sender_id or message.chat_id
        if not primary.get("chat_id") or sender_id != primary["chat_id"]:
            return False

        text = message.content.strip()
        cmd = text.split()[0].lower()

        if cmd.startswith("/approve_voice_"):
            request_id = cmd[len("/approve_voice_"):]
            action = "approve"
        elif cmd.startswith("/deny_voice_"):
            request_id = cmd[len("/deny_voice_"):]
            action = "deny"
        else:
            return False

        if not request_id:
            return False

        # Import voice store lazily to avoid circular deps
        try:
            import time as _time

            from pinky_daemon.voice_store import VoiceStore

            store = VoiceStore(db_path="data/voice_calls.db")
            req = store.get_call_request(request_id)
            if not req:
                reply = f"Voice call request {request_id[:8]}... not found."
            elif action == "approve":
                if req.approval_state == "approved":
                    reply = f"✅ Already approved: {req.target_name}"
                elif req.expires_at and _time.time() > req.expires_at:
                    store.update_call_request_state(request_id, approval_state="expired")
                    reply = f"⏰ Request expired: {req.target_name}"
                else:
                    store.update_call_request_state(
                        request_id,
                        approval_state="approved",
                        authorized_by="owner",
                        authorized_at=_time.time(),
                    )
                    # Trigger the dial
                    dial_info = ""
                    try:
                        import os

                        from pinky_daemon.voice_engine import dial_approved_call

                        base_url = (
                            self._registry.get_setting("PINKY_BASE_URL")
                            or os.environ.get("PINKY_BASE_URL", "")
                        )
                        if base_url:
                            updated_req = store.get_call_request(request_id)
                            result = await dial_approved_call(
                                updated_req, store, self._registry,
                                base_url, self._send_callback,
                            )
                            if result.get("call_sid"):
                                dial_info = f"\n📲 Dialing... (SID: {result['call_sid'][:12]}...)"
                            elif result.get("error"):
                                dial_info = f"\n⚠️ Dial failed: {result['error']}"
                        else:
                            dial_info = "\n⚠️ PINKY_BASE_URL not set — cannot dial"
                    except Exception as dial_err:
                        dial_info = f"\n⚠️ Dial error: {dial_err}"

                    reply = (
                        f"✅ Approved call to {req.target_name} ({req.target_phone})\n"
                        f"Goal: {req.goal}{dial_info}"
                    )
            else:
                if req.approval_state == "rejected":
                    reply = f"🚫 Already denied: {req.target_name}"
                elif req.approval_state == "approved":
                    reply = f"⚠️ Cannot deny — already approved: {req.target_name}"
                else:
                    store.update_call_request_state(
                        request_id, approval_state="rejected"
                    )
                    reply = f"🚫 Denied call to {req.target_name}"

        except ImportError:
            reply = "Voice module not available."

        if self._send_callback:
            await self._send_callback(
                message.agent_name, message.platform, message.chat_id, reply,
            )
        return True

    async def _handle_approval_command(self, message: BrokerMessage) -> bool:
        """Intercept /approve_<id> and /deny_<id> commands from the owner."""
        primary = self._registry.get_primary_user()
        sender_id = message.sender_id or message.chat_id
        if not primary.get("chat_id") or sender_id != primary["chat_id"]:
            return False

        text = message.content.strip()
        cmd = text.split()[0].lower()

        if cmd.startswith("/approve_"):
            target_chat_id = cmd[len("/approve_"):]
            action = "approve"
        elif cmd.startswith("/deny_"):
            target_chat_id = cmd[len("/deny_"):]
            action = "deny"
        else:
            return False

        if not target_chat_id:
            return False

        agent_name = message.agent_name

        if action == "approve":
            status = self._registry.get_user_status(agent_name, target_chat_id)
            if status == "approved":
                reply = f"User {target_chat_id} is already approved."
            else:
                display_name = self._registry.get_user_display_name(
                    agent_name, target_chat_id,
                )
                self._registry.approve_user(
                    agent_name, target_chat_id,
                    display_name=display_name,
                    approved_by="primary_user",
                )
                delivered = await self.handle_approval(agent_name, target_chat_id)
                reply = f"✅ Approved. {delivered} pending message(s) delivered to {agent_name}."

                # Notify the approved user
                if self._send_callback:
                    try:
                        await self._send_callback(
                            agent_name, message.platform, target_chat_id,
                            "You've been approved! Your messages are now being delivered.",
                        )
                    except Exception as e:
                        _log(f"broker: failed to notify approved user {target_chat_id}: {e}")
        else:
            self._registry.deny_user(agent_name, target_chat_id)
            reply = f"🚫 User {target_chat_id} denied."

        if self._send_callback:
            await self._send_callback(
                message.agent_name, message.platform, message.chat_id, reply,
            )
        return True

    async def handle_inbound(self, message: BrokerMessage) -> None:
        """Handle an incoming platform message. Non-blocking."""
        agent_name = message.agent_name

        # 0. Intercept /stop command from owner
        if message.content.strip().startswith("/stop"):
            handled = await self._handle_stop_command(message)
            if handled:
                return

        # 0b. Intercept /approve_voice_ and /deny_voice_ from owner
        text_lower = message.content.strip().lower()
        if text_lower.startswith("/approve_voice_") or text_lower.startswith("/deny_voice_"):
            handled = await self._handle_voice_approval_command(message)
            if handled:
                return

        # 0c. Intercept /approve_<id> and /deny_<id> from owner (user approval)
        if text_lower.startswith("/approve_") or text_lower.startswith("/deny_"):
            handled = await self._handle_approval_command(message)
            if handled:
                return

        # 1. Check sender status — always use the individual sender_id for approval,
        #    not the chat_id (which is the group ID for group messages).
        user_id = message.sender_id or message.chat_id
        status = self._registry.get_user_status(agent_name, user_id)

        if status == "denied":
            self._stats["denied"] += 1
            _log(f"broker: denied message from {message.sender_name} ({user_id}) for {agent_name}")
            return

        if status is None or status == "pending":
            primary = self._registry.get_primary_user()
            # First-run ownership claim: if no primary user has ever been
            # configured, the first person to message the bot is treated as the
            # owner — set them as primary (which also auto-approves them across
            # all agents). This removes the confusing "waiting for approval"
            # message a fresh owner would otherwise get from their own bot.
            # Tradeoff: whoever messages first claims ownership, so the owner
            # should connect before sharing the bot. Only fires when primary is
            # completely unset.
            if not primary.get("chat_id"):
                self._registry.set_primary_user(
                    user_id,
                    display_name=message.sender_name or "",
                )
                _log(
                    f"broker: no primary user configured — claimed {user_id} "
                    f"({message.sender_name}) as primary/owner for {agent_name}"
                )
                primary = self._registry.get_primary_user()
            # Auto-approve primary user
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
                    # Onboarding: notify new user and primary user
                    if self._send_callback:
                        try:
                            await self._send_callback(
                                agent_name, message.platform, message.chat_id,
                                "Request sent! Waiting for approval.",
                            )
                        except Exception as e:
                            _log(f"broker: failed to send onboarding reply to {user_id}: {e}")
                        primary = self._registry.get_primary_user()
                        if primary.get("chat_id"):
                            name_display = message.sender_name or "Unknown"
                            username = message.metadata.get("username", "")
                            if username:
                                name_display += f" (@{username})"
                            notification = (
                                f"🆕 New user wants to talk to {agent_name}:\n"
                                f"{name_display} (ID: {user_id})\n\n"
                                f"/approve_{user_id}\n"
                                f"/deny_{user_id}"
                            )
                            try:
                                await self._send_callback(
                                    agent_name, message.platform, primary["chat_id"],
                                    notification,
                                )
                            except Exception as e:
                                _log(f"broker: failed to notify owner about new user {user_id}: {e}")
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

        # 3. Log to activity feed
        if self._activity:
            try:
                sender = message.sender_name or message.chat_id
                preview = (message.content or "")[:80]
                if len(message.content or "") > 80:
                    preview += "..."
                self._activity.log(
                    agent_name, "message_received",
                    f"Message from {sender} on {message.platform}",
                    description=preview,
                )
            except Exception:
                pass

    async def dispatch_pre_authorized(
        self, agent_name: str, message: BrokerMessage,
    ) -> None:
        """Dispatch a message whose sender is already authorized upstream.

        Bypasses the human-platform onboarding flow that ``handle_inbound``
        runs (``get_user_status`` → ``add_pending_user`` → ``/approve_…``
        Telegram prompt to the owner). Intended for callers that enforce
        their own identity gating before invoking the broker — currently
        the ferry host-callback (``host_pinky``), where peer-fleet ACL has
        already been enforced. Future pre-authorized channels (federation,
        MCP-host inbound) should land on this same primitive rather than
        reusing ``handle_inbound``.

        Concretely: routes the message to the agent's streaming session
        without consulting ``approved_users``. The agent will see the
        message in its prompt feed exactly as if the broker had approved
        it via the human-platform flow.

        Activity-log emission is intentionally left to the caller — ferry
        inbound has its own observability (host-pinky's stats counters),
        and the broker's ``message_received`` event is shaped for human
        platforms (sender/preview formatting). If a future caller wants
        broker-side activity logs, expose that as a separate flag.
        """
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
        image_types = {"photo", "sticker", "animation"}
        if message.attachments:
            parts = []
            for att in message.attachments:
                att_type = att.get("type", "file")
                file_name = att.get("file_name", "")
                file_id = att.get("file_id", "")
                local_path = att.get("local_path", "")
                original_path = att.get("original_path", "")
                if local_path and original_path:
                    # GIF preview: show composite path and note original
                    parts.append(f"{att_type}: {local_path}")
                    parts.append(f"(4-frame preview of GIF/video — original at {original_path})")
                elif local_path:
                    parts.append(f"{att_type}: {local_path}")
                elif file_name:
                    parts.append(f"{att_type}: {file_name} (file_id: {file_id})")
                else:
                    parts.append(f"{att_type} (file_id: {file_id})")
            body += f"\n\U0001F4CE Attachments: {', '.join(parts)}"
            has_images = any(
                a.get("local_path") and a.get("type") in image_types
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
            # Return the mapped session in ANY state so _route_streaming's
            # auto-wake / wait-for-reconnect logic operates on the assigned
            # session instead of leaking the message into 'main'.
            if session is not None:
                return session
        # Fall back to main
        return sessions.get("main")

    async def _route_streaming(self, agent_name: str, message: BrokerMessage) -> None:
        """Route a message via streaming session — non-blocking.

        Resolution order:

        1. Existing session for this channel/label is connected → use it.
        2. Existing session is disconnected with a persisted session_id →
           reconnect in-place (idle-sleep wake).
        3. No session object yet OR reconnect failed → cold-start a fresh
           session via ``_ensure_session_callback`` (sibling boot policy:
           non-main agents have no session at startup; see api.py boot
           policy comment at the streaming-session startup loop).
        4. Still nothing usable → notify the user that the agent isn't
           running.

        Step 3 closes the gap where inbound platform messages (Telegram,
        Discord, etc.) used to fall straight to step 4 for any sibling
        agent that had never been touched via the web admin, even though
        the web admin's ``/agents/{name}/chat`` endpoint always cold-starts
        via ``_ensure_streaming_session``. See bradbrok/PinkyBot fix branch
        ``fix/inbound-msg-cold-wake``.
        """
        streaming = self._get_streaming_session(agent_name, message.chat_id)

        # Auto-wake: deliberate idle-sleep with a retained resume_handle can be
        # woken in-line by calling connect(). Per @murzik on PR #492 review,
        # we must NOT route RECONNECTING through connect() here — that would
        # race the in-flight reconnect (force_restart or attempt_reconnect)
        # and produce a double-connect. RECONNECTING falls through to the
        # wait-for-reconnect block below instead. DEAD also falls through:
        # resurrection is the scheduler/watchdog's job, not the broker's.
        if (
            streaming
            and streaming.state == SessionState.IDLE_SLEEPING
            and streaming.resume_handle
        ):
            # #149 P1: don't relaunch the transport for an agent whose
            # isolation_mode has no runnable provisioner (e.g. a local session
            # later relabeled unix_user) — that would wake it under the daemon
            # uid with none of the requested OS isolation. Skip + log.
            blocked = self._isolation_guard(agent_name) if self._isolation_guard else None
            if blocked:
                _log(f"broker: {agent_name} auto-wake blocked — {blocked[1]}")
                streaming = None
            else:
                _log(f"broker: {agent_name} is idle-sleeping — auto-waking for inbound message")
                try:
                    await streaming.connect()
                    _log(f"broker: {agent_name} auto-woke successfully")
                except Exception as e:
                    _log(f"broker: {agent_name} auto-wake failed: {e}")
                    streaming = None

        # Cold-start path (PR #460): no session object yet → use the on-demand
        # ensurer if wired (api.py registers it post-init). This matches the
        # web admin chat path so inbound platform messages don't fall through
        # to "not running" just because the sibling auto-start was skipped at
        # boot. Only runs when there is no streaming object at all —
        # in-flight-reconnect cases (streaming exists but state != CONNECTED)
        # fall through to the wait-for-reconnect block below; cold-starting
        # over them would race the in-flight reconnect.
        if streaming is None and self._ensure_session_callback:
            label = "main"
            try:
                if message.chat_id:
                    mapped = self._registry.get_channel_session(agent_name, message.chat_id)
                    if mapped:
                        label = mapped
                _log(f"broker: {agent_name} has no live session — cold-starting via ensurer (label={label})")
                streaming = await self._ensure_session_callback(agent_name, label=label)
                if streaming and streaming.state == SessionState.CONNECTED:
                    _log(f"broker: {agent_name} cold-started successfully")
            except Exception as e:
                _log(f"broker: {agent_name} cold-start failed: {e}")
                streaming = None

        # Wait-for-reconnect: if the session object still exists but isn't
        # CONNECTED, an in-flight reconnect or context_restart is most likely
        # the cause (RECONNECTING state). disconnect()→connect() runs in a
        # separate task and briefly leaves state != CONNECTED, with
        # ``resume_handle`` possibly wiped so the auto-wake branch above
        # cannot help. Poll for a bounded window before falling back to the
        # user-visible "not running" error so the message gets delivered as
        # soon as the new session comes up instead of being dropped.
        # See _INBOUND_RECONNECT_WAIT_SEC.
        if streaming is not None and streaming.state != SessionState.CONNECTED:
            _log(
                f"broker: {agent_name} session present but disconnected — "
                f"waiting up to {_INBOUND_RECONNECT_WAIT_SEC:g}s for reconnect"
            )
            deadline = time.monotonic() + _INBOUND_RECONNECT_WAIT_SEC
            while time.monotonic() < deadline:
                await asyncio.sleep(_INBOUND_RECONNECT_POLL_SEC)
                if streaming.state == SessionState.CONNECTED:
                    _log(f"broker: {agent_name} reconnect completed — resuming delivery")
                    break

        if not streaming or streaming.state != SessionState.CONNECTED:
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
                _log(f"broker: voice transcription failed for {agent_name}, sending fallback"
                     f" | attachments={message.attachments}")
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
        # Build reply hint for external platforms (agent-only, not stored in chat history)
        hint = ""
        _no_hint_platforms = {"web", "api", ""}
        if message.platform and message.platform not in _no_hint_platforms:
            hint = (
                f"\n💬 Reply on {message.platform} using pinky-messaging: "
                f'send(chat_id="{message.chat_id}", platform="{message.platform}", text=...)'
            )
            if message.message_id:
                hint += (
                    f' — or thread(message_id="{message.message_id}", text=...) '
                    f"to quote-reply to this message"
                )
        await streaming.send(
            prompt,
            platform=message.platform,
            chat_id=message.chat_id,
            message_id=message.message_id,
            agent_hint=hint,
        )
        # Server-side presence: successful inbound delivery = agent pipe is working
        try:
            self._registry.stamp_last_seen(agent_name)
        except Exception as e:
            _log(f"broker: stamp_last_seen failed for {agent_name}: {e}")
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
        if not streaming or streaming.state != SessionState.CONNECTED:
            _log(f"broker: can't deliver agent message to {to_agent} — not connected")
            return False

        from datetime import datetime
        from datetime import timezone as tz
        ts = datetime.now(tz.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        prompt = f"[agent | {from_agent} | internal | {ts}]\n{message}"
        await streaming.send(prompt)
        # Server-side presence: successful delivery = agent is reachable
        try:
            self._registry.stamp_last_seen(to_agent)
        except Exception as e:
            _log(f"broker: stamp_last_seen failed for {to_agent}: {e}")
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
                # download_file is sync (blocking httpx GET) -- offload so a
                # multi-MB download doesn't freeze the daemon event loop.
                local_path = await asyncio.to_thread(
                    adapter.download_file, att["file_id"], dest_dir=dest_dir,
                )
                local_path = os.path.abspath(local_path)
                att["local_path"] = local_path
                _log(f"broker: downloaded {att['type']} for {agent_name}: {local_path}")
                # For GIFs and animations, generate a 4-quadrant preview image
                _is_anim_ext = local_path.lower().endswith((".gif", ".mp4", ".webm", ".mov"))
                if att.get("type") in {"animation", "video"} or _is_anim_ext:
                    try:
                        # ffprobe/ffmpeg subprocesses + PIL resize -- offload too
                        preview = await asyncio.to_thread(_make_gif_preview, local_path)
                        if preview:
                            att["local_path"] = preview
                            att["original_path"] = local_path
                            _log(f"broker: gif preview generated: {preview}")
                    except Exception as pe:
                        _log(f"broker: gif preview failed: {pe}")
            except Exception as e:
                _log(f"broker: failed to download {att['type']} for {agent_name}: {e}")

    async def _transcribe_voice(self, agent_name: str, message: BrokerMessage) -> str:
        """Download and transcribe a voice attachment. Returns transcript or empty string."""
        _log(f"broker: _transcribe_voice called for {agent_name}")
        agent = self._registry.get(agent_name)
        if not agent:
            _log(f"broker: voice transcribe — agent {agent_name} not found in registry")
            return ""

        voice_cfg = agent.voice_config or {}
        provider = voice_cfg.get("transcribe_provider", "openai")
        _log(f"broker: voice transcribe — provider={provider}, voice_cfg={voice_cfg}")

        # Find the voice attachment
        voice_att = next(
            (a for a in (message.attachments or []) if a.get("type") == "voice"),
            None,
        )
        if not voice_att or not voice_att.get("file_id"):
            _log(f"broker: no voice attachment found | attachments={message.attachments}")
            return ""

        # Download the voice file via Telegram adapter
        file_id = voice_att["file_id"]
        _log(f"broker: voice file_id={file_id}")
        try:
            # Use the send_callback's adapter to download
            from pinky_outreach.telegram import TelegramAdapter
            # Get the bot token for this agent
            raw_token = self._registry.get_raw_token(agent_name, "telegram")
            if not raw_token:
                _log(f"broker: no telegram token for {agent_name}, can't download voice")
                return ""
            adapter = TelegramAdapter(raw_token)
            # Sync download -- offload so it doesn't block the event loop
            local_path = await asyncio.to_thread(
                adapter.download_file, file_id, dest_dir=tempfile.mkdtemp(prefix="pinky_voice_"),
            )
            file_size = os.path.getsize(local_path) if os.path.exists(local_path) else -1
            _log(f"broker: downloaded voice file for {agent_name}: {local_path} ({file_size} bytes)")
            if file_size <= 0:
                _log(f"broker: voice file is empty or missing: {local_path}")
                return ""
        except Exception as e:
            _log(f"broker: failed to download voice for {agent_name}: {type(e).__name__}: {e}")
            return ""

        # Transcribe
        try:
            # whisper_local needs no API key — skip key lookup for it
            if provider == "whisper_local":
                api_key = ""
            else:
                api_key = self._registry.get_setting(f"{provider.upper()}_API_KEY") or os.environ.get(f"{provider.upper()}_API_KEY", "")
                if not api_key and provider == "openai":
                    api_key = self._registry.get_setting("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY", "")
                if not api_key and provider == "deepgram":
                    api_key = self._registry.get_setting("DEEPGRAM_API_KEY") or os.environ.get("DEEPGRAM_API_KEY", "")
                if not api_key and provider == "yandex":
                    api_key = self._registry.get_setting("YANDEX_API_KEY") or os.environ.get("YANDEX_API_KEY", "")

                if not api_key:
                    _log(f"broker: no API key for {provider} transcription (checked DB + env)")
                    return ""
                _log(f"broker: got API key for {provider} ({len(api_key)} chars)")

            if provider == "openai":
                import httpx
                stt_model = voice_cfg.get("openai_stt_model", "gpt-4o-transcribe")
                # Telegram sends .oga files; normalize filename to .ogg for OpenAI compat
                upload_name = os.path.basename(local_path)
                if upload_name.endswith(".oga"):
                    upload_name = upload_name[:-4] + ".ogg"
                async with httpx.AsyncClient(timeout=60) as client:
                    with open(local_path, "rb") as f:
                        resp = await client.post(
                            "https://api.openai.com/v1/audio/transcriptions",
                            headers={"Authorization": f"Bearer {api_key}"},
                            files={"file": (upload_name, f, "audio/ogg")},
                            data={"model": stt_model},
                        )
                    # Fallback: if gpt-4o-transcribe rejects the format, retry with whisper-1
                    if resp.status_code == 400 and stt_model != "whisper-1":
                        _log(f"broker: {stt_model} returned 400, retrying with whisper-1: "
                             f"{resp.text[:200]}")
                        with open(local_path, "rb") as f:
                            resp = await client.post(
                                "https://api.openai.com/v1/audio/transcriptions",
                                headers={"Authorization": f"Bearer {api_key}"},
                                files={"file": (upload_name, f, "audio/ogg")},
                                data={"model": "whisper-1"},
                            )
                    _log(f"broker: openai response status={resp.status_code}")
                    if resp.status_code >= 400:
                        _log(f"broker: openai transcription error body: {resp.text[:500]}")
                    resp.raise_for_status()
                    transcript = resp.json().get("text", "")
                    _log(f"broker: openai transcribed with {stt_model}: {len(transcript)} chars")

            elif provider == "deepgram":
                import httpx
                with open(local_path, "rb") as f:
                    audio_data = f.read()
                async with httpx.AsyncClient(timeout=60) as client:
                    resp = await client.post(
                        "https://api.deepgram.com/v1/listen",
                        params={
                            "model": "nova-3",
                            "smart_format": "true",
                            "detect_language": "true",
                        },
                        headers={
                            "Authorization": f"Token {api_key}",
                            "Content-Type": "audio/ogg",
                        },
                        content=audio_data,
                    )
                resp.raise_for_status()
                result = resp.json()
                transcript = (
                    result.get("results", {})
                    .get("channels", [{}])[0]
                    .get("alternatives", [{}])[0]
                    .get("transcript", "")
                )
                detected_lang = (
                    result.get("results", {})
                    .get("channels", [{}])[0]
                    .get("detected_language", "")
                )
                if detected_lang:
                    _log(f"broker: deepgram detected language: {detected_lang}")

            elif provider == "whisper_local":
                from faster_whisper import WhisperModel
                model_size = voice_cfg.get("whisper_model", "base")
                lang = voice_cfg.get("whisper_lang", None)  # None = auto-detect
                _log(f"broker: whisper_local transcribing with model={model_size}")
                # Run in executor to avoid blocking the event loop.
                # Set HF_HUB_OFFLINE=1 to skip network version checks (uses cached model).
                def _run_whisper() -> str:
                    import os as _os
                    _os.environ["HF_HUB_OFFLINE"] = "1"
                    model = WhisperModel(model_size, device="cpu", compute_type="int8")
                    segments, _ = model.transcribe(local_path, beam_size=5, language=lang)
                    return " ".join(seg.text.strip() for seg in segments)
                transcript = await asyncio.get_running_loop().run_in_executor(None, _run_whisper)

            elif provider == "yandex":
                import httpx
                folder_id = (
                    voice_cfg.get("yandex_folder_id")
                    or self._registry.get_setting("YANDEX_FOLDER_ID")
                    or os.environ.get("YANDEX_FOLDER_ID", "")
                )
                lang = voice_cfg.get("yandex_lang", "ru-RU")
                with open(local_path, "rb") as f:
                    audio_data = f.read()
                async with httpx.AsyncClient(timeout=30) as client:
                    resp = await client.post(
                        "https://stt.api.cloud.yandex.net/speech/v1/stt:recognize",
                        headers={
                            "Authorization": f"Api-Key {api_key}",
                            "Content-Type": "audio/ogg; codecs=opus",
                        },
                        params={"folderId": folder_id, "lang": lang},
                        content=audio_data,
                    )
                resp.raise_for_status()
                transcript = resp.json().get("result", "")

            else:
                _log(f"broker: unknown transcription provider: {provider}")
                return ""

            _log(f"broker: transcribed voice for {agent_name} ({provider}): {transcript[:80]}...")
            return transcript
        except Exception as e:
            import traceback
            _log(f"broker: transcription failed for {agent_name}: {type(e).__name__}: {e}")
            _log(f"broker: transcription traceback: {traceback.format_exc()}")
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
            base_url = os.environ.get("PINKY_DAEMON_URL", "http://localhost:8888").rstrip("/")
            req = urllib.request.Request(
                f"{base_url}/broker/send-voice",
                data=body,
                method="POST",
                headers={"Content-Type": "application/json"},
            )

            # The endpoint is served by THIS process on the same event loop.
            # A sync urlopen here would block the loop and deadlock against
            # our own request until the socket timeout -- run it in a thread.
            def _post() -> dict:
                with urllib.request.urlopen(req, timeout=60) as resp:
                    return json.loads(resp.read())

            result = await asyncio.to_thread(_post)
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
        lines.append("- **send_gif / send_voice / send_photo / send_document / send_video**: Send rich media (send_video plays inline)")
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
            if any(s.state == SessionState.CONNECTED for s in sessions.values())
        ]

    def register_streaming(self, agent_name: str, session, label: str = "main") -> None:
        """Register a StreamingSession for an agent under a label.

        Defense-in-depth: if a still-connected session is already registered
        under this label, overwriting it would orphan a live SDK subprocess
        that keeps processing messages. Log loudly and schedule a disconnect
        of the displaced session.
        """
        if agent_name not in self._streaming:
            self._streaming[agent_name] = {}
        displaced = self._streaming[agent_name].get(label)
        if (
            displaced is not None
            and displaced is not session
            and getattr(displaced, "state", None) == SessionState.CONNECTED
        ):
            _log(
                f"broker: WARNING overwriting still-connected streaming session "
                f"for {agent_name}/{label} -- scheduling disconnect of displaced session"
            )
            try:
                task = asyncio.get_running_loop().create_task(
                    self._disconnect_displaced(agent_name, label, displaced)
                )
                self._background_tasks.add(task)
                task.add_done_callback(self._background_tasks.discard)
            except RuntimeError:
                _log(
                    f"broker: no running event loop -- displaced session for "
                    f"{agent_name}/{label} left connected"
                )
        self._streaming[agent_name][label] = session
        _log(f"broker: registered streaming session for {agent_name}/{label}")

    async def _disconnect_displaced(self, agent_name: str, label: str, displaced) -> None:
        try:
            await displaced.disconnect()
            _log(f"broker: displaced streaming session for {agent_name}/{label} disconnected")
        except Exception as e:
            _log(
                f"broker: failed to disconnect displaced session for "
                f"{agent_name}/{label}: {e}"
            )

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
        # Clean up typing indicators only when no sessions remain for this agent
        remaining = self._streaming.get(agent_name, {})
        if not remaining:
            self._stop_all_typing(agent_name)

    def list_streaming_sessions(self, agent_name: str) -> list[dict]:
        """List streaming session labels and status for an agent."""
        sessions = self._streaming.get(agent_name, {})
        return [
            {
                "label": label,
                "connected": s.state == SessionState.CONNECTED,
                "stats": s.stats,
                "session_id": s.resume_handle[:12] if s.resume_handle else "",
            }
            for label, s in sessions.items()
        ]

    def get_streaming_session(self, agent_name: str, label: str = "main"):
        """Return the registered session instance for ``agent_name/label``.

        Returns ``None`` if no session is registered. Used by hook-driven
        endpoints (e.g. ``/agents/<name>/transport/wake``) to reach into
        the Transport-typed session and call backend-specific surfaces
        (``notify_tail``, ``set_transcript_path``). The returned object's
        type is implementation-defined (``StreamingSession`` for SDK,
        ``TmuxSession`` for tmux, ``CodexSession`` for codex) — callers
        must duck-type the method they need and tolerate its absence.
        """
        return self._streaming.get(agent_name, {}).get(label)

    @property
    def stats(self) -> dict:
        stats = dict(self._stats)
        stats["streaming"] = {
            name: {label: s.stats for label, s in sessions.items()}
            for name, sessions in self._streaming.items()
        }
        return stats
