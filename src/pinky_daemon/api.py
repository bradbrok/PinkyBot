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
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from pinky_daemon.agent_comms import AgentComms
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.outreach_config import OutreachConfigStore
from pinky_daemon.sessions import SessionManager, SessionState
from pinky_daemon.skill_store import SkillStore


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# ── Request/Response Models ──────────────────────────────────


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

    manager = SessionManager(max_sessions=max_sessions)
    store = ConversationStore(db_path=db_path)
    comms = AgentComms(db_path=db_path.replace(".db", "_comms.db"))
    skills = SkillStore(db_path=db_path.replace(".db", "_skills.db"))
    outreach_config = OutreachConfigStore(db_path=db_path.replace(".db", "_outreach.db"))

    # Serve frontend
    frontend_dir = Path(__file__).parent.parent.parent / "frontend"
    if frontend_dir.exists():
        app.mount("/static", StaticFiles(directory=str(frontend_dir)), name="static")

    @app.get("/")
    async def root():
        """Health check and server info."""
        return {
            "name": "pinky",
            "version": "0.1.0",
            "sessions": manager.count,
        }

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
        """Get conversation history for a session."""
        session = manager.get(session_id)
        if not session:
            raise HTTPException(404, f"Session '{session_id}' not found")

        history = session.get_history(limit=limit)
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

    return app
