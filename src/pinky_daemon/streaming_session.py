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
from dataclasses import dataclass, field
from pathlib import Path

from pinky_daemon.sessions import SessionUsage

# Models with native 1M context (SDK reports 200k incorrectly)
_1M_MODELS = {"claude-sonnet-4-6", "claude-opus-4-6"}

DEFAULT_STREAMING_ALLOWED_TOOLS = [
    "Read",
    "Glob",
    "Grep",
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
    model: str = ""
    working_dir: str = "."
    allowed_tools: list[str] = field(default_factory=list)
    mcp_servers: dict = field(default_factory=dict)
    permission_mode: str = "bypassPermissions"
    max_turns: int = 25
    system_prompt: str = ""
    resume_session_id: str = ""  # SDK session ID to resume from previous run
    wake_context: str = ""  # Saved continuation context to inject on wake
    wake_context_builder: object = None  # Callable(agent_name) -> str; refreshes wake_context on restart
    context_warn_pct: int = 40  # Warn agent to save state at this %
    context_restart_pct: int = 80  # Force restart at this %
    idle_timeout: int = 3600  # Auto-sleep after this many seconds idle (0 = disabled)


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
    ) -> None:
        self._config = config
        self._response_callback = response_callback
        self._cost_callback = cost_callback  # Sync callback to persist costs
        self._conversation_store = conversation_store
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
        self.account_info: dict = {}  # Populated from SDK init: email, subscriptionType, apiProvider
        self._on_session_id = None  # async fn(agent_name, session_id) — called when session_id is captured
        self._context_warned = False  # Track if we've already warned this session

    async def connect(self) -> None:
        """Connect to Claude Code. Starts the reader loop."""
        from claude_agent_sdk import ClaudeSDKClient, ClaudeAgentOptions

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

        if self._config.model:
            options.model = self._config.model

        if self._config.max_turns:
            options.max_turns = self._config.max_turns

        if self._config.system_prompt:
            options.system_prompt = self._config.system_prompt

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

        tools_hint = (
            "You have explicit pinky-messaging outreach tools: "
            "send, thread, react, send_gif, send_voice, send_photo, send_document, broadcast."
        )
        wake_prompt = (
            f"Session resumed after daemon restart.{ctx_block}\n\n"
            "Pick up where you left off. Users will message you through Telegram. "
            "Use send(chat_id, platform, text) for normal responses. "
            "Use thread(message_id, text) only when you want to quote/thread a specific message. "
            f"{tools_hint} If you do not call an outreach tool, Pinky may fall back to plain-text delivery based on agent settings."
            if is_resume else
            f"New session started.{ctx_block}\n\n"
            "You're connected via Pinky's message broker. Users will message you through Telegram. "
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
    ) -> None:
        """Send a message to the agent. Non-blocking — returns immediately.

        Args:
            prompt: The formatted message to send.
            platform: The platform the message came from (e.g. 'telegram').
            chat_id: The chat_id to route the response back to.
            message_id: The source message_id to route reactions back to.
        """
        if not self._connected or not self._client:
            _log(f"streaming[{self.agent_name}]: not connected, dropping message")
            return

        self.last_active = time.time()
        self._stats["messages_sent"] += 1

        # Log to conversation store with platform metadata
        if self._conversation_store:
            try:
                self._conversation_store.append(
                    self.id, "user", prompt,
                    platform=platform, chat_id=chat_id,
                )
            except Exception:
                pass

        try:
            await self._client.query(prompt)
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
            ToolUseBlock,
            ToolResultBlock,
        )

        _log(f"streaming[{self.agent_name}]: reader loop running")
        turn_tool_uses = []  # Track tool uses per turn

        try:
            async for msg in self._client.receive_messages():
                if isinstance(msg, AssistantMessage):
                    # Extract text and tool uses from content blocks
                    text_parts = []
                    block_types = [type(b).__name__ for b in msg.content]
                    if any(t != "TextBlock" for t in block_types):
                        _log(f"streaming[{self.agent_name}]: content blocks: {block_types}")
                    for block in msg.content:
                        if isinstance(block, TextBlock):
                            text_parts.append(block.text)
                        elif isinstance(block, ToolUseBlock):
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
                            except Exception:
                                pass
                    if msg.usage:
                        self.usage.last_usage = msg.usage

                    # Log assistant response to conversation store with metadata
                    if self._last_response and self._conversation_store:
                        try:
                            metadata = {}
                            if turn_tool_uses:
                                metadata["tool_uses"] = turn_tool_uses
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
                    turn_tool_uses = []  # Reset for next turn
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
                self._stats["auto_restarts"] += 1
                await self.force_restart()

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

    async def force_restart(self) -> None:
        """Force a context restart — disconnect, clear session, reconnect fresh."""
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
        self.session_id = ""
        self._context_warned = False

        try:
            await self.connect()
            _log(f"streaming[{self.agent_name}]: force restart complete — fresh session")
        except Exception as e:
            _log(f"streaming[{self.agent_name}]: force restart failed: {e}")
            self._connected = False

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
                "1. Save any important state to your memory files (MEMORY.md, memory/*.md)\n"
                "2. Use reflect() to persist key learnings\n"
                "3. Note what you were working on so you can resume later\n\n"
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
    def is_connected(self) -> bool:
        return self._connected

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "connected": self._connected,
            "pending_responses": len(self._pending_chats),
            "cost_usd": round(self.usage.total_cost_usd, 6),
            "account": self.account_info,
        }

    @property
    def id(self) -> str:
        return f"{self.agent_name}-main"
