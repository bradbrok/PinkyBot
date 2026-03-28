"""Pinky API — stateful HTTP server for Claude Code sessions.

FastAPI server that manages Claude Code sessions via REST endpoints.
Each session maintains its own conversation context, MCP connections,
and tool permissions.

Usage:
    python -m pinky_daemon --mode api --port 8888
    curl -X POST http://localhost:8888/sessions -d '{"model": "sonnet"}'
    curl -X POST http://localhost:8888/sessions/{id}/message -d '{"content": "Hello"}'
"""

from __future__ import annotations

import os
import sys
import uuid
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pinky_daemon.agent_comms import AgentComms
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.hooks import (
    AuditStore,
    HookEvent,
    HookManager,
    create_cost_tracker_hook,
    create_heartbeat_hook,
    create_context_save_hook,
    create_typing_indicator_hook,
)
from pinky_daemon.autonomy import AutonomyEngine, AgentEvent, EventType
from pinky_daemon.outreach_config import OutreachConfigStore
from pinky_daemon.scheduler import AgentScheduler
from pinky_daemon.session_store import SessionStore
from pinky_daemon.sessions import SessionManager, SessionState
from pinky_daemon.skill_store import SkillStore
from pinky_daemon.task_store import TaskStore


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Request/Response Models ──────────────────────────────────


VALID_PERMISSION_MODES = ("", "default", "acceptEdits", "bypassPermissions", "dontAsk", "plan", "auto")


class CreateSessionRequest(BaseModel):
    """Create a new Claude Code session."""

    model: str = ""
    soul: str = ""  # Inline soul text, or path to CLAUDE.md
    working_dir: str = "."
    allowed_tools: list[str] = Field(default_factory=lambda: [
        "mcp__memory__*",
        "mcp__outreach__*",
        "Read",
        "Glob",
        "Grep",
    ])
    max_turns: int = 25
    timeout: float = 300.0
    system_prompt: str = ""
    session_id: str = ""
    restart_threshold_pct: float = 80.0
    auto_restart: bool = True
    permission_mode: str = ""  # default, acceptEdits, bypassPermissions, dontAsk, plan, auto


class SendMessageRequest(BaseModel):
    """Send a message to a session."""

    content: str


class SessionResponse(BaseModel):
    """Session info returned by API."""

    id: str
    state: str
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


class ContextResponse(BaseModel):
    """Context window status."""

    session_id: str
    estimated_tokens: int
    max_tokens: int
    context_used_pct: float
    message_count: int
    needs_restart: bool
    restart_threshold_pct: float
    checkpoints: int
    last_checkpoint_at: float | None


class RestartResponse(BaseModel):
    """Result of a session restart."""

    session_id: str
    checkpoint_summary: str
    messages_checkpointed: int
    tokens_at_checkpoint: int
    restart_number: int


class MessageResponse(BaseModel):
    """Response from sending a message."""

    role: str
    content: str
    timestamp: float
    duration_ms: int
    error: str


class HistoryResponse(BaseModel):
    """Conversation history."""

    session_id: str
    messages: list[dict]
    count: int


class SearchResponse(BaseModel):
    """Search results."""

    query: str
    results: list[dict]
    count: int


class ConversationListResponse(BaseModel):
    """List of conversations."""

    conversations: list[dict]
    count: int


# ── Agent Comms Models ───────────────────────────────────────


class SendAgentMessageRequest(BaseModel):
    """Send a message to another agent/session."""

    to: str  # session_id, group name, or "*" for broadcast
    content: str
    metadata: dict = Field(default_factory=dict)


class CreateGroupRequest(BaseModel):
    """Create a named agent group."""

    name: str
    members: list[str]


class JoinGroupRequest(BaseModel):
    """Join a group."""

    session_id: str


# ── Skill Models ────────────────────────────────────────────


class RegisterSkillRequest(BaseModel):
    """Register a new skill/plugin."""

    name: str
    description: str = ""
    skill_type: str = "custom"
    version: str = "0.1.0"
    enabled: bool = True
    config: dict = Field(default_factory=dict)


class UpdateSkillRequest(BaseModel):
    """Update an existing skill."""

    description: str | None = None
    skill_type: str | None = None
    version: str | None = None
    enabled: bool | None = None
    config: dict | None = None


class SessionSkillRequest(BaseModel):
    """Enable/disable a skill for a session."""

    enabled: bool


# ── Outreach Config Models ──────────────────────────────────


class ConfigurePlatformRequest(BaseModel):
    """Configure a messaging platform."""

    token: str = ""
    enabled: bool | None = None
    settings: dict = Field(default_factory=dict)


# ── Agent Models ─────────────────────────────────────────────


class RegisterAgentRequest(BaseModel):
    """Register a named agent."""

    name: str
    display_name: str = ""
    model: str = "opus"
    soul: str = ""
    system_prompt: str = ""
    working_dir: str = "."
    permission_mode: str = "auto"
    allowed_tools: list[str] = Field(default_factory=list)
    max_turns: int = 25
    timeout: float = 300.0
    max_sessions: int = 5
    groups: list[str] = Field(default_factory=list)


class UpdateAgentRequest(BaseModel):
    """Update an agent's config."""

    display_name: str | None = None
    model: str | None = None
    soul: str | None = None
    system_prompt: str | None = None
    working_dir: str | None = None
    permission_mode: str | None = None
    allowed_tools: list[str] | None = None
    max_turns: int | None = None
    timeout: float | None = None
    max_sessions: int | None = None
    groups: list[str] | None = None
    enabled: bool | None = None


class AddDirectiveRequest(BaseModel):
    """Add a directive to an agent."""

    directive: str
    priority: int = 0


class SetAgentTokenRequest(BaseModel):
    """Set a bot token for an agent."""

    token: str
    enabled: bool = True
    settings: dict = Field(default_factory=dict)


class SpawnSessionRequest(BaseModel):
    """Spawn a new session from an agent's config."""

    session_id: str = ""  # Auto-generated if empty
    session_type: str = "chat"  # main, worker, chat


class CreateTaskRequest(BaseModel):
    title: str
    project_id: int = 0
    description: str = ""
    status: str = "pending"
    priority: str = "normal"
    assigned_agent: str = ""
    created_by: str = ""
    tags: list[str] = Field(default_factory=list)
    due_date: str = ""
    parent_id: int = 0
    blocked_by: list[int] = Field(default_factory=list)


class UpdateTaskRequest(BaseModel):
    title: str | None = None
    project_id: int | None = None
    description: str | None = None
    status: str | None = None
    priority: str | None = None
    assigned_agent: str | None = None
    tags: list[str] | None = None
    due_date: str | None = None
    parent_id: int | None = None
    blocked_by: list[int] | None = None


class CreateProjectRequest(BaseModel):
    name: str
    description: str = ""


class AddCommentRequest(BaseModel):
    author: str = ""
    content: str


# ── API Server ───────────────────────────────────────────────


def create_api(
    *,
    max_sessions: int = 50,
    default_working_dir: str = ".",
    db_path: str = "data/conversations.db",
) -> FastAPI:
    """Create the FastAPI application."""

    app = FastAPI(
        title="Pinky",
        description="Stateful Claude Code session API",
        version="0.1.0",
    )

    session_store = SessionStore(db_path=db_path.replace(".db", "_sessions.db"))
    store = ConversationStore(db_path=db_path)
    agents = AgentRegistry(db_path=db_path.replace(".db", "_agents.db"))
    audit = AuditStore(db_path=db_path.replace(".db", "_audit.db"))
    hooks = HookManager(audit_store=audit)

    # Register built-in hooks
    hooks.register(HookEvent.post_tool_use, create_heartbeat_hook(agents))
    hooks.register(HookEvent.session_end, create_context_save_hook(agents))
    hooks.register(HookEvent.session_end, create_cost_tracker_hook())
    hooks.register(HookEvent.session_start, create_typing_indicator_hook(agents))

    manager = SessionManager(
        max_sessions=max_sessions, store=session_store,
        conversation_store=store, agent_registry=agents,
        hook_manager=hooks,
    )
    comms = AgentComms(db_path=db_path.replace(".db", "_comms.db"))
    skills = SkillStore(db_path=db_path.replace(".db", "_skills.db"))
    outreach_config = OutreachConfigStore(db_path=db_path.replace(".db", "_outreach.db"))
    tasks = TaskStore(db_path=db_path.replace(".db", "_tasks.db"))

    # Serve frontend
    frontend_dir = Path(__file__).parent.parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

    @app.get("/api")
    async def api_info():
        """Health check and server info (JSON)."""
        return {
            "name": "pinky",
            "version": "0.1.0",
            "sessions": manager.count,
        }

    # Keep the old / endpoint as an alias for backwards compat
    @app.get("/")
    async def root():
        """Dashboard — serves the main UI or falls back to JSON health check."""
        dash_path = frontend_dir / "dashboard.html" if frontend_dir.exists() else None
        if dash_path and dash_path.exists():
            return FileResponse(str(dash_path))
        return {
            "name": "pinky",
            "version": "0.1.0",
            "sessions": manager.count,
        }

    @app.get("/dashboard", response_class=HTMLResponse)
    async def dashboard_ui():
        """Serve the main dashboard."""
        dash_path = frontend_dir / "dashboard.html" if frontend_dir.exists() else None
        if dash_path and dash_path.exists():
            return FileResponse(str(dash_path))
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    @app.get("/chat", response_class=HTMLResponse)
    async def chat_ui():
        """Serve the chat frontend."""
        chat_path = frontend_dir / "chat.html" if frontend_dir.exists() else None
        if chat_path and chat_path.exists():
            return FileResponse(str(chat_path))
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    @app.get("/fleet", response_class=HTMLResponse)
    async def fleet_ui():
        """Serve the fleet management dashboard."""
        fleet_path = frontend_dir / "fleet.html" if frontend_dir.exists() else None
        if fleet_path and fleet_path.exists():
            return FileResponse(str(fleet_path))
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    @app.get("/agents-ui", response_class=HTMLResponse)
    async def agents_ui():
        """Serve the agents management page."""
        agents_path = frontend_dir / "agents.html" if frontend_dir.exists() else None
        if agents_path and agents_path.exists():
            return FileResponse(str(agents_path))
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    @app.get("/settings", response_class=HTMLResponse)
    async def settings_ui():
        """Serve the settings page."""
        settings_path = frontend_dir / "settings.html" if frontend_dir.exists() else None
        if settings_path and settings_path.exists():
            return FileResponse(str(settings_path))
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    @app.post("/sessions", response_model=SessionResponse)
    async def create_session(req: CreateSessionRequest):
        """Create a new Claude Code session.

        Returns session info including the session ID for subsequent calls.
        """
        # Resolve soul: if it's a file path, read it
        soul_text = req.soul
        system_prompt = req.system_prompt

        if req.soul and not req.soul.startswith("#"):
            # Looks like a file path
            soul_path = Path(req.soul)
            if soul_path.exists():
                soul_text = soul_path.read_text()
                if not system_prompt:
                    system_prompt = soul_text

        if req.permission_mode and req.permission_mode not in VALID_PERMISSION_MODES:
            raise HTTPException(400, f"Invalid permission_mode '{req.permission_mode}'. Valid: {', '.join(m for m in VALID_PERMISSION_MODES if m)}")

        working_dir = req.working_dir or default_working_dir

        session = manager.create(
            session_id=req.session_id,
            model=req.model,
            soul=soul_text,
            working_dir=working_dir,
            allowed_tools=req.allowed_tools,
            max_turns=req.max_turns,
            timeout=req.timeout,
            system_prompt=system_prompt,
            restart_threshold_pct=req.restart_threshold_pct,
            auto_restart=req.auto_restart,
            permission_mode=req.permission_mode,
        )

        _log(f"api: created session {session.id}")
        info = session.info
        return SessionResponse(**info.to_dict())

    @app.get("/sessions", response_model=list[SessionResponse])
    async def list_sessions():
        """List all active sessions."""
        return [SessionResponse(**s.to_dict()) for s in manager.list()]

    @app.get("/sessions/{session_id}", response_model=SessionResponse)
    async def get_session(session_id: str):
        """Get session info."""
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")
        return SessionResponse(**session.info.to_dict())

    @app.delete("/sessions/{session_id}")
    async def delete_session(session_id: str):
        """Delete a session and free resources."""
        deleted = manager.delete(session_id)
        if not deleted:
            raise HTTPException(404, f"Session '{session_id}' not found")
        _log(f"api: deleted session {session_id}")
        return {"deleted": True, "session_id": session_id}

    @app.post("/sessions/{session_id}/message", response_model=MessageResponse)
    async def send_message(session_id: str, req: SendMessageRequest):
        """Send a message to a session and get the response.

        The session maintains conversation context across calls.
        Each call resumes the previous Claude Code session.
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        if session.state == SessionState.closed:
            raise HTTPException(410, f"Session '{session_id}' is closed")

        if session.state == SessionState.running:
            raise HTTPException(409, f"Session '{session_id}' is busy")

        _log(f"api: message to {session_id}: {req.content[:80]}...")

        # Log user message to conversation store
        store.append(session_id, "user", req.content)

        msg = await session.send(req.content)

        # Log assistant response to conversation store
        if msg.content:
            store.append(session_id, "assistant", msg.content)

        return MessageResponse(
            role=msg.role,
            content=msg.content,
            timestamp=msg.timestamp,
            duration_ms=msg.duration_ms,
            error=msg.error,
        )

    @app.get("/sessions/{session_id}/history", response_model=HistoryResponse)
    async def get_history(session_id: str, limit: int = 50):
        """Get conversation history for a session.

        Returns in-memory history if available, otherwise falls back
        to the persistent conversation store (survives server restarts).
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        history = session.get_history(limit=limit)

        # Fall back to persistent conversation store if in-memory is empty
        if not history:
            messages = store.get_history(session_id, limit=limit)
            history = [m.to_dict() for m in messages]

        return HistoryResponse(
            session_id=session_id,
            messages=history,
            count=len(history),
        )

    @app.get("/sessions/{session_id}/context", response_model=ContextResponse)
    async def get_context(session_id: str):
        """Get context window status for a session.

        Shows estimated token usage, whether a restart is needed,
        and checkpoint history.
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        status = session.get_context_status()
        return ContextResponse(**status.to_dict())

    # ── Conversation Store Endpoints ──────────────────────────

    @app.get("/conversations", response_model=ConversationListResponse)
    async def list_conversations(platform: str = "", limit: int = 50):
        """List all conversations grouped by session.

        Returns session IDs with message counts and timestamps.
        """
        convos = store.list_conversations(platform=platform, limit=limit)
        return ConversationListResponse(
            conversations=[c.to_dict() for c in convos],
            count=len(convos),
        )

    @app.get("/conversations/search", response_model=SearchResponse)
    async def search_conversations(
        q: str,
        session_id: str = "",
        platform: str = "",
        chat_id: str = "",
        limit: int = 50,
    ):
        """Full-text search across all conversations.

        Searches message content using FTS5. Supports boolean operators
        (AND, OR, NOT) and phrase matching ("exact phrase").
        """
        if not q:
            raise HTTPException(400, "Query parameter 'q' is required")

        results = store.search(
            q,
            session_id=session_id,
            platform=platform,
            chat_id=chat_id,
            limit=limit,
        )
        return SearchResponse(
            query=q,
            results=[r.to_dict() for r in results],
            count=len(results),
        )

    @app.get("/conversations/{session_id}", response_model=HistoryResponse)
    async def get_conversation(session_id: str, limit: int = 50, offset: int = 0):
        """Get persisted conversation history for a session.

        Unlike /sessions/{id}/history (in-memory), this reads from
        the persistent SQLite store and survives server restarts.
        """
        messages = store.get_history(session_id, limit=limit, offset=offset)
        return HistoryResponse(
            session_id=session_id,
            messages=[m.to_dict() for m in messages],
            count=len(messages),
        )

    # ── Session Management (continued) ───────────────────────

    @app.post("/sessions/{session_id}/restart", response_model=RestartResponse)
    async def restart_session(session_id: str):
        """Force a context restart with checkpoint.

        Saves a summary of the current conversation, resets the
        Claude Code session, and prepares for fresh messages.
        The session ID stays the same — callers don't notice.
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        if session.state == SessionState.closed:
            raise HTTPException(410, f"Session '{session_id}' is closed")

        if session.state == SessionState.running:
            raise HTTPException(409, f"Session '{session_id}' is busy")

        _log(f"api: manual restart for {session_id}")
        checkpoint = await session.restart()

        return RestartResponse(
            session_id=session_id,
            checkpoint_summary=checkpoint.summary[:500],
            messages_checkpointed=checkpoint.message_count,
            tokens_at_checkpoint=checkpoint.estimated_tokens_at_checkpoint,
            restart_number=session._restart_count,
        )

    # ── Agent Communication Endpoints ────────────────────────

    @app.post("/sessions/{session_id}/send")
    async def send_agent_message(session_id: str, req: SendAgentMessageRequest):
        """Send a message from one session to another.

        Supports direct (to=session_id), group (to=group_name),
        and broadcast (to="*") messaging.
        """
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        if req.to == "*":
            # Broadcast
            active = [s.id for s in manager.list()]
            msg = comms.broadcast(session_id, req.content, active_sessions=active, metadata=req.metadata)
        elif comms.get_group_members(req.to):
            # Group message
            msg = comms.send_group(session_id, req.to, req.content, metadata=req.metadata)
        else:
            # Direct message
            msg = comms.send(session_id, req.to, req.content, metadata=req.metadata)

        return msg.to_dict()

    @app.get("/sessions/{session_id}/inbox")
    async def get_inbox(session_id: str, unread_only: bool = True, limit: int = 50):
        """Get a session's inbox — messages from other agents."""
        messages = comms.get_inbox(session_id, unread_only=unread_only, limit=limit)
        return {
            "session_id": session_id,
            "messages": [m.to_dict() for m in messages],
            "count": len(messages),
            "unread": comms.unread_count(session_id),
        }

    @app.post("/sessions/{session_id}/inbox/read")
    async def mark_inbox_read(session_id: str, message_ids: list[int] | None = None):
        """Mark messages as read. Pass message_ids or omit to mark all."""
        count = comms.mark_read(session_id, message_ids)
        return {"marked": count}

    @app.post("/groups")
    async def create_group(req: CreateGroupRequest):
        """Create a named group of agents."""
        result = comms.create_group(req.name, req.members)
        return result

    @app.get("/groups")
    async def list_groups():
        """List all agent groups."""
        return {"groups": comms.list_groups()}

    @app.get("/groups/{name}")
    async def get_group(name: str):
        """Get group members."""
        members = comms.get_group_members(name)
        if not members:
            raise HTTPException(404, f"Group '{name}' not found")
        return {"name": name, "members": members}

    @app.post("/groups/{name}/join")
    async def join_group(name: str, req: JoinGroupRequest):
        """Add a session to a group."""
        comms.join_group(name, req.session_id)
        return {"joined": True, "group": name, "session_id": req.session_id}

    @app.post("/groups/{name}/leave")
    async def leave_group(name: str, req: JoinGroupRequest):
        """Remove a session from a group."""
        left = comms.leave_group(name, req.session_id)
        if not left:
            raise HTTPException(404, "Not a member")
        return {"left": True, "group": name, "session_id": req.session_id}

    # ── Skill Management Endpoints ──────────────────────────

    @app.post("/skills")
    async def register_skill(req: RegisterSkillRequest):
        """Register a new skill or update an existing one."""
        skill = skills.register(
            req.name,
            description=req.description,
            skill_type=req.skill_type,
            version=req.version,
            enabled=req.enabled,
            config=req.config,
        )
        return skill.to_dict()

    @app.get("/skills")
    async def list_skills(skill_type: str = "", enabled_only: bool = False):
        """List all registered skills."""
        result = skills.list(skill_type=skill_type, enabled_only=enabled_only)
        return {"skills": [s.to_dict() for s in result], "count": len(result)}

    @app.get("/skills/{name}")
    async def get_skill(name: str):
        """Get a skill by name."""
        skill = skills.get(name)
        if not skill:
            raise HTTPException(404, f"Skill '{name}' not found")
        return skill.to_dict()

    @app.put("/skills/{name}")
    async def update_skill(name: str, req: UpdateSkillRequest):
        """Update an existing skill's properties."""
        existing = skills.get(name)
        if not existing:
            raise HTTPException(404, f"Skill '{name}' not found")

        skill = skills.register(
            name,
            description=req.description if req.description is not None else existing.description,
            skill_type=req.skill_type if req.skill_type is not None else existing.skill_type,
            version=req.version if req.version is not None else existing.version,
            enabled=req.enabled if req.enabled is not None else existing.enabled,
            config=req.config if req.config is not None else existing.config,
        )
        return skill.to_dict()

    @app.delete("/skills/{name}")
    async def delete_skill(name: str):
        """Unregister a skill."""
        deleted = skills.delete(name)
        if not deleted:
            raise HTTPException(404, f"Skill '{name}' not found")
        return {"deleted": True, "name": name}

    @app.post("/skills/{name}/enable")
    async def enable_skill(name: str):
        """Enable a skill globally."""
        if not skills.enable(name):
            raise HTTPException(404, f"Skill '{name}' not found")
        return {"enabled": True, "name": name}

    @app.post("/skills/{name}/disable")
    async def disable_skill(name: str):
        """Disable a skill globally."""
        if not skills.disable(name):
            raise HTTPException(404, f"Skill '{name}' not found")
        return {"disabled": True, "name": name}

    @app.get("/sessions/{session_id}/skills")
    async def get_session_skills(session_id: str):
        """Get skills for a session with effective enabled state."""
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")
        result = skills.get_session_skills(session_id)
        return {"session_id": session_id, "skills": result, "count": len(result)}

    @app.put("/sessions/{session_id}/skills/{skill_name}")
    async def set_session_skill(session_id: str, skill_name: str, req: SessionSkillRequest):
        """Enable or disable a skill for a specific session."""
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        if req.enabled:
            ok = skills.enable_for_session(session_id, skill_name)
        else:
            ok = skills.disable_for_session(session_id, skill_name)

        if not ok:
            raise HTTPException(404, f"Skill '{skill_name}' not found")
        return {"session_id": session_id, "skill": skill_name, "enabled": req.enabled}

    @app.delete("/sessions/{session_id}/skills/{skill_name}")
    async def clear_session_skill_override(session_id: str, skill_name: str):
        """Remove per-session override, reverting to global default."""
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")
        skills.clear_session_override(session_id, skill_name)
        return {"session_id": session_id, "skill": skill_name, "override_cleared": True}

    # ── Outreach Configuration Endpoints ────────────────────

    @app.get("/outreach/platforms")
    async def list_platforms():
        """List all configured outreach platforms."""
        result = outreach_config.list()
        return {"platforms": [p.to_dict() for p in result], "count": len(result)}

    @app.get("/outreach/platforms/{platform}")
    async def get_platform(platform: str):
        """Get platform configuration (token is never exposed)."""
        config = outreach_config.get(platform)
        if not config:
            raise HTTPException(404, f"Platform '{platform}' not configured")
        return config.to_dict()

    @app.put("/outreach/platforms/{platform}")
    async def configure_platform(platform: str, req: ConfigurePlatformRequest):
        """Configure or update a messaging platform.

        Set token, enabled state, and platform-specific settings.
        Token is stored securely and never returned in API responses.
        """
        try:
            config = outreach_config.configure(
                platform,
                token=req.token,
                enabled=req.enabled,
                settings=req.settings if req.settings else None,
            )
            return config.to_dict()
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.post("/outreach/platforms/{platform}/enable")
    async def enable_platform(platform: str):
        """Enable an outreach platform."""
        if not outreach_config.enable(platform):
            raise HTTPException(404, f"Platform '{platform}' not configured")
        return {"enabled": True, "platform": platform}

    @app.post("/outreach/platforms/{platform}/disable")
    async def disable_platform(platform: str):
        """Disable an outreach platform."""
        if not outreach_config.disable(platform):
            raise HTTPException(404, f"Platform '{platform}' not configured")
        return {"disabled": True, "platform": platform}

    @app.post("/outreach/platforms/{platform}/test")
    async def test_platform(platform: str):
        """Test connectivity to a platform.

        Attempts to call the platform's API with the stored token
        and returns success/error info.
        """
        result = outreach_config.test_connection(platform)
        return result

    @app.delete("/outreach/platforms/{platform}")
    async def delete_platform(platform: str):
        """Remove a platform configuration entirely."""
        deleted = outreach_config.delete(platform)
        if not deleted:
            raise HTTPException(404, f"Platform '{platform}' not configured")
        return {"deleted": True, "platform": platform}

    # ── Agent Registry Endpoints ────────────────────────────

    @app.post("/agents")
    async def register_agent(req: RegisterAgentRequest):
        """Register a new agent or update an existing one."""
        agent = agents.register(
            req.name,
            display_name=req.display_name,
            model=req.model,
            soul=req.soul,
            system_prompt=req.system_prompt,
            working_dir=req.working_dir,
            permission_mode=req.permission_mode,
            allowed_tools=req.allowed_tools,
            max_turns=req.max_turns,
            timeout=req.timeout,
            max_sessions=req.max_sessions,
            groups=req.groups,
        )
        return agent.to_dict()

    @app.get("/agents")
    async def list_agents(group: str = "", enabled_only: bool = False):
        """List all registered agents."""
        result = agents.list(group=group, enabled_only=enabled_only)
        return {"agents": [a.to_dict() for a in result], "count": len(result)}

    @app.get("/agents/{name}")
    async def get_agent(name: str):
        """Get an agent by name."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        return agent.to_dict()

    @app.put("/agents/{name}")
    async def update_agent(name: str, req: UpdateAgentRequest):
        """Update an agent's configuration."""
        existing = agents.get(name)
        if not existing:
            raise HTTPException(404, f"Agent '{name}' not found")

        kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
        agent = agents.register(name, **kwargs)
        return agent.to_dict()

    @app.delete("/agents/{name}")
    async def delete_agent(name: str):
        """Delete an agent and all its directives/tokens."""
        deleted = agents.delete(name)
        if not deleted:
            raise HTTPException(404, f"Agent '{name}' not found")
        return {"deleted": True, "name": name}

    # ── Agent Directives ────────────────────────────────────

    @app.post("/agents/{name}/directives")
    async def add_directive(name: str, req: AddDirectiveRequest):
        """Add a persistent directive to an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        directive = agents.add_directive(name, req.directive, priority=req.priority)
        return directive.to_dict()

    @app.get("/agents/{name}/directives")
    async def get_directives(name: str, active_only: bool = True):
        """Get all directives for an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        result = agents.get_directives(name, active_only=active_only)
        return {"agent": name, "directives": [d.to_dict() for d in result], "count": len(result)}

    @app.delete("/agents/{name}/directives/{directive_id}")
    async def remove_directive(name: str, directive_id: int):
        """Remove a directive."""
        if not agents.remove_directive(directive_id):
            raise HTTPException(404, "Directive not found")
        return {"deleted": True, "id": directive_id}

    @app.post("/agents/{name}/directives/{directive_id}/toggle")
    async def toggle_directive(name: str, directive_id: int, active: bool = True):
        """Enable or disable a directive."""
        if not agents.toggle_directive(directive_id, active):
            raise HTTPException(404, "Directive not found")
        return {"id": directive_id, "active": active}

    # ── Agent Tokens ────────────────────────────────────────

    @app.put("/agents/{name}/tokens/{platform}")
    async def set_agent_token(name: str, platform: str, req: SetAgentTokenRequest):
        """Set a bot token for an agent on a platform."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        token = agents.set_token(name, platform, req.token, enabled=req.enabled, settings=req.settings)
        return token.to_dict()

    @app.get("/agents/{name}/tokens")
    async def list_agent_tokens(name: str):
        """List all tokens for an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        result = agents.list_tokens(name)
        return {"agent": name, "tokens": [t.to_dict() for t in result], "count": len(result)}

    @app.delete("/agents/{name}/tokens/{platform}")
    async def remove_agent_token(name: str, platform: str):
        """Remove a bot token for an agent."""
        if not agents.remove_token(name, platform):
            raise HTTPException(404, "Token not found")
        return {"deleted": True, "agent": name, "platform": platform}

    # ── Spawn Session from Agent ────────────────────────────

    # ── Agent Heart Files ─────────────────────────────────

    @app.get("/agents/{name}/files")
    async def list_agent_files(name: str):
        """List heart files in the agent's working directory."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        work_dir = Path(agent.working_dir).resolve()
        if not work_dir.exists():
            return {"agent": name, "working_dir": str(work_dir), "files": [], "exists": False}
        # Heart file extensions — config, soul, and docs
        heart_exts = {".md", ".yaml", ".yml", ".toml", ".json", ".txt", ".cfg", ".ini"}
        files = []
        for f in sorted(work_dir.iterdir()):
            if f.is_file() and not f.name.startswith('.') and (f.suffix in heart_exts or f.name == "CLAUDE.md"):
                files.append({
                    "name": f.name,
                    "size": f.stat().st_size,
                    "modified": f.stat().st_mtime,
                    "is_claude_md": f.name == "CLAUDE.md",
                })
        return {"agent": name, "working_dir": str(work_dir), "files": files, "exists": True}

    @app.get("/agents/{name}/files/{filename}")
    async def read_agent_file(name: str, filename: str):
        """Read a heart file from the agent's working directory."""
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        file_path = Path(agent.working_dir).resolve() / filename
        if not file_path.exists() or not file_path.is_file():
            raise HTTPException(404, f"File '{filename}' not found")
        # Safety: don't serve files outside the working dir
        if not str(file_path).startswith(str(Path(agent.working_dir).resolve())):
            raise HTTPException(403, "Access denied")
        return {"name": filename, "content": file_path.read_text(errors="replace")}

    @app.put("/agents/{name}/files/{filename}")
    async def write_agent_file(name: str, filename: str, req: SendMessageRequest):
        """Write a heart file to the agent's working directory.

        Uses content field for file contents.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        work_dir = Path(agent.working_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        file_path = work_dir / filename
        # Safety check
        if not str(file_path.resolve()).startswith(str(work_dir)):
            raise HTTPException(403, "Access denied")
        file_path.write_text(req.content)
        _log(f"api: wrote {filename} to {work_dir} for agent {name}")
        return {"written": True, "name": filename, "size": len(req.content)}

    # ── Spawn Session from Agent ────────────────────────────

    @app.post("/agents/{name}/sessions")
    async def spawn_agent_session(name: str, req: SpawnSessionRequest):
        """Spawn a new session from an agent's config.

        Creates a session pre-configured with the agent's model, soul,
        tools, permissions, and active directives. Writes CLAUDE.md to
        the agent's working directory before spawning.
        """
        agent = agents.get(name)
        if not agent:
            raise HTTPException(404, f"Agent '{name}' not found")
        if not agent.enabled:
            raise HTTPException(400, f"Agent '{name}' is disabled")

        # Build system prompt from agent config + directives
        system_prompt = agents.build_system_prompt(name)

        # Ensure working directory exists and write CLAUDE.md
        work_dir = Path(agent.working_dir or default_working_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        claude_md = work_dir / "CLAUDE.md"
        claude_md.write_text(system_prompt)
        _log(f"api: wrote CLAUDE.md ({len(system_prompt)} chars) to {work_dir}")

        session_type = req.session_type or "chat"
        if session_type == "main":
            session_id = req.session_id or f"{name}-main"
        else:
            session_id = req.session_id or f"{name}-{uuid.uuid4().hex[:8]}"

        session = manager.create(
            session_id=session_id,
            model=agent.model,
            soul=agent.soul,
            working_dir=str(work_dir),
            allowed_tools=agent.allowed_tools or None,
            max_turns=agent.max_turns,
            timeout=agent.timeout,
            system_prompt=system_prompt,
            restart_threshold_pct=agent.restart_threshold_pct,
            auto_restart=agent.auto_restart,
            permission_mode=agent.permission_mode,
            session_type=session_type,
            agent_name=name,
        )

        _log(f"api: spawned {session_type} session {session.id} from agent {name}")
        info = session.info
        return {
            "agent": name,
            "session": SessionResponse(**info.to_dict()).model_dump(),
        }

    @app.get("/agents/{name}/sessions")
    async def list_agent_sessions(name: str):
        """List all active sessions spawned from an agent."""
        if not agents.get(name):
            raise HTTPException(404, f"Agent '{name}' not found")
        all_sessions = manager.list()
        agent_sessions = [s for s in all_sessions if s.agent_name == name or s.id.startswith(f"{name}-")]
        return {
            "agent": name,
            "sessions": [SessionResponse(**s.to_dict()).model_dump() for s in agent_sessions],
            "count": len(agent_sessions),
        }

    # ── Schedule Endpoints ─────────────────────────────────

    class AddScheduleRequest(BaseModel):
        name: str = ""
        cron: str
        prompt: str = ""
        timezone: str = "America/Los_Angeles"

    @app.post("/agents/{agent_name}/schedules")
    async def add_schedule(agent_name: str, req: AddScheduleRequest):
        """Add a cron-based wake schedule for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        schedule = agents.add_schedule(
            agent_name, req.cron,
            name=req.name, prompt=req.prompt, timezone=req.timezone,
        )
        return schedule.to_dict()

    @app.get("/agents/{agent_name}/schedules")
    async def list_schedules(agent_name: str, enabled_only: bool = True):
        """List all schedules for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        schedules = agents.get_schedules(agent_name, enabled_only=enabled_only)
        return {
            "agent": agent_name,
            "schedules": [s.to_dict() for s in schedules],
            "count": len(schedules),
        }

    @app.delete("/agents/{agent_name}/schedules/{schedule_id}")
    async def remove_schedule(agent_name: str, schedule_id: int):
        """Remove a schedule."""
        if not agents.remove_schedule(schedule_id):
            raise HTTPException(404, f"Schedule {schedule_id} not found")
        return {"deleted": True}

    @app.post("/agents/{agent_name}/schedules/{schedule_id}/toggle")
    async def toggle_schedule(agent_name: str, schedule_id: int, enabled: bool = True):
        """Enable/disable a schedule."""
        if not agents.toggle_schedule(schedule_id, enabled):
            raise HTTPException(404, f"Schedule {schedule_id} not found")
        return {"toggled": True, "enabled": enabled}

    @app.get("/schedules")
    async def list_all_schedules(enabled_only: bool = True):
        """List all schedules across all agents."""
        schedules = agents.get_all_schedules(enabled_only=enabled_only)
        return {
            "schedules": [s.to_dict() for s in schedules],
            "count": len(schedules),
        }

    # ── Heartbeat Endpoints ────────────────────────────────

    class RecordHeartbeatRequest(BaseModel):
        session_id: str = ""
        status: str = "alive"
        context_pct: float = 0.0
        message_count: int = 0
        metadata: dict = Field(default_factory=dict)

    @app.post("/agents/{agent_name}/heartbeat")
    async def record_heartbeat(agent_name: str, req: RecordHeartbeatRequest):
        """Record a heartbeat for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        hb = agents.record_heartbeat(
            agent_name,
            session_id=req.session_id, status=req.status,
            context_pct=req.context_pct, message_count=req.message_count,
            metadata=req.metadata,
        )
        return hb.to_dict()

    @app.get("/agents/{agent_name}/heartbeats")
    async def get_heartbeats(agent_name: str, limit: int = 20):
        """Get recent heartbeats for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        heartbeats = agents.get_heartbeats(agent_name, limit=limit)
        return {
            "agent": agent_name,
            "heartbeats": [h.to_dict() for h in heartbeats],
            "count": len(heartbeats),
        }

    @app.get("/agents/{agent_name}/heartbeat")
    async def get_latest_heartbeat(agent_name: str):
        """Get the most recent heartbeat for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        hb = agents.get_latest_heartbeat(agent_name)
        if not hb:
            return {"agent": agent_name, "heartbeat": None}
        return {"agent": agent_name, "heartbeat": hb.to_dict()}

    @app.get("/heartbeats")
    async def get_all_heartbeats():
        """Get latest heartbeat for every agent."""
        heartbeats = agents.get_all_latest_heartbeats()
        return {
            "heartbeats": [h.to_dict() for h in heartbeats],
            "count": len(heartbeats),
        }

    # ── Agent Context (continuation state) ──────────────────

    class SetContextRequest(BaseModel):
        task: str = ""
        context: str = ""
        notes: str = ""
        blockers: list[str] = Field(default_factory=list)
        priority_items: list[str] = Field(default_factory=list)
        metadata: dict = Field(default_factory=dict)

    @app.put("/agents/{agent_name}/context")
    async def set_agent_context(agent_name: str, req: SetContextRequest):
        """Save continuation context for an agent.

        Agents call this before a context restart to preserve their state.
        The context is injected into the system prompt on next session start.
        """
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        # Find which session is saving (for updated_by)
        session_id = ""
        for s in manager.list():
            if s.agent_name == agent_name and s.session_type == "main":
                session_id = s.id
                break

        ctx = agents.set_context(
            agent_name,
            task=req.task, context=req.context, notes=req.notes,
            blockers=req.blockers, priority_items=req.priority_items,
            metadata=req.metadata, updated_by=session_id,
        )
        return ctx.to_dict()

    @app.get("/agents/{agent_name}/context")
    async def get_agent_context(agent_name: str):
        """Get the saved continuation context for an agent."""
        if not agents.get(agent_name):
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        ctx = agents.get_context(agent_name)
        if not ctx:
            return {"agent_name": agent_name, "context": None}
        return ctx.to_dict()

    @app.delete("/agents/{agent_name}/context")
    async def clear_agent_context(agent_name: str):
        """Clear the continuation context after consumption."""
        agents.clear_context(agent_name)
        return {"cleared": True}

    @app.post("/agents/{agent_name}/sleep")
    async def deep_sleep_agent(agent_name: str):
        """Put an agent into deep sleep — save context and close all sessions.

        The agent saves its current state and all sessions are closed.
        On next wake (manual or scheduled), context is restored.
        """
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        # Close all sessions for this agent
        closed = 0
        for s in list(manager.list()):
            if s.agent_name == agent_name:
                manager.delete(s.id)
                closed += 1

        _log(f"api: agent {agent_name} entered deep sleep, closed {closed} session(s)")
        return {
            "agent": agent_name,
            "status": "sleeping",
            "sessions_closed": closed,
            "context_saved": agents.get_context(agent_name) is not None,
        }

    # ── Wake Trigger ───────────────────────────────────────

    @app.post("/agents/{agent_name}/wake")
    async def wake_agent(agent_name: str, prompt: str = ""):
        """Manually trigger a wake for an agent's main session."""
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        main_session = manager.get(f"{agent_name}-main")
        if not main_session:
            raise HTTPException(404, f"No main session for agent '{agent_name}'. Spawn one first.")

        wake_prompt = prompt or "Manual wake trigger"
        _log(f"api: waking agent {agent_name} with prompt: {wake_prompt[:80]}...")

        msg = await main_session.send(wake_prompt)
        store.append(f"{agent_name}-main", "user", wake_prompt)
        if msg.content:
            store.append(f"{agent_name}-main", "assistant", msg.content)

        return {
            "agent": agent_name,
            "session_id": f"{agent_name}-main",
            "response": msg.content,
            "duration_ms": msg.duration_ms,
        }

    # ── Auto-spawn + Scheduler Startup ─────────────────────

    async def _spawn_main_session(agent_name: str) -> str | None:
        """Spawn a main session for an agent if one doesn't exist."""
        existing = manager.get(f"{agent_name}-main")
        if existing and existing.state != SessionState.closed:
            return existing.id

        agent = agents.get(agent_name)
        if not agent or not agent.enabled:
            return None

        system_prompt = agents.build_system_prompt(agent_name)
        work_dir = Path(agent.working_dir or default_working_dir).resolve()
        work_dir.mkdir(parents=True, exist_ok=True)
        claude_md = work_dir / "CLAUDE.md"
        claude_md.write_text(system_prompt)

        session = manager.create(
            session_id=f"{agent_name}-main",
            model=agent.model,
            soul=agent.soul,
            working_dir=str(work_dir),
            allowed_tools=agent.allowed_tools or None,
            max_turns=agent.max_turns,
            timeout=agent.timeout,
            system_prompt=system_prompt,
            restart_threshold_pct=agent.restart_threshold_pct,
            auto_restart=agent.auto_restart,
            permission_mode=agent.permission_mode,
            session_type="main",
            agent_name=agent_name,
        )
        _log(f"api: auto-spawned main session {session.id} for agent {agent_name}")
        return session.id

    async def _wake_callback(agent_name: str, session_id: str, prompt: str) -> None:
        """Callback for the scheduler to wake an agent."""
        session = manager.get(session_id)
        if not session:
            # Try to spawn if auto_start
            spawned_id = await _spawn_main_session(agent_name)
            if spawned_id:
                session = manager.get(spawned_id)
        if not session:
            _log(f"scheduler: no session for {agent_name}, skipping wake")
            return
        if session.state == SessionState.running:
            _log(f"scheduler: session {session_id} is busy, skipping wake")
            return

        msg = await session.send(prompt)
        store.append(session_id, "user", prompt)
        if msg.content:
            store.append(session_id, "assistant", msg.content)
        _log(f"scheduler: woke {agent_name} via {session_id}")

    scheduler = AgentScheduler(agents, wake_callback=_wake_callback)

    # Autonomy engine — self-directed work loops
    autonomy = AutonomyEngine(
        agents, tasks, store,
        session_sender=_wake_callback,
    )

    @app.on_event("startup")
    async def on_startup():
        """Auto-spawn main sessions, start scheduler, start autonomy engine."""
        # Auto-spawn main sessions for agents with auto_start=True
        auto_start_agents = agents.list_auto_start_agents()
        for agent in auto_start_agents:
            try:
                session_id = await _spawn_main_session(agent.name)
                if session_id:
                    _log(f"startup: main session ready for {agent.name} -> {session_id}")
            except Exception as e:
                _log(f"startup: failed to spawn main session for {agent.name}: {e}")

        # Start the scheduler
        await scheduler.start()

        # Start autonomy engine
        await autonomy.start()

        # Start work loops for auto_start agents
        for agent in auto_start_agents:
            await autonomy.start_agent_loop(agent.name)

        _log(f"startup: scheduler + autonomy running, {len(auto_start_agents)} agent(s) auto-started")

    @app.on_event("shutdown")
    async def on_shutdown():
        """Stop scheduler and autonomy on shutdown."""
        await autonomy.stop()
        await scheduler.stop()

    # ── Scheduler Control ──────────────────────────────────

    @app.get("/scheduler/status")
    async def scheduler_status():
        """Get scheduler status."""
        all_schedules = agents.get_all_schedules(enabled_only=False)
        auto_start = agents.list_auto_start_agents()
        return {
            "running": scheduler.running,
            "total_schedules": len(all_schedules),
            "enabled_schedules": sum(1 for s in all_schedules if s.enabled),
            "auto_start_agents": [a.name for a in auto_start],
        }

    # ── Autonomy Engine ────────────────────────────────────

    @app.get("/autonomy/status")
    async def autonomy_status():
        """Get autonomy engine status — active loops, event queues."""
        return autonomy.get_status()

    @app.post("/autonomy/{agent_name}/start")
    async def start_agent_loop(agent_name: str):
        """Start the autonomous work loop for an agent."""
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")
        await autonomy.start_agent_loop(agent_name)
        return {"agent": agent_name, "loop": "started"}

    @app.post("/autonomy/{agent_name}/stop")
    async def stop_agent_loop(agent_name: str):
        """Stop an agent's work loop."""
        await autonomy.stop_agent_loop(agent_name)
        return {"agent": agent_name, "loop": "stopped"}

    class PushEventRequest(BaseModel):
        type: str = "manual_wake"  # message_received, task_assigned, alert, manual_wake
        data: dict = Field(default_factory=dict)
        priority: int = 0  # 0=normal, 1=high, 2=urgent

    @app.post("/autonomy/{agent_name}/event")
    async def push_agent_event(agent_name: str, req: PushEventRequest):
        """Push an event to an agent's queue — triggers autonomous action.

        Event types: message_received, task_assigned, task_updated,
        schedule_wake, manual_wake, worker_report, alert
        """
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        try:
            event_type = EventType(req.type)
        except ValueError:
            raise HTTPException(400, f"Invalid event type: {req.type}")

        event = AgentEvent(
            type=event_type,
            agent_name=agent_name,
            data=req.data,
            priority=req.priority,
        )
        await autonomy.push_event(event)
        return {
            "agent": agent_name,
            "event": req.type,
            "queued": True,
            "pending": autonomy.event_queue.pending_count(agent_name),
        }

    # ── Audit & Hooks ──────────────────────────────────────

    @app.get("/audit")
    async def get_audit_log(
        agent_name: str = "", session_id: str = "",
        event: str = "", limit: int = 50,
    ):
        """Query the audit trail."""
        entries = audit.get_log(
            agent_name=agent_name, session_id=session_id,
            event=event, limit=limit,
        )
        return {"entries": [e.to_dict() for e in entries], "count": len(entries)}

    @app.get("/audit/costs")
    async def get_audit_costs(agent_name: str = "", session_id: str = ""):
        """Get cost summary from audit trail."""
        return audit.get_costs(agent_name=agent_name, session_id=session_id)

    @app.get("/hooks")
    async def list_hooks():
        """List all registered hooks."""
        return hooks.list_hooks()

    @app.get("/activity")
    async def get_activity_feed(limit: int = 50, since: float = 0.0):
        """Get live activity feed from hooks.

        Poll this endpoint for real-time agent activity.
        Use 'since' param with the last timestamp to get only new events.
        """
        feed = hooks.get_activity_feed(limit=limit, since=since)
        return {"events": feed, "count": len(feed)}

    @app.get("/activity/active")
    async def get_active_agents():
        """Get currently active agents and what they're doing."""
        active = hooks.get_active_agents()
        return {"agents": active, "count": len(active)}

    # ── Task/Project Management ──────────────────────────

    @app.get("/tasks-ui", response_class=HTMLResponse)
    async def tasks_ui():
        """Serve the task board."""
        p = frontend_dir / "tasks.html" if frontend_dir.exists() else None
        if p and p.exists():
            return FileResponse(str(p))
        return HTMLResponse("<h1>Frontend not found</h1>", status_code=404)

    # Projects

    @app.post("/projects")
    async def create_project(req: CreateProjectRequest):
        project = tasks.create_project(req.name, description=req.description)
        return project.to_dict()

    @app.get("/projects")
    async def list_projects(include_archived: bool = False):
        projects = tasks.list_projects(include_archived=include_archived)
        return {"projects": [p.to_dict() for p in projects], "count": len(projects)}

    @app.get("/projects/{project_id}")
    async def get_project(project_id: int):
        project = tasks.get_project(project_id)
        if not project:
            raise HTTPException(404, "Project not found")
        project_tasks = tasks.list(project_id=project_id, include_completed=True)
        return {
            "project": project.to_dict(),
            "tasks": [t.to_dict() for t in project_tasks],
            "task_count": len(project_tasks),
        }

    @app.put("/projects/{project_id}")
    async def update_project(project_id: int, req: CreateProjectRequest):
        project = tasks.update_project(project_id, name=req.name, description=req.description)
        if not project:
            raise HTTPException(404, "Project not found")
        return project.to_dict()

    @app.delete("/projects/{project_id}")
    async def delete_project(project_id: int):
        if not tasks.delete_project(project_id):
            raise HTTPException(404, "Project not found")
        return {"deleted": True}

    # Tasks

    @app.post("/tasks")
    async def create_task(req: CreateTaskRequest):
        task = tasks.create(
            req.title,
            project_id=req.project_id,
            description=req.description,
            status=req.status,
            priority=req.priority,
            assigned_agent=req.assigned_agent,
            created_by=req.created_by,
            tags=req.tags,
            due_date=req.due_date,
            parent_id=req.parent_id,
            blocked_by=req.blocked_by,
        )

        # Push event to assigned agent's autonomy loop
        if task.assigned_agent:
            priority = 2 if task.priority == "urgent" else 1 if task.priority == "high" else 0
            await autonomy.push_event(AgentEvent(
                type=EventType.task_assigned,
                agent_name=task.assigned_agent,
                data={"task_id": task.id, "title": task.title, "priority": task.priority},
                priority=priority,
            ))

        return task.to_dict()

    @app.get("/tasks")
    async def list_tasks(
        status: str = "",
        assigned_agent: str = "",
        priority: str = "",
        tag: str = "",
        project_id: int | None = None,
        include_completed: bool = False,
        limit: int = 100,
    ):
        task_list = tasks.list(
            status=status, assigned_agent=assigned_agent,
            priority=priority, tag=tag, project_id=project_id,
            include_completed=include_completed, limit=limit,
        )
        return {"tasks": [t.to_dict() for t in task_list], "count": len(task_list)}

    @app.get("/tasks/stats")
    async def task_stats():
        by_status = tasks.count_by_status()
        by_agent = tasks.count_by_agent()
        return {"by_status": by_status, "by_agent": by_agent}

    @app.get("/tasks/{task_id}")
    async def get_task(task_id: int):
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        subtasks = tasks.get_subtasks(task_id)
        comments = tasks.get_comments(task_id)
        return {
            "task": task.to_dict(),
            "subtasks": [s.to_dict() for s in subtasks],
            "comments": [c.to_dict() for c in comments],
        }

    @app.put("/tasks/{task_id}")
    async def update_task(task_id: int, req: UpdateTaskRequest):
        kwargs = {k: v for k, v in req.model_dump().items() if v is not None}
        task = tasks.update(task_id, **kwargs)
        if not task:
            raise HTTPException(404, "Task not found")
        return task.to_dict()

    @app.delete("/tasks/{task_id}")
    async def delete_task(task_id: int):
        if not tasks.delete(task_id):
            raise HTTPException(404, "Task not found")
        return {"deleted": True}

    # Task self-service (for agents to call autonomously)

    @app.post("/tasks/claim/{task_id}")
    async def claim_task(task_id: int, agent_name: str = ""):
        """Agent claims an unassigned task. Sets status to in_progress."""
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")
        if task.assigned_agent and task.assigned_agent != agent_name:
            raise HTTPException(409, f"Task already assigned to {task.assigned_agent}")

        updated = tasks.update(task_id, assigned_agent=agent_name, status="in_progress")
        tasks.add_comment(task_id, agent_name, f"Claimed and started work")
        return updated.to_dict()

    @app.post("/tasks/complete/{task_id}")
    async def complete_task(task_id: int, agent_name: str = "", summary: str = ""):
        """Agent marks a task as completed with an optional summary."""
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")

        updated = tasks.update(task_id, status="completed")
        comment = summary or "Task completed"
        tasks.add_comment(task_id, agent_name or task.assigned_agent, comment)

        # Push worker_report event to parent agent if task has a creator
        if task.created_by and task.created_by != agent_name:
            await autonomy.push_event(AgentEvent(
                type=EventType.worker_report,
                agent_name=task.created_by,
                data={
                    "task_id": task.id,
                    "title": task.title,
                    "worker": agent_name or task.assigned_agent,
                    "result": summary,
                },
                priority=1,
            ))

        return updated.to_dict()

    @app.post("/tasks/block/{task_id}")
    async def block_task(task_id: int, agent_name: str = "", reason: str = ""):
        """Agent marks a task as blocked with a reason."""
        task = tasks.get(task_id)
        if not task:
            raise HTTPException(404, "Task not found")

        updated = tasks.update(task_id, status="blocked")
        tasks.add_comment(task_id, agent_name or task.assigned_agent, f"Blocked: {reason}")
        return updated.to_dict()

    @app.get("/tasks/next")
    async def next_task(agent_name: str = "", priority: str = ""):
        """Get the next available task for an agent to work on.

        Returns highest-priority unassigned task, or the agent's
        highest-priority pending task.
        """
        # First check agent's own assigned tasks
        if agent_name:
            my_tasks = tasks.list(assigned_agent=agent_name, status="pending")
            if my_tasks:
                return {"task": my_tasks[0].to_dict(), "source": "assigned"}

            my_tasks = tasks.list(assigned_agent=agent_name, status="in_progress")
            if my_tasks:
                return {"task": my_tasks[0].to_dict(), "source": "in_progress"}

        # Then check unassigned tasks
        unassigned = tasks.list(assigned_agent="")
        # Filter to truly unassigned (empty string match)
        unassigned = [t for t in unassigned if not t.assigned_agent]
        if unassigned:
            return {"task": unassigned[0].to_dict(), "source": "unassigned"}

        return {"task": None, "source": "none"}

    # Self-monitoring endpoints

    @app.get("/agents/{agent_name}/health")
    async def agent_health(agent_name: str):
        """Comprehensive health check for an agent.

        Returns: session status, context usage, heartbeat health,
        task workload, autonomy loop status, error rate.
        """
        agent = agents.get(agent_name)
        if not agent:
            raise HTTPException(404, f"Agent '{agent_name}' not found")

        # Session info
        main_session = manager.get(f"{agent_name}-main")
        session_info = None
        if main_session:
            session_info = {
                "id": main_session.id,
                "state": main_session.state.value,
                "context_used_pct": main_session.context_used_pct,
                "message_count": main_session.message_count,
                "needs_restart": main_session.needs_restart,
            }

        # Heartbeat
        hb = agents.get_latest_heartbeat(agent_name)
        heartbeat_info = None
        if hb:
            age = time.time() - hb.timestamp
            heartbeat_info = {
                "status": hb.status,
                "age_seconds": int(age),
                "healthy": age < (agent.heartbeat_interval * 2) if agent.heartbeat_interval else True,
            }

        # Task workload
        agent_tasks = tasks.list(assigned_agent=agent_name)
        task_info = {
            "total_active": len(agent_tasks),
            "pending": sum(1 for t in agent_tasks if t.status == "pending"),
            "in_progress": sum(1 for t in agent_tasks if t.status == "in_progress"),
            "blocked": sum(1 for t in agent_tasks if t.status == "blocked"),
        }

        # Autonomy status
        autonomy_info = autonomy.get_status()
        loop_active = agent_name in autonomy_info.get("active_loops", [])
        pending_events = autonomy.event_queue.pending_count(agent_name)

        # Recent errors from audit
        errors = audit.get_log(agent_name=agent_name, event="error", limit=5)

        # Cost
        cost_info = audit.get_costs(agent_name=agent_name)

        return {
            "agent": agent_name,
            "enabled": agent.enabled,
            "role": agent.role,
            "session": session_info,
            "heartbeat": heartbeat_info,
            "tasks": task_info,
            "autonomy": {
                "loop_active": loop_active,
                "pending_events": pending_events,
            },
            "costs": cost_info,
            "recent_errors": [e.to_dict() for e in errors],
            "recommendation": _health_recommendation(session_info, heartbeat_info, task_info, errors),
        }

    def _health_recommendation(session, heartbeat, tasks, errors) -> str:
        """Generate a health recommendation based on metrics."""
        issues = []
        if session and session.get("needs_restart"):
            issues.append("context_full")
        if session and session.get("state") == "error":
            issues.append("session_error")
        if heartbeat and not heartbeat.get("healthy"):
            issues.append("heartbeat_stale")
        if tasks.get("blocked", 0) > 0:
            issues.append("tasks_blocked")
        if len(errors) >= 3:
            issues.append("high_error_rate")

        if not issues:
            return "healthy"
        if "context_full" in issues:
            return "needs_restart"
        if "session_error" in issues:
            return "needs_attention"
        if "high_error_rate" in issues:
            return "unstable"
        return "degraded"

    import time  # already imported but needed in scope

    # Task comments

    @app.post("/tasks/{task_id}/comments")
    async def add_comment(task_id: int, req: AddCommentRequest):
        if not tasks.get(task_id):
            raise HTTPException(404, "Task not found")
        comment = tasks.add_comment(task_id, req.author, req.content)
        return comment.to_dict()

    @app.get("/tasks/{task_id}/comments")
    async def get_comments(task_id: int, limit: int = 50):
        comments = tasks.get_comments(task_id, limit=limit)
        return {"comments": [c.to_dict() for c in comments], "count": len(comments)}

    @app.delete("/tasks/{task_id}/comments/{comment_id}")
    async def delete_comment(task_id: int, comment_id: int):
        if not tasks.delete_comment(comment_id):
            raise HTTPException(404, "Comment not found")
        return {"deleted": True}

    return app
