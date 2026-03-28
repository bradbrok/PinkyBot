"""Session manager — stateful Claude Code sessions.

Each session maintains its own Claude Code subprocess context,
MCP server connections, and conversation history. Sessions are
created via API and persist until explicitly destroyed or timed out.
"""

from __future__ import annotations

import asyncio
import sys
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum

from pinky_daemon.claude_runner import ClaudeRunner, ClaudeRunnerConfig, RunResult


class SessionState(str, Enum):
    idle = "idle"
    running = "running"
    error = "error"
    closed = "closed"


@dataclass
class SessionMessage:
    """A single message in the session history."""

    role: str  # "user" or "assistant"
    content: str
    timestamp: float = field(default_factory=time.time)
    duration_ms: int = 0
    error: str = ""


@dataclass
class SessionInfo:
    """Public session metadata."""

    id: str
    state: SessionState
    model: str
    soul: str  # Soul file path or inline
    created_at: float
    last_active: float
    message_count: int
    mcp_servers: list[str]
    allowed_tools: list[str]

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
        }


class Session:
    """A stateful Claude Code session.

    Wraps a ClaudeRunner with a persistent session ID so that
    each call to send() continues the same conversation.
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
    ) -> None:
        self.id = session_id or f"pinky-{uuid.uuid4().hex[:12]}"
        self.model = model
        self.soul = soul
        self.working_dir = working_dir
        self.state = SessionState.idle
        self.created_at = time.time()
        self.last_active = self.created_at
        self.history: list[SessionMessage] = []
        self.mcp_servers: list[str] = []
        self.allowed_tools = allowed_tools or []

        config = ClaudeRunnerConfig(
            working_dir=working_dir,
            session_id=self.id,
            model=model,
            max_turns=max_turns,
            timeout=timeout,
            allowed_tools=self.allowed_tools,
        )
        self._runner = ClaudeRunner(config)
        self._system_prompt = system_prompt
        self._lock = asyncio.Lock()

    async def send(self, content: str) -> SessionMessage:
        """Send a message and get the response.

        Messages are serialized per-session to prevent interleaving.
        """
        async with self._lock:
            self.state = SessionState.running
            self.last_active = time.time()

            # Record user message
            user_msg = SessionMessage(role="user", content=content)
            self.history.append(user_msg)

            start = time.time()
            result = await self._runner.run(
                content,
                session_id=self.id,
                resume=len(self.history) > 1,  # Resume after first message
                system_prompt=self._system_prompt if len(self.history) == 1 else "",
            )
            elapsed_ms = int((time.time() - start) * 1000)

            # Record assistant response
            assistant_msg = SessionMessage(
                role="assistant",
                content=result.output,
                duration_ms=elapsed_ms,
                error=result.error if not result.ok else "",
            )
            self.history.append(assistant_msg)

            self.state = SessionState.idle if result.ok else SessionState.error
            return assistant_msg

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

    def close(self) -> None:
        """Mark session as closed."""
        self.state = SessionState.closed


class SessionManager:
    """Manages multiple concurrent sessions."""

    def __init__(self, *, max_sessions: int = 50) -> None:
        self._sessions: dict[str, Session] = {}
        self._max_sessions = max_sessions

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
    ) -> Session:
        """Create a new session."""
        if len(self._sessions) >= self._max_sessions:
            # Evict oldest idle session
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
        )
        self._sessions[session.id] = session
        _log(f"sessions: created {session.id} model={model or 'default'}")
        return session

    def get(self, session_id: str) -> Session | None:
        """Get a session by ID."""
        return self._sessions.get(session_id)

    def list(self) -> list[SessionInfo]:
        """List all active sessions."""
        return [s.info for s in self._sessions.values() if s.state != SessionState.closed]

    def delete(self, session_id: str) -> bool:
        """Delete a session."""
        session = self._sessions.pop(session_id, None)
        if session:
            session.close()
            _log(f"sessions: deleted {session_id}")
            return True
        return False

    def _evict_oldest(self) -> None:
        """Evict the oldest idle session to make room."""
        idle = [
            s for s in self._sessions.values()
            if s.state in (SessionState.idle, SessionState.error, SessionState.closed)
        ]
        if not idle:
            return

        oldest = min(idle, key=lambda s: s.last_active)
        self.delete(oldest.id)
        _log(f"sessions: evicted {oldest.id} (idle since {oldest.last_active})")

    @property
    def count(self) -> int:
        return len(self._sessions)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
