"""Session manager — stateful Claude Code sessions.

Each session maintains its own Claude Code subprocess context,
MCP server connections, and conversation history. Sessions are
created via API and persist until explicitly destroyed or timed out.

Context tracking: sessions estimate token usage from message lengths
and support auto-restart with checkpoint summaries when context fills up.
"""

from __future__ import annotations

import asyncio
import json
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.claude_runner import ClaudeRunner, ClaudeRunnerConfig, RunResult
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.session_store import SessionRecord, SessionStore


# Rough token estimate: ~4 chars per token for English text
CHARS_PER_TOKEN = 4

# Default context window sizes by model family
MODEL_CONTEXT_SIZES = {
    "opus": 200_000,
    "sonnet": 200_000,
    "haiku": 200_000,
    "default": 200_000,
}


class SessionState(str, Enum):
    idle = "idle"
    running = "running"
    error = "error"
    closed = "closed"
    restarting = "restarting"


class SessionType(str, Enum):
    main = "main"        # Always-on primary session (heartbeat, wake schedules)
    worker = "worker"    # Disposable task session (spawned by main, auto-closes)
    chat = "chat"        # Interactive chat session (UI or API driven)


@dataclass
class SessionMessage:
    """A single message in the session history."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: float = field(default_factory=time.time)
    duration_ms: int = 0
    error: str = ""

    @property
    def estimated_tokens(self) -> int:
        return max(1, len(self.content) // CHARS_PER_TOKEN)


@dataclass
class ContextStatus:
    """Context window status for a session."""

    session_id: str
    estimated_tokens: int
    max_tokens: int
    context_used_pct: float
    message_count: int
    needs_restart: bool
    restart_threshold_pct: float
    checkpoints: int
    last_checkpoint_at: float | None

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "estimated_tokens": self.estimated_tokens,
            "max_tokens": self.max_tokens,
            "context_used_pct": round(self.context_used_pct, 1),
            "message_count": self.message_count,
            "needs_restart": self.needs_restart,
            "restart_threshold_pct": self.restart_threshold_pct,
            "checkpoints": self.checkpoints,
            "last_checkpoint_at": self.last_checkpoint_at,
        }


@dataclass
class Checkpoint:
    """A conversation checkpoint for restart recovery."""

    summary: str
    message_count: int
    timestamp: float = field(default_factory=time.time)
    estimated_tokens_at_checkpoint: int = 0


@dataclass
class SessionUsage:
    """Cumulative usage stats for a session."""

    total_cost_usd: float = 0.0
    total_turns: int = 0
    total_queries: int = 0
    total_duration_ms: int = 0
    total_api_duration_ms: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    last_stop_reason: str = ""
    last_usage: dict = field(default_factory=dict)
    last_model_usage: dict = field(default_factory=dict)

    def record(self, result) -> None:
        """Record usage from a RunResult."""
        self.total_cost_usd += result.cost_usd
        self.total_turns += result.num_turns
        self.total_queries += 1
        self.total_duration_ms += result.duration_ms
        self.total_api_duration_ms += result.duration_api_ms
        self.last_stop_reason = result.stop_reason
        if result.usage:
            self.last_usage = result.usage
            self.input_tokens += result.usage.get("input_tokens", 0)
            self.output_tokens += result.usage.get("output_tokens", 0)
            self.cache_read_tokens += result.usage.get("cache_read_input_tokens", 0) or result.usage.get("cache_read_tokens", 0)
            self.cache_write_tokens += result.usage.get("cache_creation_input_tokens", 0) or result.usage.get("cache_write_tokens", 0)
        if result.model_usage:
            self.last_model_usage = result.model_usage

    def to_dict(self) -> dict:
        return {
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_turns": self.total_turns,
            "total_queries": self.total_queries,
            "total_duration_ms": self.total_duration_ms,
            "total_api_duration_ms": self.total_api_duration_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "cache_read_tokens": self.cache_read_tokens,
            "cache_write_tokens": self.cache_write_tokens,
            "last_stop_reason": self.last_stop_reason,
            "last_usage": self.last_usage,
            "last_model_usage": self.last_model_usage,
        }


@dataclass
class SessionInfo:
    """Public session metadata."""

    id: str
    state: SessionState
    model: str
    soul: str
    created_at: float
    last_active: float
    message_count: int
    mcp_servers: list[str]
    allowed_tools: list[str]
    context_used_pct: float = 0.0
    permission_mode: str = ""
    session_type: str = "chat"
    agent_name: str = ""
    usage: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "state": self.state.value,
            "model": self.model,
            "soul": self.soul[:200] if self.soul else "",
            "created_at": self.created_at,
            "last_active": self.last_active,
            "message_count": self.message_count,
            "mcp_servers": self.mcp_servers,
            "allowed_tools": self.allowed_tools,
            "context_used_pct": round(self.context_used_pct, 1),
            "permission_mode": self.permission_mode,
            "session_type": self.session_type,
            "agent_name": self.agent_name,
            "usage": self.usage,
        }


class Session:
    """A stateful Claude Code session.

    Wraps a ClaudeRunner with a persistent session ID so that
    each call to send() continues the same conversation.

    Tracks estimated context usage and supports auto-restart
    with checkpoint summaries when context fills up.
    """

    def __init__(
        self,
        *,
        session_id: str = "",
        model: str = "",
        soul: str = "",
        working_dir: str = ".",
        allowed_tools: list[str] | None = None,
        max_turns: int = 25,
        timeout: float = 300.0,
        system_prompt: str = "",
        restart_threshold_pct: float = 80.0,
        auto_restart: bool = True,
        permission_mode: str = "",
        session_type: str = "chat",
        agent_name: str = "",
    ) -> None:
        self.id = session_id or f"pinky-{uuid.uuid4().hex[:12]}"
        self.model = model
        self.soul = soul
        self.working_dir = working_dir
        self.state = SessionState.idle
        self.session_type = SessionType(session_type) if session_type else SessionType.chat
        self.agent_name = agent_name
        self.created_at = time.time()
        self.last_active = self.created_at
        self.history: list[SessionMessage] = []
        self.mcp_servers: list[str] = []
        self.allowed_tools = allowed_tools or []
        self.permission_mode = permission_mode
        self.checkpoints: list[Checkpoint] = []
        self.restart_threshold_pct = restart_threshold_pct
        self.auto_restart = auto_restart
        self._restart_count = 0
        self._sdk_session_id = ""  # Real session ID from SDK
        self.usage = SessionUsage()

        self._max_turns = max_turns
        self._timeout = timeout
        self._system_prompt = system_prompt
        self._lock = asyncio.Lock()
        self._store: SessionStore | None = None  # Set by SessionManager
        self._conversation_store: ConversationStore | None = None  # Set by SessionManager
        self._agent_registry: AgentRegistry | None = None  # Set by SessionManager
        self._hook_manager = None  # Set by SessionManager
        self._init_runner(working_dir, model, max_turns, timeout, permission_mode)

    def _init_runner(
        self,
        working_dir: str,
        model: str,
        max_turns: int,
        timeout: float,
        permission_mode: str = "",
    ) -> None:
        """Initialize runner — prefer SDK, fall back to CLI subprocess."""
        try:
            from pinky_daemon.sdk_runner import SDKRunner, SDKRunnerConfig, sdk_available
            if sdk_available():
                # Read .mcp.json from working dir to pass MCP servers to SDK
                mcp_servers = {}
                mcp_json_path = Path(working_dir) / ".mcp.json"
                if mcp_json_path.exists():
                    try:
                        mcp_data = json.loads(mcp_json_path.read_text())
                        mcp_servers = mcp_data.get("mcpServers", {})
                        self.mcp_servers = list(mcp_servers.keys())
                        _log(f"session {self.id}: loaded {len(mcp_servers)} MCP servers from .mcp.json")
                    except Exception as e:
                        _log(f"session {self.id}: failed to read .mcp.json: {e}")
                sdk_config = SDKRunnerConfig(
                    working_dir=working_dir,
                    model=model or None,
                    max_turns=max_turns,
                    allowed_tools=self.allowed_tools,
                    mcp_servers=mcp_servers,
                )
                self._runner = SDKRunner(
                    sdk_config,
                    hook_manager=self._hook_manager,
                    agent_name=self.agent_name,
                )
                self._runner_type = "sdk"
                _log(f"session {self.id}: using SDK runner")
                return
        except Exception:
            pass

        # Fallback to CLI subprocess
        config = ClaudeRunnerConfig(
            working_dir=working_dir,
            session_id=self.id,
            model=model,
            max_turns=max_turns,
            timeout=timeout,
            allowed_tools=self.allowed_tools,
            permission_mode=permission_mode,
        )
        self._runner = ClaudeRunner(config)
        self._runner_type = "cli"
        _log(f"session {self.id}: using CLI runner (SDK not available)")

    @property
    def max_tokens(self) -> int:
        """Estimated max context tokens for this session's model."""
        for key, size in MODEL_CONTEXT_SIZES.items():
            if key in (self.model or "").lower():
                return size
        return MODEL_CONTEXT_SIZES["default"]

    @property
    def estimated_tokens(self) -> int:
        """Estimate total tokens used in the current context."""
        # System prompt tokens
        sys_tokens = len(self._system_prompt) // CHARS_PER_TOKEN if self._system_prompt else 0

        # Message tokens (only since last restart)
        msg_tokens = sum(m.estimated_tokens for m in self._active_history())

        return sys_tokens + msg_tokens

    @property
    def context_used_pct(self) -> float:
        """Percentage of context window used."""
        if self.max_tokens == 0:
            return 0.0
        return (self.estimated_tokens / self.max_tokens) * 100

    @property
    def needs_restart(self) -> bool:
        """Whether the session should be restarted due to context pressure."""
        return self.context_used_pct >= self.restart_threshold_pct

    def _active_history(self) -> list[SessionMessage]:
        """Messages since the last checkpoint/restart."""
        if not self.checkpoints:
            return self.history

        last_cp = self.checkpoints[-1]
        return [m for m in self.history if m.timestamp > last_cp.timestamp]

    async def send(self, content: str) -> SessionMessage:
        """Send a message and get the response.

        If auto_restart is enabled and context is near full,
        automatically checkpoints and restarts before processing.
        """
        async with self._lock:
            # Auto-restart if context is getting full
            if self.auto_restart and self.needs_restart and self.history:
                _log(f"session {self.id}: auto-restarting at {self.context_used_pct:.0f}% context")
                await self._checkpoint_and_restart()

            self.state = SessionState.running
            self.last_active = time.time()

            # Record user message
            user_msg = SessionMessage(role="user", content=content)
            self.history.append(user_msg)

            # Determine if this is a fresh start or continuation
            active = self._active_history()
            is_first = len(active) <= 1  # Only the message we just added

            # Build system prompt for fresh starts
            system_prompt = ""
            if is_first:
                system_prompt = self._build_restart_prompt()

            start = time.time()

            # Use real SDK session ID for resume if available
            resume_id = self._sdk_session_id if self._sdk_session_id else self.id

            result = await self._runner.run(
                content,
                session_id=resume_id,
                resume=not is_first,
                system_prompt=system_prompt,
            )
            elapsed_ms = int((time.time() - start) * 1000)

            # Capture real session ID from SDK
            if result.session_id:
                self._sdk_session_id = result.session_id

            # Record usage stats
            self.usage.record(result)

            # Record assistant response
            assistant_msg = SessionMessage(
                role="assistant",
                content=result.output,
                duration_ms=elapsed_ms,
                error=result.error if not result.ok else "",
            )
            self.history.append(assistant_msg)

            self.state = SessionState.idle if result.ok else SessionState.error
            self._persist()
            return assistant_msg

    async def restart(self) -> Checkpoint:
        """Manually restart the session with a checkpoint.

        Saves a summary of the current conversation, resets the
        Claude Code session, and prepares for fresh messages.
        """
        async with self._lock:
            return await self._checkpoint_and_restart()

    async def _checkpoint_and_restart(self) -> Checkpoint:
        """Internal: create checkpoint and restart session."""
        self.state = SessionState.restarting

        # Generate summary of active conversation
        summary = self._generate_checkpoint_summary()

        checkpoint = Checkpoint(
            summary=summary,
            message_count=len(self.history),
            estimated_tokens_at_checkpoint=self.estimated_tokens,
        )
        self.checkpoints.append(checkpoint)
        self._restart_count += 1

        # Reset the runner's session ID to force a new CC session
        new_session_suffix = f"-r{self._restart_count}"
        self._runner._config.session_id = f"{self.id}{new_session_suffix}"

        _log(
            f"session {self.id}: checkpointed at {checkpoint.message_count} messages, "
            f"~{checkpoint.estimated_tokens_at_checkpoint} tokens. restart #{self._restart_count}"
        )

        self.state = SessionState.idle
        self._persist()
        return checkpoint

    def _generate_checkpoint_summary(self) -> str:
        """Generate a summary of the active conversation for restart."""
        active = self._active_history()
        if not active:
            return ""

        # Build a condensed version of the conversation
        parts = []

        # Include key user messages and assistant responses
        for msg in active:
            role_label = "User" if msg.role == "user" else "Assistant"
            # Truncate long messages
            content = msg.content[:500] + "..." if len(msg.content) > 500 else msg.content
            parts.append(f"{role_label}: {content}")

        conversation = "\n".join(parts)

        return (
            f"[Conversation checkpoint — {len(active)} messages, "
            f"restart #{self._restart_count + 1}]\n\n"
            f"Previous conversation summary:\n{conversation}"
        )

    def _build_restart_prompt(self) -> str:
        """Build system prompt for a restarted session.

        Includes: original system prompt + checkpoint summary + agent wake context.
        """
        parts = []

        # Original system prompt
        if self._system_prompt:
            parts.append(self._system_prompt)

        # Agent wake context (persistent, set by agent before restart)
        if self.agent_name and self._agent_registry:
            ctx = self._agent_registry.get_context(self.agent_name)
            if ctx:
                ctx_prompt = ctx.to_prompt()
                if ctx_prompt:
                    parts.append(f"\n---\n{ctx_prompt}\n---")

        # Last checkpoint summary
        if self.checkpoints:
            last = self.checkpoints[-1]
            parts.append(
                f"\n---\n{last.summary}\n---\n"
                f"Continue the conversation from where we left off."
            )

        return "\n\n".join(parts) if parts else ""

    def get_context_status(self) -> ContextStatus:
        """Get detailed context window status."""
        return ContextStatus(
            session_id=self.id,
            estimated_tokens=self.estimated_tokens,
            max_tokens=self.max_tokens,
            context_used_pct=self.context_used_pct,
            message_count=len(self.history),
            needs_restart=self.needs_restart,
            restart_threshold_pct=self.restart_threshold_pct,
            checkpoints=len(self.checkpoints),
            last_checkpoint_at=self.checkpoints[-1].timestamp if self.checkpoints else None,
        )

    @property
    def info(self) -> SessionInfo:
        return SessionInfo(
            id=self.id,
            state=self.state,
            model=self.model,
            soul=self.soul,
            created_at=self.created_at,
            last_active=self.last_active,
            message_count=len(self.history),
            mcp_servers=self.mcp_servers,
            allowed_tools=self.allowed_tools,
            context_used_pct=self.context_used_pct,
            permission_mode=self.permission_mode,
            session_type=self.session_type.value,
            agent_name=self.agent_name,
            usage=self.usage.to_dict(),
        )

    @property
    def message_count(self) -> int:
        return len(self.history)

    def get_history(self, limit: int = 50) -> list[dict]:
        """Get recent message history."""
        msgs = self.history[-limit:]
        return [
            {
                "role": m.role,
                "content": m.content,
                "timestamp": m.timestamp,
                "duration_ms": m.duration_ms,
                "error": m.error,
            }
            for m in msgs
        ]

    def load_history_from_store(self) -> int:
        """Load conversation history from the persistent conversation store.

        Called during session restoration so message_count and context_used_pct
        are accurate after server restart. Returns number of messages loaded.
        """
        if not self._conversation_store:
            return 0

        messages = self._conversation_store.get_history(self.id, limit=500)
        if not messages:
            return 0

        self.history = [
            SessionMessage(
                role=m.role,
                content=m.content,
                timestamp=m.timestamp,
            )
            for m in messages
        ]
        return len(self.history)

    def _persist(self) -> None:
        """Save session state to the persistent store."""
        if not self._store:
            return
        record = SessionRecord(
            id=self.id,
            model=self.model,
            soul=self.soul,
            working_dir=self.working_dir,
            allowed_tools=self.allowed_tools,
            max_turns=self._max_turns,
            timeout=self._timeout,
            system_prompt=self._system_prompt,
            restart_threshold_pct=self.restart_threshold_pct,
            auto_restart=self.auto_restart,
            permission_mode=self.permission_mode,
            state=self.state.value,
            created_at=self.created_at,
            last_active=self.last_active,
            restart_count=self._restart_count,
            sdk_session_id=self._sdk_session_id,
            session_type=self.session_type.value,
            agent_name=self.agent_name,
        )
        self._store.save(record)

    def refresh(self) -> None:
        """Refresh the session — reinitialize runner with fresh MCP config while preserving state.

        Keeps: SDK session ID, conversation history, usage stats, checkpoints.
        Refreshes: runner, MCP servers, tool list.
        The next message will resume the existing Claude Code conversation.
        """
        old_sdk_id = self._sdk_session_id
        old_history_len = len(self.history)

        # Reinit the runner (re-reads .mcp.json, creates fresh SDK runner)
        self._init_runner(
            self.working_dir, self.model,
            self._max_turns, self._timeout,
            self.permission_mode,
        )

        # Preserve the SDK session ID so next run() resumes the conversation
        self._sdk_session_id = old_sdk_id

        _log(
            f"session {self.id}: refreshed runner, preserved sdk_session={old_sdk_id[:12] if old_sdk_id else 'none'}, "
            f"history={old_history_len} messages, mcp_servers={self.mcp_servers}"
        )
        self._persist()

    def close(self) -> None:
        """Mark session as closed."""
        self.state = SessionState.closed
        self._persist()


class SessionManager:
    """Manages multiple concurrent sessions with optional persistence."""

    def __init__(
        self,
        *,
        max_sessions: int = 50,
        store: SessionStore | None = None,
        conversation_store: ConversationStore | None = None,
        agent_registry: AgentRegistry | None = None,
        hook_manager=None,
    ) -> None:
        self._sessions: dict[str, Session] = {}
        self._max_sessions = max_sessions
        self._store = store
        self._conversation_store = conversation_store
        self._agent_registry = agent_registry
        self._hook_manager = hook_manager

        # Restore persisted sessions on startup
        if store:
            self._restore_sessions()

    def _restore_sessions(self) -> None:
        """Restore active sessions from the persistent store."""
        if not self._store:
            return

        records = self._store.list_active()
        for rec in records:
            session = Session(
                session_id=rec.id,
                model=rec.model,
                soul=rec.soul,
                working_dir=rec.working_dir,
                allowed_tools=rec.allowed_tools,
                max_turns=rec.max_turns,
                timeout=rec.timeout,
                system_prompt=rec.system_prompt,
                restart_threshold_pct=rec.restart_threshold_pct,
                auto_restart=rec.auto_restart,
                permission_mode=rec.permission_mode,
                session_type=rec.session_type,
                agent_name=rec.agent_name,
            )
            # Restore metadata
            session.created_at = rec.created_at
            session.last_active = rec.last_active
            session._restart_count = rec.restart_count
            session._sdk_session_id = rec.sdk_session_id
            session._store = self._store
            session._conversation_store = self._conversation_store
            session._agent_registry = self._agent_registry
            session._hook_manager = self._hook_manager
            self._sessions[session.id] = session

            # Load conversation history from persistent store
            msg_count = session.load_history_from_store()
            _log(f"sessions: restored {session.id} model={rec.model or 'default'} messages={msg_count}")

        if records:
            _log(f"sessions: restored {len(records)} session(s) from store")

    def create(
        self,
        *,
        session_id: str = "",
        model: str = "",
        soul: str = "",
        working_dir: str = ".",
        allowed_tools: list[str] | None = None,
        max_turns: int = 25,
        timeout: float = 300.0,
        system_prompt: str = "",
        restart_threshold_pct: float = 80.0,
        auto_restart: bool = True,
        permission_mode: str = "",
        session_type: str = "chat",
        agent_name: str = "",
    ) -> Session:
        """Create a new session."""
        if len(self._sessions) >= self._max_sessions:
            self._evict_oldest()

        session = Session(
            session_id=session_id,
            model=model,
            soul=soul,
            working_dir=working_dir,
            allowed_tools=allowed_tools,
            max_turns=max_turns,
            timeout=timeout,
            system_prompt=system_prompt,
            restart_threshold_pct=restart_threshold_pct,
            auto_restart=auto_restart,
            permission_mode=permission_mode,
            session_type=session_type,
            agent_name=agent_name,
        )
        session._store = self._store
        session._conversation_store = self._conversation_store
        session._agent_registry = self._agent_registry
        session._hook_manager = self._hook_manager
        self._sessions[session.id] = session
        session._persist()
        _log(f"sessions: created {session.id} model={model or 'default'}")
        return session

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def list(self) -> list[SessionInfo]:
        return [s.info for s in self._sessions.values() if s.state != SessionState.closed]

    def refresh(self, session_id: str) -> Session | None:
        """Refresh a session — reinit runner with fresh MCP config, keep conversation."""
        session = self._sessions.get(session_id)
        if not session:
            return None
        session.refresh()
        return session

    def delete(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session:
            session.close()
            _log(f"sessions: deleted {session_id}")
            return True
        return False

    def _evict_oldest(self) -> None:
        idle = [
            s for s in self._sessions.values()
            if s.state in (SessionState.idle, SessionState.error, SessionState.closed)
        ]
        if not idle:
            return
        oldest = min(idle, key=lambda s: s.last_active)
        self.delete(oldest.id)
        _log(f"sessions: evicted {oldest.id}")

    @property
    def count(self) -> int:
        return len(self._sessions)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
