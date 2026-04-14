"""Streaming Session — persistent bidirectional Claude Code connection.

Uses ClaudeSDKClient for non-blocking message delivery. Messages go in
via send(), responses come back via a background reader loop that calls
the response callback.

This is the preferred session type for broker-connected agents where
messages arrive asynchronously from platform users.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import zoneinfo
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

from pinky_daemon.sessions import SessionUsage

# Models with native 1M context (SDK reports 200k incorrectly)
_1M_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6"}

DEFAULT_STREAMING_ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
    "Agent",  # subagent spawning
    "mcp__memory__*",
    "mcp__pinky-memory__*",
    "mcp__pinky-self__*",
    "mcp__pinky-messaging__*",
]


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class StreamingSessionConfig:
    """Configuration for a streaming session."""
    agent_name: str = ""
    label: str = "main"
    model: str = ""
    working_dir: str = "."
    allowed_tools: list[str] = field(default_factory=list)
    disallowed_tools: list[str] = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)
    permission_mode: str = "bypassPermissions"
    max_turns: int = 0
    system_prompt: str = ""
    resume_session_id: str = ""  # SDK session ID to resume from previous run
    wake_context: str = ""  # Saved continuation context to inject on wake
    wake_context_builder: object = None  # Callable(agent_name) -> str; refreshes wake_context on restart
    restart_guard: object = None  # Callable(session) -> dict; blocks restart if persistence is stale
    context_warn_pct: int = 40  # Warn agent to save state at this %
    context_restart_pct: int = 80  # Force restart at this %
    restart_guard_cooldown_sec: int = 60  # Minimum gap between restart-block warnings
    idle_timeout: int = 3600  # Auto-sleep after this many seconds idle (0 = disabled)
    timezone: str = "America/Los_Angeles"  # IANA timezone for wake timestamp
    subagents: dict = field(default_factory=dict)  # name -> AgentDefinition
    provider_url: str = ""   # ANTHROPIC_BASE_URL override (e.g. "http://localhost:11434" for Ollama)
    provider_key: str = ""   # ANTHROPIC_API_KEY override (empty = use env var)
    thinking_effort: str = "medium"  # low, medium, high, max — default thinking depth
    restart_reason: str = ""  # "context_restart", "auto_restart", etc. — cleared after wake prompt


@dataclass
class StreamingTurnResult:
    """Completed assistant turn details for broker/API handling."""

    agent_name: str
    session_id: str
    platform: str = ""
    chat_id: str = ""
    message_id: str = ""
    response_text: str = ""
    tool_uses: list[dict] = field(default_factory=list)
    used_outreach_tools: bool = False
    total_cost_usd: float = 0.0
    num_turns: int = 0
    model_usage: dict = field(default_factory=dict)


_OUTREACH_TOOL_NAMES = {
    "thread",
    "reply",  # deprecated alias for thread
    "send",
    "react",
    "send_gif",
    "send_voice",
    "send_photo",
    "send_document",
    "broadcast",
    "send_message",
    "add_reaction",
    "send_voice_note",
}


def _tool_basename(tool_name: str) -> str:
    if "__" in tool_name:
        return tool_name.rsplit("__", 1)[-1]
    return tool_name


def _is_outreach_tool(tool_name: str) -> bool:
    return _tool_basename(tool_name) in _OUTREACH_TOOL_NAMES


def _describe_tool_use(tool_name: str, tool_input: dict) -> str:
    """Build a human-readable description of a tool invocation."""
    name = _tool_basename(tool_name)
    inp = tool_input or {}
    detail = ""

    if name == "Bash":
        desc = inp.get("description", "")
        cmd = inp.get("command", "")
        detail = desc or (cmd[:60] if cmd else "")
    elif name == "Read":
        path = inp.get("file_path", "")
        detail = path.rsplit("/", 1)[-1] if path else ""
    elif name == "Write":
        path = inp.get("file_path", "")
        detail = path.rsplit("/", 1)[-1] if path else ""
    elif name == "Edit":
        path = inp.get("file_path", "")
        detail = path.rsplit("/", 1)[-1] if path else ""
    elif name == "Grep":
        pattern = inp.get("pattern", "")
        detail = f'"{pattern[:40]}"' if pattern else ""
    elif name == "Glob":
        pattern = inp.get("pattern", "")
        detail = pattern[:40] if pattern else ""
    elif name in ("WebSearch", "web_search"):
        query = inp.get("query", "")
        detail = query[:50] if query else ""
    elif name in ("WebFetch", "web_fetch"):
        url = inp.get("url", "")
        detail = url[:60] if url else ""

    # MCP tools: show server__tool as "server: tool"
    if "__" in tool_name:
        parts = tool_name.split("__", 2)
        if len(parts) >= 3:
            name = f"{parts[1]}: {parts[2]}"

    if detail:
        return f"{name} — {detail}"
    return name


class StreamingSession:
    """Persistent bidirectional Claude Code session via SDK client.

    Unlike Session which blocks on each send(), StreamingSession:
    - Connects once and stays connected
    - send() writes to transport and returns immediately
    - A background reader loop processes responses
    - Response callback fires when agent finishes a turn
    """

    def __init__(
        self,
        config: StreamingSessionConfig,
        *,
        response_callback=None,  # async fn(StreamingTurnResult)
        conversation_store=None,  # ConversationStore for history logging
        cost_callback=None,  # fn(agent_name, cost_usd, input_tokens, output_tokens, session_id)
        analytics_store=None,
    ) -> None:
        self._config = config
        self._response_callback = response_callback
        self._cost_callback = cost_callback  # Sync callback to persist costs
        self._conversation_store = conversation_store
        self._analytics_store = analytics_store
        self._client = None
        self._reader_task: asyncio.Task | None = None
        self._connected = False
        self._last_response = ""
        self._pending_chats: list[tuple[str, str, str]] = []  # Queue of (platform, chat_id, message_id)

        self.agent_name = config.agent_name
        self.session_id = config.resume_session_id  # CC session ID (persisted across restarts)
        self.created_at = time.time()
        self.last_active = self.created_at
        self.usage = SessionUsage()
        self._stats = {"turns": 0, "messages_sent": 0, "errors": 0, "reconnects": 0, "auto_restarts": 0}
        self._current_activity = ""  # Current tool being used (for UI streaming)
        self._activity_log: list[str] = []  # All tool activities this turn
        self._current_thinking = ""  # Latest thinking block (for UI streaming)
        self.account_info: dict = {}  # Populated from SDK init: email, subscriptionType, apiProvider
        self._on_session_id = None  # async fn(agent_name, session_id) — called when session_id is captured
        self._context_warned = False  # Track if we've already warned this session
        self._last_restart_block_notice_at = 0.0
        self._effort_override: str | None = None  # Session-level thinking effort override

    async def connect(self) -> None:
        """Connect to Claude Code. Starts the reader loop."""
        from claude_agent_sdk import ClaudeAgentOptions, ClaudeSDKClient

        # Load MCP servers from .mcp.json
        mcp_servers = self._config.mcp_servers
        if not mcp_servers:
            mcp_json_path = Path(self._config.working_dir) / ".mcp.json"
            if mcp_json_path.exists():
                try:
                    mcp_data = json.loads(mcp_json_path.read_text())
                    mcp_servers = mcp_data.get("mcpServers", {})
                    _log(f"streaming[{self.agent_name}]: loaded {len(mcp_servers)} MCP servers")
                except Exception as e:
                    _log(f"streaming[{self.agent_name}]: failed to read .mcp.json: {e}")

        options = ClaudeAgentOptions(
            cwd=self._config.working_dir,
            allowed_tools=self._config.allowed_tools or DEFAULT_STREAMING_ALLOWED_TOOLS,
            permission_mode=self._config.permission_mode,
            mcp_servers=mcp_servers or None,
        )

        if self._config.disallowed_tools:
            options.disallowed_tools = self._config.disallowed_tools

        if self._config.model:
            options.model = self._config.model

        if self._config.max_turns:
            options.max_turns = self._config.max_turns

        if self._config.system_prompt:
            options.system_prompt = self._config.system_prompt

        if self._config.subagents:
            options.agents = self._config.subagents

        # Apply thinking effort
        effort = self.effective_effort
        if effort and effort != "medium":
            options.effort = effort

        # Build provider env overrides (Ollama / custom compatible endpoints)
        provider_env = {}
        if self._config.provider_url:
            provider_env["ANTHROPIC_BASE_URL"] = self._config.provider_url
        if self._config.provider_key:
            provider_env["ANTHROPIC_API_KEY"] = self._config.provider_key
            provider_env["ANTHROPIC_AUTH_TOKEN"] = self._config.provider_key
        if provider_env:
            # Generous timeout for slow local/third-party models (30 min)
            provider_env.setdefault("API_TIMEOUT_MS", "1800000")
            options.env = provider_env

        # Resume previous session if we have a session ID
        if self.session_id:
            options.resume = self.session_id
            _log(f"streaming[{self.agent_name}]: resuming session {self.session_id[:12]}...")

        self._client = ClaudeSDKClient(options)
        await self._client.connect()
        self._connected = True

        # Capture account info from SDK init result
        try:
            server_info = await self._client.get_server_info()
            if server_info and "account" in server_info:
                self.account_info = server_info["account"]
                _log(f"streaming[{self.agent_name}]: account — {self.account_info.get('subscriptionType', 'unknown')} ({self.account_info.get('apiProvider', 'unknown')})")
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: failed to get account info: {e}")

        # Start background reader
        self._reader_task = asyncio.create_task(self._reader_loop())

        _log(f"streaming[{self.agent_name}]: connected, reader loop started")

        # Auto-send wake prompt with saved context injected
        is_resume = bool(self.session_id)
        ctx_block = ""
        if self._config.wake_context:
            ctx_block = f"\n\n── Saved State ──\n{self._config.wake_context}\n──────────────────"

        # Current time for agent orientation
        try:
            _tz = zoneinfo.ZoneInfo(self._config.timezone or "America/Los_Angeles")
            _now = datetime.now(_tz)
            time_str = _now.strftime("%A, %B %-d, %Y at %-I:%M %p %Z")
        except Exception:
            time_str = datetime.now().strftime("%A, %B %-d, %Y at %-I:%M %p UTC")

        tools_hint = (
            "You have explicit pinky-messaging outreach tools: "
            "send, thread, react, send_gif, send_voice, send_photo, send_document, broadcast."
            "\n\nIMPORTANT: If your tools are deferred (require ToolSearch before use), "
            "immediately call ToolSearch with TWO queries to pre-load your core tools "
            "(max 10 per call):\n"
            '1. "select:mcp__pinky-messaging__send,mcp__pinky-messaging__thread,'
            "mcp__pinky-messaging__react,mcp__pinky-self__send_heartbeat,"
            "mcp__pinky-self__send_to_agent,mcp__pinky-self__check_inbox,"
            "mcp__pinky-self__create_task,mcp__pinky-self__get_next_task,"
            'mcp__pinky-self__complete_task,mcp__pinky-self__context_status"\n'
            '2. "select:mcp__pinky-memory__reflect,mcp__pinky-memory__recall,'
            "mcp__pinky-self__who_am_i,mcp__pinky-self__load_my_context,"
            "mcp__pinky-self__save_my_context,mcp__pinky-self__check_my_health,"
            'mcp__pinky-self__agent_status,mcp__pinky-self__claim_task"\n'
            "Do this silently before any other action — do not mention it to the user. "
            "Call both ToolSearch queries in parallel in a single response."
        )
        restart_reason = self._config.restart_reason
        if is_resume:
            header = f"Session resumed after daemon restart. Current time: {time_str}."
            instruction = "Pick up where you left off."
        elif restart_reason == "context_restart":
            header = f"Context restarted. Current time: {time_str}."
            instruction = "Fresh context — pick up from saved state."
        elif restart_reason == "auto_restart":
            header = f"Auto-restarted (context limit). Current time: {time_str}."
            instruction = "Context was getting full — pick up from saved state."
        else:
            header = f"New session started. Current time: {time_str}."
            instruction = "You're connected via Pinky's message broker."
        self._config.restart_reason = ""  # Clear after use

        wake_prompt = (
            f"{header}{ctx_block}\n\n"
            f"{instruction} Users will message you through Telegram. "
            "Use send(chat_id, platform, text) for normal responses. "
            "Use thread(message_id, text) only when you want to quote/thread a specific message. "
            f"{tools_hint} If you do not call an outreach tool, Pinky may fall back to plain-text delivery based on agent settings."
        )
        try:
            await self._client.query(wake_prompt)
            _log(f"streaming[{self.agent_name}]: sent wake prompt ({'resume' if is_resume else 'new'})")
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: wake prompt failed: {e}")

    async def send(
        self,
        prompt: str,
        platform: str = "",
        chat_id: str = "",
        message_id: str = "",
        agent_hint: str = "",
    ) -> None:
        """Send a message to the agent. Non-blocking — returns immediately.

        Args:
            prompt: The formatted message to send.
            platform: The platform the message came from (e.g. 'telegram').
            chat_id: The chat_id to route the response back to.
            message_id: The source message_id to route reactions back to.
            agent_hint: Extra context appended to the query but NOT stored in
                conversation history (e.g. reply-platform hints).
        """
        if not self._connected or not self._client:
            _log(f"streaming[{self.agent_name}]: not connected, dropping message")
            return

        self.last_active = time.time()
        self._stats["messages_sent"] += 1

        # Log to conversation store with platform metadata (clean prompt, no hints)
        if self._conversation_store:
            try:
                self._conversation_store.append(
                    self.id, "user", prompt,
                    platform=platform, chat_id=chat_id,
                )
            except Exception as e:
                _log(f"streaming[{self.agent_name}]: conversation store append failed: {e}")

        try:
            await self._client.query(prompt + agent_hint)
            if chat_id:
                self._pending_chats.append((platform, chat_id, message_id))
            _log(f"streaming[{self.agent_name}]: sent message (chat={chat_id})")
        except Exception as e:
            self._stats["errors"] += 1
            _log(f"streaming[{self.agent_name}]: send error: {e}")
            # Try to reconnect
            await self._try_reconnect()

    async def _reader_loop(self) -> None:
        """Background loop that reads responses and fires callbacks."""
        from claude_agent_sdk.types import (
            AssistantMessage,
            ResultMessage,
            TextBlock,
            ThinkingBlock,
            ToolResultBlock,
            ToolUseBlock,
        )

        _log(f"streaming[{self.agent_name}]: reader loop running")
        turn_tool_uses = []  # Track tool uses per turn
        turn_thinking: list[str] = []  # Track thinking blocks per turn

        try:
            async for msg in self._client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    # If the SDK signals an API-level error (e.g. content filtering,
                    # rate limit, invalid request), skip the content blocks entirely —
                    # the text in them may be raw API error JSON that must never reach
                    # the user's chat.
                    if msg.error:
                        _log(
                            f"streaming[{self.agent_name}]: assistant error={msg.error!r}"
                            f" stop_reason={msg.stop_reason!r} — suppressing content"
                        )
                        # Don't touch _last_response; fall through to usage/session_id capture.
                    else:
                        # Extract text and tool uses from content blocks
                        text_parts = []
                        block_types = [type(b).__name__ for b in msg.content]
                        if any(t != "TextBlock" for t in block_types):
                            _log(f"streaming[{self.agent_name}]: content blocks: {block_types}")
                        for block in msg.content:
                            if isinstance(block, TextBlock):
                                text_parts.append(block.text)
                            elif isinstance(block, ThinkingBlock):
                                if block.thinking:
                                    turn_thinking.append(block.thinking)
                                    self._current_thinking = block.thinking
                            elif isinstance(block, ToolUseBlock):
                                desc = _describe_tool_use(
                                    block.name,
                                    block.input if isinstance(block.input, dict) else {},
                                )
                                self._current_activity = desc
                                self._activity_log.append(desc)
                                turn_tool_uses.append({
                                    "tool": block.name,
                                    "input": block.input if isinstance(block.input, dict) else str(block.input)[:200],
                                })
                            elif isinstance(block, ToolResultBlock):
                                # Attach result to the last matching tool use
                                content_str = str(block.content)[:300] if block.content else ""
                                if turn_tool_uses:
                                    turn_tool_uses[-1]["error"] = block.is_error
                                    if content_str:
                                        turn_tool_uses[-1]["result_preview"] = content_str[:200]
                        text = "\n".join(text_parts)
                        if text:
                            self._last_response = text

                    # Track usage
                    if msg.usage:
                        self.usage.input_tokens += msg.usage.get("input_tokens", 0)
                        self.usage.output_tokens += msg.usage.get("output_tokens", 0)

                    # Capture session ID for persistence
                    if msg.session_id and msg.session_id != self.session_id:
                        self.session_id = msg.session_id
                        _log(f"streaming[{self.agent_name}]: captured session_id {self.session_id[:12]}")
                        if self._on_session_id:
                            try:
                                await self._on_session_id(self.agent_name, self.session_id)
                            except Exception:
                                pass

                elif isinstance(msg, ResultMessage):
                    # Debug: log result message details
                    if msg.num_turns and msg.num_turns > 0:
                        _log(f"streaming[{self.agent_name}]: result — turns={msg.num_turns}, cost=${msg.total_cost_usd or 0:.4f}, model_usage={msg.model_usage}")

                    # Get the routing info for this response
                    if self._pending_chats:
                        resp_platform, resp_chat_id, resp_message_id = self._pending_chats.pop(0)
                    else:
                        resp_platform, resp_chat_id, resp_message_id = ("", "", "")

                    # If the SDK reports an error result, discard _last_response — it may
                    # contain raw API error JSON (e.g. content filter, rate limit) that must
                    # never be forwarded to the user's chat.
                    if msg.is_error:
                        _log(
                            f"streaming[{self.agent_name}]: error result"
                            f" stop_reason={msg.stop_reason!r}"
                            f" errors={msg.errors!r} — suppressing forwarded response"
                        )
                        self._last_response = ""
                        self._current_activity = ""
                        self._activity_log = []
                        self._current_thinking = ""
                        turn_tool_uses = []
                        turn_thinking = []
                        self._stats["turns"] += 1
                        self.last_active = time.time()
                        continue

                    # Turn complete — fire response callback
                    turn_result = StreamingTurnResult(
                        agent_name=self.agent_name,
                        session_id=self.id,
                        platform=resp_platform,
                        chat_id=resp_chat_id,
                        message_id=resp_message_id,
                        response_text=self._last_response,
                        tool_uses=list(turn_tool_uses),
                        used_outreach_tools=any(
                            _is_outreach_tool(tool_use.get("tool", ""))
                            for tool_use in turn_tool_uses
                        ),
                        total_cost_usd=msg.total_cost_usd or 0.0,
                        num_turns=msg.num_turns or 0,
                        model_usage=msg.model_usage or {},
                    )

                    if self._response_callback and (turn_result.response_text or turn_result.tool_uses):
                        try:
                            await self._response_callback(turn_result)
                        except Exception as e:
                            _log(f"streaming[{self.agent_name}]: callback error: {e}")

                    # Track usage from result
                    if msg.total_cost_usd:
                        self.usage.total_cost_usd += msg.total_cost_usd
                        # Persist cost to DB for lifetime tracking
                        if self._cost_callback:
                            try:
                                self._cost_callback(
                                    self.agent_name, msg.total_cost_usd,
                                    msg.usage.get("input_tokens", 0) if msg.usage else 0,
                                    msg.usage.get("output_tokens", 0) if msg.usage else 0,
                                    self.session_id or "",
                                )
                            except Exception as e:
                                _log(f"streaming[{self.agent_name}]: cost callback error: {e}")
                    if msg.usage:
                        self.usage.last_usage = msg.usage

                    # Log assistant response to conversation store with metadata
                    if self._last_response and self._conversation_store:
                        try:
                            metadata = {}
                            if turn_tool_uses:
                                metadata["tool_uses"] = turn_tool_uses
                            if turn_thinking:
                                metadata["thinking"] = turn_thinking
                            if msg.model_usage:
                                metadata["model_usage"] = msg.model_usage
                            if msg.total_cost_usd:
                                metadata["cost_usd"] = msg.total_cost_usd
                            if msg.num_turns:
                                metadata["num_turns"] = msg.num_turns
                            if metadata:
                                _log(f"streaming[{self.agent_name}]: saving metadata: {list(metadata.keys())}, tools={len(turn_tool_uses)}")
                            self._conversation_store.append(
                                self.id, "assistant", self._last_response,
                                platform=resp_platform, chat_id=resp_chat_id,
                                metadata=metadata if metadata else None,
                            )
                        except Exception as e:
                            _log(f"streaming[{self.agent_name}]: failed to save to conversation store: {e}")

                    self._last_response = ""
                    self._current_activity = ""
                    self._activity_log = []
                    self._current_thinking = ""
                    turn_tool_uses = []  # Reset for next turn
                    turn_thinking = []  # Reset for next turn
                    self._stats["turns"] += 1
                    self.last_active = time.time()

                    _log(f"streaming[{self.agent_name}]: turn complete (total: {self._stats['turns']})")

                    # Check context usage for auto-restart
                    await self._check_context()

        except Exception as e:
            _log(f"streaming[{self.agent_name}]: reader loop error: {e}")
            self._connected = False
            # Try reconnect
            await self._try_reconnect()

    async def _check_context(self) -> None:
        """Check context usage after each turn. Warn or force restart."""
        if not self._client or not self._connected:
            return

        try:
            ctx = await self._client.get_context_usage()
            total = ctx.get("totalTokens", 0)
            reported_max = ctx.get("maxTokens", 0)

            # Fix: SDK reports 200k for 1M models — use actual window
            max_t = reported_max
            if self._config.model in _1M_MODELS and reported_max <= 200_000:
                max_t = 1_000_000

            pct = round(total / max_t * 100) if max_t > 0 else 0

            if pct >= self._config.context_restart_pct:
                # Force restart
                _log(f"streaming[{self.agent_name}]: context at {pct}% — force restarting")
                restarted = await self.force_restart()
                if restarted:
                    self._stats["auto_restarts"] += 1

            elif pct >= self._config.context_warn_pct and not self._context_warned:
                # Warn agent
                self._context_warned = True
                remaining = max_t - total
                warn_msg = (
                    f"[SYSTEM] Context at {pct}% ({total:,}/{max_t:,} tokens). "
                    f"~{remaining:,} tokens remaining. "
                    f"Save your state with save_my_context before hitting {self._config.context_restart_pct}%, "
                    f"or call context_restart when ready."
                )
                try:
                    await self._client.query(warn_msg)
                    _log(f"streaming[{self.agent_name}]: warned agent at {pct}% context")
                except Exception:
                    pass

        except Exception as e:
            _log(f"streaming[{self.agent_name}]: context check failed: {e}")

    async def _notify_restart_blocked(self, guard: dict) -> None:
        """Tell the agent why restart is blocked, with basic rate limiting."""
        if not self._client:
            return

        now = time.time()
        cooldown = max(int(getattr(self._config, "restart_guard_cooldown_sec", 60) or 60), 1)
        if (now - self._last_restart_block_notice_at) < cooldown:
            return

        self._last_restart_block_notice_at = now
        self._stats["restart_blocks"] = self._stats.get("restart_blocks", 0) + 1

        detail = guard.get("message") or (
            "Context restart is blocked until you call save_my_context() from this session."
        )
        warn_msg = (
            "[SYSTEM] Context restart blocked. "
            f"{detail} Use save_my_context() now, then retry once you've saved your latest work."
        )
        try:
            await self._client.query(warn_msg)
        except Exception:
            pass

    async def force_restart(self) -> bool:
        """Force a context restart — disconnect, clear session, reconnect fresh."""
        if self._config.restart_guard:
            try:
                guard = self._config.restart_guard(self)
            except Exception as e:
                _log(f"streaming[{self.agent_name}]: restart guard failed: {e}")
                guard = {}
            if guard and not guard.get("restart_safe", False):
                _log(
                    f"streaming[{self.agent_name}]: restart blocked: "
                    f"{guard.get('reason', 'missing save')}"
                )
                await self._notify_restart_blocked(guard)
                return False

        _log(f"streaming[{self.agent_name}]: force restarting session")

        # Notify the persistence callback to clear session ID
        if self._on_session_id:
            try:
                await self._on_session_id(self.agent_name, "")
            except Exception:
                pass

        # Disconnect
        await self.disconnect()

        # Refresh wake context from DB before reconnecting
        if self._config.wake_context_builder:
            try:
                self._config.wake_context = self._config.wake_context_builder(self.agent_name)
            except Exception as e:
                _log(f"streaming[{self.agent_name}]: failed to refresh wake context: {e}")

        # Reconnect fresh with wake context
        self._config.resume_session_id = ""
        if not self._config.restart_reason:
            self._config.restart_reason = "auto_restart"
        self.session_id = ""
        self._context_warned = False

        try:
            await self.connect()
            _log(f"streaming[{self.agent_name}]: force restart complete — fresh session")
            return True
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: force restart failed: {e}")
            self._connected = False
            return False

    async def idle_sleep(self) -> bool:
        """Put the session to sleep due to inactivity.

        Asks the agent to save memories, then disconnects. Session ID is
        preserved so the next wake can resume.
        Returns True if successfully slept.
        """
        if not self._connected or not self._client:
            return False

        _log(f"streaming[{self.agent_name}]: idle sleep triggered ({self._config.idle_timeout}s idle)")

        # Ask agent to save state before sleeping
        try:
            await self._client.query(
                "[SYSTEM] You've been idle for over an hour. Auto-sleep is activating.\n\n"
                "Before your session is suspended:\n"
                "1. Use reflect() to persist key learnings and current task state\n"
                "2. Note what you were working on so you can resume later\n\n"
                "Your session will be preserved and resumed when you're needed next."
            )
            _log(f"streaming[{self.agent_name}]: memory save prompt sent before idle sleep")
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: memory save failed before idle sleep: {e}")

        # Disconnect but preserve session ID for resume
        await self.disconnect()
        self._stats["auto_restarts"] += 1
        _log(f"streaming[{self.agent_name}]: idle sleep complete — session preserved for resume")
        return True

    async def _try_reconnect(self) -> None:
        """Attempt to reconnect after a failure."""
        self._stats["reconnects"] += 1
        _log(f"streaming[{self.agent_name}]: attempting reconnect #{self._stats['reconnects']}")

        try:
            await self.disconnect()
            await asyncio.sleep(2)
            await self.connect()
            _log(f"streaming[{self.agent_name}]: reconnected successfully")
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: reconnect failed: {e}")
            self._connected = False

    async def disconnect(self) -> None:
        """Disconnect from Claude Code."""
        self._connected = False
        if self._reader_task and not self._reader_task.done():
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
        if self._client:
            try:
                await self._client.disconnect()
            except Exception:
                pass
            self._client = None
        _log(f"streaming[{self.agent_name}]: disconnected")

    @property
    def effective_effort(self) -> str:
        """Current thinking effort: session override > config default."""
        return self._effort_override or self._config.thinking_effort or "medium"

    def set_effort(self, level: str) -> None:
        """Set session-level thinking effort override."""
        if level not in ("low", "medium", "high", "max"):
            raise ValueError(f"Invalid effort level: {level}")
        self._effort_override = level
        _log(f"streaming[{self.agent_name}]: effort set to {level}")

    def clear_effort_override(self) -> None:
        """Clear session override, revert to agent default."""
        self._effort_override = None
        _log(f"streaming[{self.agent_name}]: effort override cleared")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "connected": self._connected,
            "pending_responses": len(self._pending_chats),
            "current_activity": self._current_activity,
            "current_thinking": self._current_thinking,
            "activity_log": list(self._activity_log),
            "cost_usd": round(self.usage.total_cost_usd, 6),
            "account": self.account_info,
            "thinking_effort": self.effective_effort,
        }

    @property
    def id(self) -> str:
        return f"{self.agent_name}-{self._config.label or 'main'}"
