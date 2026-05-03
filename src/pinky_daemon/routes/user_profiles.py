"""User profile and relationship routes.

Extracted from api.py. The `user_profiles: UserProfileStore` instance
itself stays in api.py — it's also read by `_kb_ingest_people_profiles`
inside `create_api()` — and is passed in via `set_dependencies(...)`.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException

from pinky_daemon.api_models import (
    AddRelationshipRequest,
    SetVisibilityRequest,
    UpdateProfileEntry,
    UpsertProfileEntry,
)
from pinky_daemon.user_profile_store import (
    PROFILE_CATEGORIES,
    ProfileEntry,
    Relationship,
)

router = APIRouter(tags=["user-profiles"])


# ── Shared dependency state ───────────────────────────────────────────────────

_user_profiles: Any = None
_agents: Any = None


def set_dependencies(*, user_profiles, agents) -> None:
    """Wire shared instances for the user-profiles router."""
    global _user_profiles, _agents
    _user_profiles = user_profiles
    _agents = agents


# ── Routes ────────────────────────────────────────────────────────────────────


@router.get("/user-profiles")
async def list_user_profiles():
    """List all users with profiles and stats."""
    users = _user_profiles.get_all_users()
    result = []
    for uid in users:
        entries = _user_profiles.get_user_profile(uid)
        # Try to find display name
        display = uid
        for au in _agents.list_all_approved_users():
            if au["chat_id"] == uid:
                display = au.get("display_name") or uid
                break
        result.append({
            "chat_id": uid,
            "display_name": display,
            "entry_count": len(entries),
            "categories": list({e.category for e in entries}),
        })
    return {"users": result, "stats": _user_profiles.stats()}


@router.get("/user-profiles/{chat_id}")
async def get_user_profile_entries(chat_id: str, category: str = ""):
    """Get all profile entries for a user, optionally filtered by category."""
    entries = _user_profiles.get_user_profile(chat_id, category=category)
    return {
        "chat_id": chat_id,
        "entries": [e.to_dict() for e in entries],
        "categories": PROFILE_CATEGORIES,
    }


@router.post("/user-profiles/{chat_id}")
async def upsert_profile_entry(chat_id: str, req: UpsertProfileEntry):
    """Add or update a profile entry for a user."""
    if req.category not in PROFILE_CATEGORIES:
        raise HTTPException(400, f"Invalid category. Valid: {list(PROFILE_CATEGORIES)}")
    entry = _user_profiles.upsert(ProfileEntry(
        chat_id=chat_id,
        category=req.category,
        key=req.key,
        value=req.value,
        confidence=req.confidence,
        source=req.source,
    ))
    return entry.to_dict()


@router.put("/user-profiles/entries/{entry_id}")
async def update_profile_entry(entry_id: int, req: UpdateProfileEntry):
    """Update a specific profile entry."""
    entry = _user_profiles.update_entry(
        entry_id,
        value=req.value,
        confidence=req.confidence,
        source="manual",
    )
    if not entry:
        raise HTTPException(404, "Profile entry not found")
    return entry.to_dict()


@router.delete("/user-profiles/entries/{entry_id}")
async def delete_profile_entry(entry_id: int):
    """Delete a specific profile entry."""
    if not _user_profiles.delete_entry(entry_id):
        raise HTTPException(404, "Profile entry not found")
    return {"deleted": True}


@router.delete("/user-profiles/{chat_id}")
async def delete_user_profile(chat_id: str):
    """Delete all profile entries for a user."""
    count = _user_profiles.delete_user_profile(chat_id)
    return {"deleted": count}


@router.put("/user-profiles/{chat_id}/visibility/{agent_name}")
async def set_profile_visibility(
    chat_id: str, agent_name: str, req: SetVisibilityRequest
):
    """Set whether an agent can see a user's profile."""
    if not _agents.get(agent_name):
        raise HTTPException(404, f"Agent '{agent_name}' not found")
    _user_profiles.set_visibility(agent_name, chat_id, req.visible)
    return {"agent": agent_name, "chat_id": chat_id, "visible": req.visible}


@router.get("/user-profiles/{chat_id}/visibility")
async def get_profile_visibility(chat_id: str):
    """Get visibility settings for a user's profile across all agents."""
    all_agents = [a["name"] for a in _agents.list()]
    result = []
    for name in all_agents:
        visible = _user_profiles.get_visibility(name, chat_id)
        result.append({"agent_name": name, "visible": visible})
    return {"chat_id": chat_id, "agents": result}


# ── User Relationships ────────────────────────────────────────────────────────


@router.get("/user-profiles/{chat_id}/relationships")
async def get_user_relationships(chat_id: str):
    """Get all relationships for a user."""
    rels = _user_profiles.get_relationships(chat_id)
    reverse = _user_profiles.get_reverse_relationships(chat_id)
    return {
        "chat_id": chat_id,
        "relationships": [r.to_dict() for r in rels],
        "reverse_relationships": [r.to_dict() for r in reverse],
    }


@router.post("/user-profiles/{chat_id}/relationships")
async def add_user_relationship(chat_id: str, req: AddRelationshipRequest):
    """Add a relationship for a user."""
    rel = _user_profiles.add_relationship(Relationship(
        from_chat_id=chat_id,
        to_chat_id=req.to_chat_id,
        to_display_name=req.to_display_name,
        relation=req.relation,
        context=req.context,
        confidence=req.confidence,
    ))
    return rel.to_dict()


@router.delete("/user-profiles/relationships/{rel_id}")
async def delete_user_relationship(rel_id: int):
    """Delete a relationship."""
    if not _user_profiles.delete_relationship(rel_id):
        raise HTTPException(404, "Relationship not found")
    return {"deleted": True}
