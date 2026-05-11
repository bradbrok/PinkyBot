"""Knowledge base + wiki + librarian routes.

Extracted from api.py. Wired up via `set_dependencies(...)` from
`create_api()` so the route handlers can read shared stores and the
mutable librarian-state container without going through closures.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Awaitable, Callable

from fastapi import APIRouter, BackgroundTasks, HTTPException

from pinky_daemon.api_models import KBIngestRequest, WikiSaveRequest

router = APIRouter(tags=["kb"])


# ── Shared dependency state ───────────────────────────────────────────────────
# Populated by set_dependencies() called from create_api() in api.py.

_kb: Any = None
_agents: Any = None
_librarian_runner: Any = None
_librarian_state: SimpleNamespace | None = None
_schedule_librarian: Callable[[], None] | None = None
_trigger_librarian: Callable[[], Awaitable[None]] | None = None
_kb_ingest_people_profiles: Callable[[], None] | None = None
_kb_ingest_project_state: Callable[[], None] | None = None


def set_dependencies(
    *,
    kb,
    agents,
    librarian_runner,
    librarian_state: SimpleNamespace,
    schedule_librarian: Callable[[], None],
    trigger_librarian: Callable[[], Awaitable[None]],
    kb_ingest_people_profiles: Callable[[], None],
    kb_ingest_project_state: Callable[[], None],
) -> None:
    """Wire shared instances and helper callables for the KB router."""
    global _kb, _agents, _librarian_runner, _librarian_state
    global _schedule_librarian, _trigger_librarian
    global _kb_ingest_people_profiles, _kb_ingest_project_state
    _kb = kb
    _agents = agents
    _librarian_runner = librarian_runner
    _librarian_state = librarian_state
    _schedule_librarian = schedule_librarian
    _trigger_librarian = trigger_librarian
    _kb_ingest_people_profiles = kb_ingest_people_profiles
    _kb_ingest_project_state = kb_ingest_project_state


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/kb/ingest")
async def kb_ingest(req: KBIngestRequest):
    """File a new raw source into the knowledge base."""
    existing = _kb.check_duplicate(source_url=req.source_url, content=req.content)
    if existing:
        return {
            "status": "duplicate",
            "existing": existing.to_dict(),
            "message": f"Already filed as {existing.id}: {existing.title}",
        }

    source = _kb.ingest(
        title=req.title,
        content=req.content,
        source_url=req.source_url,
        source_type=req.source_type,
        filed_by=req.filed_by,
        tags=req.tags,
        owner_notes=req.owner_notes,
    )

    # Trigger librarian debounce (new data arrived)
    _schedule_librarian()

    return {"status": "filed", "source": source.to_dict()}


@router.post("/kb/auto-ingest")
async def kb_auto_ingest_trigger(sources: list[str] | None = None):
    """Manually trigger auto-ingest of system data into KB.

    Sources: "people", "projects", "all". Defaults to all.
    """
    targets = sources or ["all"]
    if "all" in targets:
        targets = ["people", "projects"]

    results = {}
    if "people" in targets:
        _kb_ingest_people_profiles()
        results["people"] = "ingested"
    if "projects" in targets:
        _kb_ingest_project_state()
        results["projects"] = "ingested"

    return {"status": "ok", "results": results}


@router.get("/kb/raw")
async def kb_list_raw(
    tag: str | None = None,
    source_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
):
    """List raw sources with optional filters."""
    sources = _kb.list_raw(tag=tag, source_type=source_type, limit=limit, offset=offset)
    total = _kb.count_raw(tag=tag, source_type=source_type)
    return {"sources": [s.to_dict() for s in sources], "total": total}


@router.get("/kb/raw/{source_id}")
async def kb_get_raw(source_id: str, include_content: bool = False):
    """Get a specific raw source by ID."""
    source = _kb.get_raw(source_id)
    if not source:
        raise HTTPException(404, f"Raw source '{source_id}' not found")
    result = source.to_dict(include_preview=True)
    if include_content:
        result["content"] = _kb.get_raw_content(source_id)
    return result


@router.delete("/kb/raw/{source_id}")
async def kb_delete_raw(source_id: str):
    """Delete a raw source (file + DB + FTS)."""
    deleted = _kb.delete_raw(source_id)
    if not deleted:
        raise HTTPException(404, f"Raw source '{source_id}' not found")
    return {"deleted": True, "source_id": source_id}


@router.put("/kb/raw/{source_id}")
async def kb_update_raw(source_id: str, req: dict):
    """Update fields on a raw source (title, content, tags, source_type, source_url)."""
    allowed = {"title", "content", "tags", "source_type", "source_url", "owner_notes"}
    updates = {k: v for k, v in req.items() if k in allowed}
    if not updates:
        raise HTTPException(400, "No valid fields to update")
    updated = _kb.update_raw(source_id, **updates)
    if not updated:
        raise HTTPException(404, f"Raw source '{source_id}' not found")
    return updated.to_dict(include_preview=True)


@router.get("/kb/wiki")
async def kb_list_wiki(limit: int = 100, offset: int = 0):
    """List wiki pages."""
    pages = _kb.list_wiki(limit=limit, offset=offset)
    return {"pages": [p.to_dict() for p in pages]}


@router.get("/kb/wiki/{slug:path}")
async def kb_get_wiki(slug: str, include_content: bool = True):
    """Get a specific wiki page by slug."""
    page = _kb.get_wiki(slug)
    if not page:
        raise HTTPException(404, f"Wiki page '{slug}' not found")
    result = page.to_dict()
    if include_content:
        result["content"] = _kb.get_wiki_content(slug)
    return result


@router.put("/kb/wiki/{slug:path}")
async def kb_save_wiki(slug: str, req: WikiSaveRequest):
    """Create or update a wiki page."""
    page = _kb.save_wiki(
        slug=slug,
        title=req.title,
        content=req.content,
        sources=req.sources,
        related=req.related,
    )
    return {"status": "saved", **page.to_dict()}


@router.delete("/kb/wiki/{slug:path}")
async def kb_delete_wiki(slug: str):
    """Delete a wiki page."""
    deleted = _kb.delete_wiki(slug)
    if not deleted:
        raise HTTPException(404, f"Wiki page '{slug}' not found")
    return {"status": "deleted", "slug": slug}


@router.get("/kb/search")
async def kb_search(q: str, scope: str = "all", limit: int = 20):
    """Full-text search across raw sources and wiki pages."""
    results = _kb.search(q, scope=scope, limit=limit)
    return {"query": q, "scope": scope, "results": results}


@router.get("/kb/stats")
async def kb_stats():
    """Get knowledge base statistics."""
    return _kb.stats().to_dict()


@router.post("/kb/reindex")
async def kb_reindex():
    """Rebuild the FTS index from disk files."""
    result = _kb.reindex()
    return {"status": "reindexed", **result}


@router.post("/kb/librarian/run")
async def kb_librarian_run(background_tasks: BackgroundTasks):
    """Manually trigger KB librarian. Runs in background."""
    if _librarian_state.running:
        return {"status": "already_running"}

    main_name = _agents.get_main_agent()
    if not main_name:
        raise HTTPException(400, "No main agent configured")
    agent = _agents.get(main_name)
    if not agent:
        raise HTTPException(404, f"Agent '{main_name}' not found")

    background_tasks.add_task(_trigger_librarian)
    return {"status": "triggered", "agent": main_name}


@router.get("/kb/librarian/state")
async def kb_librarian_state():
    """Get librarian state and config."""
    main_name = _agents.get_main_agent() or "_default"
    state = _librarian_runner.get_state(main_name)
    return {
        **state,
        "auto_run": _librarian_state.auto_run,
        "running": _librarian_state.running,
        "has_new_sources": _librarian_runner.has_new_sources(main_name),
    }


@router.put("/kb/librarian/auto-run")
async def kb_librarian_set_auto_run(enabled: bool = True):
    """Toggle librarian auto-run on ingest."""
    _librarian_state.auto_run = enabled
    return {"auto_run": _librarian_state.auto_run}


@router.get("/kb/graph")
async def kb_graph():
    """Get KB as a graph (nodes + edges) for visualization."""
    wiki_pages = _kb.list_wiki(limit=500)
    raw_sources = _kb.list_raw(limit=500)

    nodes = []
    edges = []
    seen_edges = set()

    # Wiki pages as primary nodes
    for p in wiki_pages:
        category = p.slug.split("/")[0] if "/" in p.slug else "other"
        nodes.append({
            "id": p.slug,
            "label": p.title,
            "type": "wiki",
            "category": category,
            "degree": len(p.related) + len(p.sources),
        })

        # Edges to related wiki pages
        for rel in p.related:
            edge_key = tuple(sorted([p.slug, rel]))
            if edge_key not in seen_edges:
                seen_edges.add(edge_key)
                edges.append({
                    "source": p.slug,
                    "target": rel,
                    "type": "related",
                })

    # Raw sources as secondary nodes
    for s in raw_sources:
        nodes.append({
            "id": s.id,
            "label": s.title,
            "type": "raw",
            "category": s.source_type,
            "degree": 0,
        })

    # Edges from wiki pages to their raw sources
    for p in wiki_pages:
        for src_id in p.sources:
            edges.append({
                "source": p.slug,
                "target": src_id,
                "type": "source",
            })
            # Update raw source degree
            for n in nodes:
                if n["id"] == src_id:
                    n["degree"] += 1
                    break

    return {
        "nodes": nodes,
        "edges": edges,
        "stats": {
            "wiki_count": len(wiki_pages),
            "raw_count": len(raw_sources),
            "edge_count": len(edges),
        },
    }
