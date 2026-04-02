"""Agent Hooks — lifecycle callbacks for observability and control.

Hooks fire at key points during agent execution, enabling:
- Audit logging (every tool call recorded)
- Auto-heartbeat (agent is alive as long as hooks fire)
- Cost tracking (capture spend per session/agent)
- Notifications (alert on events via external channels)
- Guardrails (block dangerous operations)
- Context save (auto-save state before shutdown)

Hooks are registered per-agent or globally, and are called by the
SDK runner during the query loop.
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class HookEvent(str, Enum):
    """Events that can trigger hooks."""

    pre_tool_use = "pre_tool_use"      # Before a tool executes
    post_tool_use = "post_tool_use"    # After a tool completes
    session_start = "session_start"    # Session begins
    session_end = "session_end"        # Session ends (Stop)
    message_sent = "message_sent"      # User message sent
    message_received = "message_received"  # Assistant response received
    subagent_start = "subagent_start"  # Subagent spawned
    subagent_end = "subagent_end"      # Subagent completed
    context_compact = "context_compact"  # Before context compaction
    error = "error"                    # Error occurred


@dataclass
class HookContext:
    """Context passed to hook callbacks."""

    event: HookEvent
    agent_name: str = ""
    session_id: str = ""
    timestamp: float = field(default_factory=time.time)
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event": self.event.value,
            "agent_name": self.agent_name,
            "session_id": self.session_id,
            "timestamp": self.timestamp,
            "data": self.data,
        }


@dataclass
class ToolUseEvent:
    """Details about a tool use event."""

    tool_name: str = ""
    tool_input: dict = field(default_factory=dict)
    tool_output: str = ""
    duration_ms: int = 0
    success: bool = True
    error: str = ""

    def to_dict(self) -> dict:
        return {
            "tool_name": self.tool_name,
            "tool_input": self.tool_input,
            "tool_output": self.tool_output[:500] if self.tool_output else "",
            "duration_ms": self.duration_ms,
            "success": self.success,
            "error": self.error,
        }


@dataclass
class AuditEntry:
    """A single audit log entry."""

    id: int = 0
    agent_name: str = ""
    session_id: str = ""
    event: str = ""
    tool_name: str = ""
    tool_input_summary: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    success: bool = True
    timestamp: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "agent_name": self.agent_name,
            "session_id": self.session_id,
            "event": self.event,
            "tool_name": self.tool_name,
            "tool_input_summary": self.tool_input_summary,
            "cost_usd": self.cost_usd,
            "duration_ms": self.duration_ms,
            "success": self.success,
            "timestamp": self.timestamp,
        }


# ── Audit Store ──────────────────────────────────────────

import sqlite3


class AuditStore:
    """SQLite-backed audit trail for hook events."""

    def __init__(self, db_path: str = "data/audit.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS audit_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name TEXT NOT NULL DEFAULT '',
                session_id TEXT NOT NULL DEFAULT '',
                event TEXT NOT NULL,
                tool_name TEXT NOT NULL DEFAULT '',
                tool_input_summary TEXT NOT NULL DEFAULT '',
                cost_usd REAL NOT NULL DEFAULT 0,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                success INTEGER NOT NULL DEFAULT 1,
                timestamp REAL NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_audit_agent ON audit_log(agent_name, timestamp DESC);
            CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id);
            CREATE INDEX IF NOT EXISTS idx_audit_event ON audit_log(event);
        """)
        self._db.commit()

    def log(
        self,
        event: str,
        *,
        agent_name: str = "",
        session_id: str = "",
        tool_name: str = "",
        tool_input_summary: str = "",
        cost_usd: float = 0.0,
        duration_ms: int = 0,
        success: bool = True,
    ) -> AuditEntry:
        """Log an audit event."""
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO audit_log
               (agent_name, session_id, event, tool_name, tool_input_summary,
                cost_usd, duration_ms, success, timestamp)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (agent_name, session_id, event, tool_name,
             tool_input_summary[:500], cost_usd, duration_ms,
             int(success), now),
        )
        self._db.commit()
        return AuditEntry(
            id=cursor.lastrowid, agent_name=agent_name, session_id=session_id,
            event=event, tool_name=tool_name, tool_input_summary=tool_input_summary[:500],
            cost_usd=cost_usd, duration_ms=duration_ms, success=success, timestamp=now,
        )

    def get_log(
        self,
        *,
        agent_name: str = "",
        session_id: str = "",
        event: str = "",
        limit: int = 50,
    ) -> list[AuditEntry]:
        """Query audit log."""
        conditions = []
        params: list = []

        if agent_name:
            conditions.append("agent_name=?")
            params.append(agent_name)
        if session_id:
            conditions.append("session_id=?")
            params.append(session_id)
        if event:
            conditions.append("event=?")
            params.append(event)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.append(limit)

        rows = self._db.execute(
            f"SELECT * FROM audit_log {where} ORDER BY timestamp DESC LIMIT ?",
            params,
        ).fetchall()

        return [
            AuditEntry(
                id=r[0], agent_name=r[1], session_id=r[2], event=r[3],
                tool_name=r[4], tool_input_summary=r[5], cost_usd=r[6],
                duration_ms=r[7], success=bool(r[8]), timestamp=r[9],
            )
            for r in rows
        ]

    def get_costs(self, *, agent_name: str = "", session_id: str = "") -> dict:
        """Get cost summary."""
        conditions = ["cost_usd > 0"]
        params: list = []

        if agent_name:
            conditions.append("agent_name=?")
            params.append(agent_name)
        if session_id:
            conditions.append("session_id=?")
            params.append(session_id)

        where = f"WHERE {' AND '.join(conditions)}"

        row = self._db.execute(
            f"SELECT SUM(cost_usd), COUNT(*) FROM audit_log {where}",
            params,
        ).fetchone()

        return {
            "total_cost_usd": round(row[0] or 0, 4),
            "query_count": row[1] or 0,
        }

    def prune(self, *, max_age_days: int = 30) -> int:
        """Remove audit entries older than max_age_days."""
        cutoff = time.time() - (max_age_days * 86400)
        cursor = self._db.execute(
            "DELETE FROM audit_log WHERE timestamp < ?", (cutoff,)
        )
        self._db.commit()
        return cursor.rowcount

    def close(self) -> None:
        self._db.close()


# ── Hook Manager ─────────────────────────────────────────

class HookManager:
    """Manages hook registration and dispatch.

    Hooks are async callables that receive a HookContext.
    They can be registered globally or per-agent.
    """

    MAX_ACTIVITY_FEED = 100  # Keep last N events in memory

    def __init__(self, audit_store: AuditStore | None = None) -> None:
        self._global_hooks: dict[HookEvent, list] = {}
        self._agent_hooks: dict[str, dict[HookEvent, list]] = {}
        self._audit = audit_store
        self._activity_feed: list[dict] = []  # Live activity buffer
        self._active_agents: dict[str, dict] = {}  # Currently active agents

    def register(
        self,
        event: HookEvent,
        callback,
        *,
        agent_name: str = "",
    ) -> None:
        """Register a hook callback.

        If agent_name is provided, hook only fires for that agent.
        Otherwise it fires globally for all agents.
        """
        if agent_name:
            if agent_name not in self._agent_hooks:
                self._agent_hooks[agent_name] = {}
            if event not in self._agent_hooks[agent_name]:
                self._agent_hooks[agent_name][event] = []
            self._agent_hooks[agent_name][event].append(callback)
        else:
            if event not in self._global_hooks:
                self._global_hooks[event] = []
            self._global_hooks[event].append(callback)

    async def fire(self, context: HookContext) -> list:
        """Fire all hooks for an event. Returns list of results."""
        results = []
        hooks = list(self._global_hooks.get(context.event, []))

        # Add agent-specific hooks
        if context.agent_name and context.agent_name in self._agent_hooks:
            hooks.extend(self._agent_hooks[context.agent_name].get(context.event, []))

        for hook in hooks:
            try:
                import asyncio
                if asyncio.iscoroutinefunction(hook):
                    result = await hook(context)
                else:
                    result = hook(context)
                results.append(result)
            except Exception as e:
                _log(f"hooks: error in {context.event.value} hook: {e}")

        # Auto-audit
        if self._audit:
            tool_data = context.data.get("tool", {})
            self._audit.log(
                context.event.value,
                agent_name=context.agent_name,
                session_id=context.session_id,
                tool_name=tool_data.get("tool_name", ""),
                tool_input_summary=json.dumps(tool_data.get("tool_input", {}))[:500] if tool_data.get("tool_input") else "",
                cost_usd=context.data.get("cost_usd", 0.0),
                duration_ms=context.data.get("duration_ms", 0),
                success=context.data.get("success", True),
            )

        # Push to live activity feed
        tool_data = context.data.get("tool", {})
        feed_entry = {
            "event": context.event.value,
            "agent": context.agent_name,
            "session": context.session_id,
            "tool": tool_data.get("tool_name", ""),
            "timestamp": context.timestamp,
            "data": {
                k: v for k, v in context.data.items()
                if k not in ("tool",) and not isinstance(v, (bytes,))
            },
        }
        self._activity_feed.append(feed_entry)
        if len(self._activity_feed) > self.MAX_ACTIVITY_FEED:
            self._activity_feed = self._activity_feed[-self.MAX_ACTIVITY_FEED:]

        # Track active agents
        if context.agent_name:
            if context.event in (HookEvent.session_start, HookEvent.pre_tool_use):
                self._active_agents[context.agent_name] = {
                    "session_id": context.session_id,
                    "status": "running",
                    "current_tool": tool_data.get("tool_name", ""),
                    "subagent_count": self._active_agents.get(context.agent_name, {}).get("subagent_count", 0),
                    "last_activity": context.timestamp,
                }
            elif context.event == HookEvent.subagent_start:
                if context.agent_name in self._active_agents:
                    self._active_agents[context.agent_name]["subagent_count"] = \
                        self._active_agents[context.agent_name].get("subagent_count", 0) + 1
            elif context.event in (HookEvent.session_end, HookEvent.error):
                if context.agent_name in self._active_agents:
                    self._active_agents[context.agent_name]["status"] = "idle"
                    self._active_agents[context.agent_name]["current_tool"] = ""

        return results

    def get_activity_feed(self, *, limit: int = 50, since: float = 0.0) -> list[dict]:
        """Get recent activity feed entries.

        Args:
            limit: Max entries to return.
            since: Only return entries after this timestamp (for polling).
        """
        feed = self._activity_feed
        if since:
            feed = [e for e in feed if e["timestamp"] > since]
        return feed[-limit:]

    def get_active_agents(self) -> dict[str, dict]:
        """Get currently active agents and what they're doing."""
        return dict(self._active_agents)

    def list_hooks(self) -> dict:
        """List all registered hooks."""
        result = {"global": {}, "agents": {}}
        for event, hooks in self._global_hooks.items():
            result["global"][event.value] = len(hooks)
        for agent, events in self._agent_hooks.items():
            result["agents"][agent] = {
                event.value: len(hooks) for event, hooks in events.items()
            }
        return result


# ── Built-in Hooks ───────────────────────────────────────

def create_heartbeat_hook(registry):
    """Create a hook that auto-records heartbeats on tool use."""

    async def heartbeat_hook(ctx: HookContext):
        if ctx.agent_name:
            registry.record_heartbeat(
                ctx.agent_name,
                session_id=ctx.session_id,
                status="alive",
                context_pct=ctx.data.get("context_pct", 0.0),
                message_count=ctx.data.get("message_count", 0),
            )

    return heartbeat_hook


def create_context_save_hook(registry):
    """Create a hook that auto-saves wake context on session end."""

    async def context_save_hook(ctx: HookContext):
        if ctx.agent_name and ctx.data.get("auto_save_context"):
            registry.set_context(
                ctx.agent_name,
                task=ctx.data.get("task", ""),
                context=ctx.data.get("context", ""),
                notes=ctx.data.get("notes", ""),
                updated_by=ctx.session_id,
            )
            _log(f"hooks: auto-saved context for {ctx.agent_name}")

    return context_save_hook


def create_typing_indicator_hook(agent_registry):
    """Create a hook that sends 'typing...' to connected platforms on session_start.

    Uses bot tokens from agent registry to send chat actions.
    """
    import asyncio
    import urllib.error
    import urllib.request

    def send_telegram_typing_sync(bot_token: str, chat_id: str) -> None:
        """Send typing indicator to Telegram (sync, runs in thread)."""
        url = f"https://api.telegram.org/bot{bot_token}/sendChatAction"
        data = json.dumps({"chat_id": chat_id, "action": "typing"}).encode()
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"})
        try:
            urllib.request.urlopen(req, timeout=5)
        except Exception:
            pass

    async def typing_hook(ctx: HookContext) -> None:
        if not ctx.agent_name:
            return

        # Get telegram token for this agent
        raw_token = agent_registry.get_raw_token(ctx.agent_name, "telegram")
        if not raw_token:
            return

        # Get token settings for chat_id
        token_config = agent_registry.get_token(ctx.agent_name, "telegram")
        chat_ids = []
        if token_config and token_config.settings:
            chat_ids = token_config.settings.get("chat_ids", [])

        # Send typing in background thread (non-blocking)
        loop = asyncio.get_event_loop()
        for chat_id in chat_ids:
            loop.run_in_executor(None, send_telegram_typing_sync, raw_token, str(chat_id))

    return typing_hook


def create_cost_tracker_hook():
    """Create a hook that logs costs from ResultMessages."""

    costs: dict[str, float] = {}  # agent -> total cost

    async def cost_hook(ctx: HookContext):
        cost = ctx.data.get("cost_usd", 0.0)
        if cost > 0 and ctx.agent_name:
            costs[ctx.agent_name] = costs.get(ctx.agent_name, 0) + cost
            _log(f"hooks: {ctx.agent_name} cost +${cost:.4f} (total: ${costs[ctx.agent_name]:.4f})")

    cost_hook.costs = costs  # type: ignore
    return cost_hook
