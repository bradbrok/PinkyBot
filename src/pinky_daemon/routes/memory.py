"""Per-agent memory + chat-history + knowledge-graph routes.

Extracted from api.py. Each agent has its own memory database
(`{working_dir}/data/memory.db`); the `_get_memory_store` callable
opens the right DB per request.

Both `_collect_agent_session_ids` and `_resolve_agent_history` stay in
api.py — they are also wired into `dream_runner` and exposed via
`app.state.agent_history_resolver` — and are passed in here as
callables.
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

try:
    from pinky_memory.store import ReflectionStore
    from pinky_memory.types import MemoryQueryFilters
except ImportError:
    ReflectionStore = None  # type: ignore[assignment, misc]
    MemoryQueryFilters = None  # type: ignore[assignment, misc]

router = APIRouter(tags=["memory"])


# ── Shared dependency state ───────────────────────────────────────────────────

_agents: Any = None
_store: Any = None
_collect_agent_session_ids: Callable[[str], set[str]] | None = None
_resolve_agent_history: Callable[..., list[dict]] | None = None


def set_dependencies(
    *,
    agents,
    store,
    collect_agent_session_ids: Callable[[str], set[str]],
    resolve_agent_history: Callable[..., list[dict]],
) -> None:
    """Wire shared instances and helpers for the memory router."""
    global _agents, _store
    global _collect_agent_session_ids, _resolve_agent_history
    _agents = agents
    _store = store
    _collect_agent_session_ids = collect_agent_session_ids
    _resolve_agent_history = resolve_agent_history


# ── Local helpers ─────────────────────────────────────────────────────────────


def _get_memory_store(agent_name: str):
    """Open the per-agent reflection store at {working_dir}/data/memory.db."""
    if ReflectionStore is None:
        raise HTTPException(501, "pinky_memory is not installed")
    agent = _agents.get(agent_name)
    if not agent:
        raise HTTPException(404, f"Agent '{agent_name}' not found")
    db_path = str(Path(agent.working_dir) / "data" / "memory.db")
    if not Path(db_path).exists():
        raise HTTPException(404, f"No memory database for agent '{agent_name}'")
    return ReflectionStore(db_path=db_path)


def _reflection_to_dict(r) -> dict:
    """Serialize a Reflection to a JSON-safe dict (omit embedding)."""
    return {
        "id": r.id,
        "type": r.type.value,
        "content": r.content,
        "context": r.context,
        "project": r.project,
        "salience": r.salience,
        "active": r.active,
        "no_recall": r.no_recall,
        "supersedes": r.supersedes,
        "superseded_by": r.superseded_by,
        "event_date": r.event_date,
        "entities": r.entities,
        "source_session_id": r.source_session_id,
        "source_channel": r.source_channel,
        "source_message_ids": r.source_message_ids,
        "created_at": r.created_at.isoformat(),
        "accessed_at": r.accessed_at.isoformat(),
        "access_count": r.access_count,
        "weight": round(r.weight, 4),
        "next_review_date": r.next_review_date,
        "review_interval_days": r.review_interval_days,
    }


# ── Memory Browsing ───────────────────────────────────────────────────────────


@router.get("/agents/{agent_name}/memories")
async def list_memories(
    agent_name: str,
    type: str = "",
    project: str = "",
    entity: str = "",
    salience_min: int | None = None,
    salience_max: int | None = None,
    active: bool = True,
    sort_by: str = "created_at",
    sort_dir: str = "desc",
    limit: int = 50,
    offset: int = 0,
):
    """List/filter memories for an agent."""
    store = _get_memory_store(agent_name)
    filters = MemoryQueryFilters(
        type=type or None,
        project=project or None,
        entity=entity or None,
        salience_min=salience_min,
        salience_max=salience_max,
        active=active,
        sort_by=sort_by,
        sort_dir=sort_dir,
        limit=min(limit, 100),
        offset=offset,
    )
    results, total = store.query(filters)
    store.close()
    return {
        "memories": [_reflection_to_dict(r) for r in results],
        "total": total,
        "limit": filters.limit,
        "offset": filters.offset,
    }


@router.get("/agents/{agent_name}/memories/search")
async def search_memories(agent_name: str, q: str = "", limit: int = 20):
    """Keyword search across an agent's memories."""
    if not q:
        raise HTTPException(400, "Query parameter 'q' is required")
    store = _get_memory_store(agent_name)
    results = store.search_by_keyword(q, limit=min(limit, 50))
    store.close()
    return {
        "memories": [_reflection_to_dict(r) for r in results],
        "query": q,
        "count": len(results),
    }


@router.get("/agents/{agent_name}/chat-history")
async def search_agent_chat_history(
    agent_name: str, q: str = "", limit: int = 50,
    after: str = "", before: str = "", role: str = "",
):
    """Search an agent's chat history across all their sessions.

    Args:
        q: Full-text search query (optional).
        limit: Max results.
        after: ISO date (YYYY-MM-DD) — only messages after this date.
        before: ISO date (YYYY-MM-DD) — only messages before this date.
        role: Filter by role (user, assistant).
    """
    agent = _agents.get(agent_name)
    if not agent:
        raise HTTPException(404, f"Agent '{agent_name}' not found")

    # Parse date filters to timestamps
    after_ts = 0.0
    before_ts = 0.0
    if after:
        try:
            after_ts = datetime.strptime(after, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            raise HTTPException(400, "Invalid 'after' date format. Use YYYY-MM-DD.") from None
    if before:
        try:
            # End of day
            before_ts = (datetime.strptime(before, "%Y-%m-%d").replace(tzinfo=timezone.utc).timestamp()) + 86400
        except ValueError:
            raise HTTPException(400, "Invalid 'before' date format. Use YYYY-MM-DD.") from None

    # Build set of session IDs belonging to this agent from both
    # active sessions AND the persistent conversation store
    agent_session_ids = _collect_agent_session_ids(agent_name)

    results = []
    if q:
        # Search globally then filter to agent's sessions
        try:
            all_results = _store.search(q, limit=limit * 3)
            results = [m for m in all_results if m.session_id in agent_session_ids or m.session_id.startswith(f"{agent_name}-")]
        except Exception:
            pass
    else:
        results = [
            SimpleNamespace(**m)
            for m in _resolve_agent_history(
                agent_name,
                after_ts=after_ts,
                before_ts=before_ts,
                limit=limit,
                role=role,
            )
        ]

    if q:
        # Apply date filters after search results
        if after_ts:
            results = [m for m in results if m.timestamp >= after_ts]
        if before_ts:
            results = [m for m in results if m.timestamp <= before_ts]
        if role:
            results = [m for m in results if m.role == role]
        results.sort(key=lambda m: m.timestamp, reverse=True)
        results = results[:limit]

    return {
        "messages": [
            {
                "id": m.id,
                "session_id": m.session_id,
                "role": m.role,
                "content": m.content[:500],
                "timestamp": m.timestamp,
                "platform": m.platform,
                "duration_ms": getattr(m, "duration_ms", 0),
                "truncated": len(m.content) > 500,
            }
            for m in results
        ],
        "query": q,
        "count": len(results),
        "sessions_searched": len(agent_session_ids),
    }


@router.get("/agents/{agent_name}/chat-history/{message_id}")
async def get_chat_message(agent_name: str, message_id: int):
    """Get a single chat message with full content (not truncated)."""
    agent = _agents.get(agent_name)
    if not agent:
        raise HTTPException(404, f"Agent '{agent_name}' not found")

    # Verify message belongs to this agent's sessions
    agent_session_ids = _collect_agent_session_ids(agent_name)
    message = _store.get_message(message_id)
    if not message:
        raise HTTPException(404, f"Message {message_id} not found")
    if message.session_id not in agent_session_ids and not message.session_id.startswith(f"{agent_name}-"):
        raise HTTPException(403, "Access denied")

    return {
        "id": message.id,
        "session_id": message.session_id,
        "role": message.role,
        "content": message.content,
        "timestamp": message.timestamp,
        "platform": message.platform,
        "chat_id": message.chat_id,
    }


@router.patch("/agents/{agent_name}/chat-history/{message_id}")
async def edit_chat_message(agent_name: str, message_id: int, req: Request):
    """Edit a chat message's content."""
    agent = _agents.get(agent_name)
    if not agent:
        raise HTTPException(404, f"Agent '{agent_name}' not found")
    body = await req.json()
    content = body.get("content", "")
    if not content.strip():
        raise HTTPException(400, "Content cannot be empty")

    # Verify message belongs to this agent's sessions (#489)
    agent_session_ids = _collect_agent_session_ids(agent_name)
    message = _store.get_message(message_id)
    if not message:
        raise HTTPException(404, f"Message {message_id} not found")
    if message.session_id not in agent_session_ids and not message.session_id.startswith(f"{agent_name}-"):
        raise HTTPException(403, "Access denied")

    if not _store.edit_message(message_id, content):
        raise HTTPException(404, f"Message {message_id} not found")
    return {"ok": True}


@router.delete("/agents/{agent_name}/chat-history/{message_id}")
async def delete_chat_message(agent_name: str, message_id: int):
    """Delete a chat message."""
    agent = _agents.get(agent_name)
    if not agent:
        raise HTTPException(404, f"Agent '{agent_name}' not found")

    # Verify message belongs to this agent's sessions (#489)
    agent_session_ids = _collect_agent_session_ids(agent_name)
    message = _store.get_message(message_id)
    if not message:
        raise HTTPException(404, f"Message {message_id} not found")
    if message.session_id not in agent_session_ids and not message.session_id.startswith(f"{agent_name}-"):
        raise HTTPException(403, "Access denied")

    if not _store.delete_message(message_id):
        raise HTTPException(404, f"Message {message_id} not found")
    return {"ok": True}


@router.get("/agents/{agent_name}/memories/stats")
async def memory_stats(agent_name: str, timeframe: str = "all"):
    """Get memory statistics for an agent."""
    store = _get_memory_store(agent_name)
    stats = store.introspect(timeframe=timeframe)
    store.close()
    return stats


@router.get("/agents/{agent_name}/memories/{memory_id}")
async def get_memory(agent_name: str, memory_id: str):
    """Get a single memory by ID."""
    store = _get_memory_store(agent_name)
    reflection = store.get(memory_id)
    store.close()
    if not reflection:
        raise HTTPException(404, f"Memory '{memory_id}' not found")
    return _reflection_to_dict(reflection)


@router.get("/agents/{agent_name}/memories/{memory_id}/links")
async def get_memory_links(agent_name: str, memory_id: str):
    """Get linked memories for a reflection."""
    store = _get_memory_store(agent_name)
    links = store.get_links(memory_id)
    # Also fetch the linked reflections themselves
    linked_memories = []
    for link in links:
        target = store.get(link.target_id)
        if target:
            linked_memories.append({
                "similarity": round(link.similarity, 3),
                "memory": _reflection_to_dict(target),
            })
    store.close()
    return {"links": linked_memories, "count": len(linked_memories)}


# ── Memory Editing ────────────────────────────────────────────────────────────


class MemoryContentUpdate(BaseModel):
    content: str


def _require_owner(request: Request) -> None:
    """Refuse anything but a web-UI owner session (#463).

    Memories are rewritten and deleted only by the owner, from the UI. An agent
    holding a valid internal signature is authenticated but is NOT the owner —
    no agent may mutate memories, its own included. Reading stays open.
    """
    if getattr(request.state, "auth_actor", None) != "owner":
        raise HTTPException(403, "Only the owner may edit or delete memories")


@router.patch("/agents/{agent_name}/memories/{memory_id}")
async def update_memory(
    agent_name: str, memory_id: str, body: MemoryContentUpdate, request: Request
):
    """Rewrite a memory's content.

    The store invalidates the stale embedding, so the reflection re-enters the
    heal-on-write backlog and semantic recall stops matching the old wording.
    """
    _require_owner(request)
    content = body.content.strip()
    if not content:
        raise HTTPException(400, "Content cannot be empty")
    store = _get_memory_store(agent_name)
    if not store.get(memory_id):
        store.close()
        raise HTTPException(404, f"Memory '{memory_id}' not found")
    store.update_content(memory_id, content)
    updated = store.get(memory_id)
    store.close()
    return _reflection_to_dict(updated)


@router.delete("/agents/{agent_name}/memories/{memory_id}")
async def delete_memory(
    agent_name: str, memory_id: str, request: Request, hard: bool = False
):
    """Delete a memory. Soft (reversible) by default.

    The default archives the reflection and logs a `memory_events` row, so the
    deletion can be undone with `revert_memory_event`. `hard=true` removes the
    row, its vector and its links outright — there is nothing left to revert.
    """
    _require_owner(request)
    store = _get_memory_store(agent_name)
    reflection = store.get(memory_id)
    if not reflection:
        store.close()
        raise HTTPException(404, f"Memory '{memory_id}' not found")
    if hard:
        store.delete_reflection(memory_id)
    elif reflection.active:
        store.archive_reflection(memory_id, reason="ui-delete")
    # An already-archived reflection is left alone: re-archiving would stack a
    # second event and bury the undo of the first.
    store.close()
    return {"id": memory_id, "deleted": True, "hard": hard}


# ── Knowledge Graph ───────────────────────────────────────────────────────────


@router.get("/agents/{agent_name}/memory/kg-graph")
async def get_memory_kg_graph(agent_name: str):
    """Get the knowledge graph as nodes + edges for visualization."""
    store = _get_memory_store(agent_name)
    try:
        entities = store.kg_entities_list(limit=500)
        triples = store.kg_query(include_expired=False, limit=1000)
    except Exception:
        store.close()
        return {"nodes": [], "edges": []}

    # Build degree map
    degree: dict[str, int] = {}
    for t in triples:
        degree[t["subject"]] = degree.get(t["subject"], 0) + 1
        degree[t["object"]] = degree.get(t["object"], 0) + 1

    # Entity name → type lookup
    type_map = {e["name"]: e["type"] for e in entities}

    # Build nodes from all entities mentioned in active triples
    node_names: set[str] = set()
    for t in triples:
        node_names.add(t["subject"])
        node_names.add(t["object"])

    nodes = [
        {
            "id": name,
            "label": name,
            "type": type_map.get(name, "unknown"),
            "degree": degree.get(name, 0),
        }
        for name in sorted(node_names)
    ]

    edges = [
        {
            "source": t["subject"],
            "target": t["object"],
            "label": t["predicate"],
            "type": "kg",
        }
        for t in triples
    ]

    store.close()
    return {"nodes": nodes, "edges": edges}


@router.get("/agents/{agent_name}/memory/kg-stats")
async def get_memory_kg_stats(agent_name: str):
    """Get knowledge graph statistics for an agent."""
    store = _get_memory_store(agent_name)
    try:
        stats = store.kg_stats()
    except Exception:
        store.close()
        return {"entities": 0, "triples_total": 0, "triples_active": 0,
                "entity_types": {}, "predicates": {}}
    store.close()
    return stats
