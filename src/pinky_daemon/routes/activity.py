"""Activity log + audit + hook-feed routes.

Extracted from api.py. Three small surfaces that all share `audit`,
`hooks`, and `activity` as their only dependencies — bundled together
because they were defined contiguously and conceptually all show
"what's happening" in the system.
"""

from __future__ import annotations

import asyncio
import json
import time
from typing import Any

from fastapi import APIRouter
from fastapi.responses import StreamingResponse

router = APIRouter(tags=["activity"])


# ── Shared dependency state ───────────────────────────────────────────────────

_audit: Any = None
_hooks: Any = None
_activity: Any = None


def set_dependencies(*, audit, hooks, activity) -> None:
    """Wire shared instances for the activity router."""
    global _audit, _hooks, _activity
    _audit = audit
    _hooks = hooks
    _activity = activity


# ── Audit & Hooks ─────────────────────────────────────────────────────────────


@router.get("/audit")
async def get_audit_log(
    agent_name: str = "", session_id: str = "",
    event: str = "", limit: int = 50,
):
    """Query the audit trail."""
    entries = _audit.get_log(
        agent_name=agent_name, session_id=session_id,
        event=event, limit=limit,
    )
    return {"entries": [e.to_dict() for e in entries], "count": len(entries)}


@router.get("/audit/costs")
async def get_audit_costs(agent_name: str = "", session_id: str = ""):
    """Get cost summary from audit trail."""
    return _audit.get_costs(agent_name=agent_name, session_id=session_id)


@router.get("/hooks")
async def list_hooks():
    """List all registered hooks."""
    return _hooks.list_hooks()


@router.get("/activity/hooks")
async def get_activity_feed(limit: int = 50, since: float = 0.0):
    """Get live activity feed from hooks (in-memory, real-time).

    Poll this endpoint for real-time agent activity.
    Use 'since' param with the last timestamp to get only new events.
    """
    feed = _hooks.get_activity_feed(limit=limit, since=since)
    return {"events": feed, "count": len(feed)}


@router.get("/activity/active")
async def get_active_agents():
    """Get currently active agents and what they're doing."""
    active = _hooks.get_active_agents()
    return {"agents": active, "count": len(active)}


# ── Activity Log ──────────────────────────────────────────────────────────────


@router.get("/activity")
async def list_activity(
    limit: int = 50, offset: int = 0, agent_name: str = "", event_type: str = ""
):
    """Return recent activity events across all agents (or filtered to one agent)."""
    events = _activity.list(
        limit=limit, offset=offset, agent_name=agent_name, event_type=event_type
    )
    return {"events": events, "count": len(events)}


@router.get("/activity/stats")
async def activity_stats():
    """Return summary stats for the activity log."""
    return _activity.get_stats()


# ── Activity SSE stream ───────────────────────────────────────────────────────


@router.get("/activity/stream")
async def stream_activity():
    """SSE endpoint for real-time activity feed.

    Polls the hook manager's activity store for new events.
    Bridge pattern — replace with proper event bus later.
    """
    async def event_generator():
        last_check = time.time()
        while True:
            await asyncio.sleep(2)
            now = time.time()
            # Get new activity since last check
            feed = _hooks.get_activity_feed(limit=20, since=last_check)
            if feed:
                for event in feed:
                    yield f"data: {json.dumps(event)}\n\n"
            else:
                yield f"data: {json.dumps({'type': 'ping', 'timestamp': now})}\n\n"
            last_check = now

    return StreamingResponse(event_generator(), media_type="text/event-stream")
