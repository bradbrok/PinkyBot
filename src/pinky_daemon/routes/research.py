"""Research pipeline routes.

Extracted from api.py. Endpoints cover topic CRUD, brief submission,
peer review, publish, and export (markdown/html/pdf).
"""

from __future__ import annotations

import os
from typing import Any, Callable

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from pinky_daemon.api_models import (
    AssignResearchRequest,
    CreateResearchRequest,
    SubmitBriefRequest,
    SubmitReviewRequest,
    UpdateResearchRequest,
)
from pinky_daemon.research_export import (
    export_brief_html,
    export_brief_markdown,
    export_brief_pdf,
    get_export_content_markdown,
)

router = APIRouter(tags=["research"])


# ── Shared dependency state ───────────────────────────────────────────────────

_research: Any = None
_activity: Any = None
_kb_ingest_research: Callable[[int], None] | None = None


def set_dependencies(
    *,
    research,
    activity,
    kb_ingest_research: Callable[[int], None],
) -> None:
    """Wire shared instances and helper callables for the research router."""
    global _research, _activity, _kb_ingest_research
    _research = research
    _activity = activity
    _kb_ingest_research = kb_ingest_research


# ── Routes ────────────────────────────────────────────────────────────────────


@router.post("/research")
async def create_research_topic(req: CreateResearchRequest):
    topic = _research.create_topic(
        title=req.title, description=req.description,
        submitted_by=req.submitted_by, priority=req.priority,
        tags=req.tags, scope=req.scope,
    )
    return topic.to_dict()


@router.get("/research")
async def list_research_topics(status: str = "", limit: int = 50, offset: int = 0):
    topics = _research.list_topics(status=status or None, limit=limit, offset=offset)
    return {"topics": [t.to_dict() for t in topics], "count": len(topics)}


@router.get("/research/stats")
async def research_stats():
    return _research.get_stats()


@router.get("/research/{topic_id}")
async def get_research_topic(topic_id: int):
    detail = _research.get_topic_detail(topic_id)
    if not detail:
        raise HTTPException(404, "Topic not found")
    return detail


@router.put("/research/{topic_id}")
async def update_research_topic(topic_id: int, req: UpdateResearchRequest):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    topic = _research.update_topic(topic_id, **updates)
    if not topic:
        raise HTTPException(404, "Topic not found")
    return topic.to_dict()


@router.post("/research/{topic_id}/assign")
async def assign_research(topic_id: int, req: AssignResearchRequest):
    topic = _research.assign_topic(topic_id, req.agent_name)
    if not topic:
        raise HTTPException(404, "Topic not found")
    return topic.to_dict()


@router.post("/research/{topic_id}/brief")
async def submit_research_brief(topic_id: int, req: SubmitBriefRequest):
    brief = _research.submit_brief(
        topic_id=topic_id, author_agent=req.author_agent,
        content=req.content, summary=req.summary,
        sources=req.sources, key_findings=req.key_findings,
    )
    return brief.to_dict()


@router.get("/research/{topic_id}/briefs")
async def list_research_briefs(topic_id: int):
    briefs = _research.get_briefs(topic_id)
    return {"briefs": [b.to_dict() for b in briefs], "count": len(briefs)}


@router.post("/research/{topic_id}/reviews")
async def submit_research_review(topic_id: int, req: SubmitReviewRequest):
    review = _research.submit_review(
        brief_id=req.brief_id, topic_id=topic_id,
        reviewer_agent=req.reviewer_agent, verdict=req.verdict,
        comments=req.comments, confidence=req.confidence,
        suggested_additions=req.suggested_additions,
        corrections=req.corrections,
    )
    return review.to_dict()


@router.get("/research/{topic_id}/reviews")
async def list_research_reviews(topic_id: int):
    reviews = _research.get_reviews(topic_id=topic_id)
    return {"reviews": [r.to_dict() for r in reviews], "count": len(reviews)}


@router.post("/research/{topic_id}/publish")
async def publish_research(topic_id: int):
    topic = _research.publish_topic(topic_id)
    if not topic:
        raise HTTPException(404, "Topic not found")
    _activity.log(
        agent_name=getattr(topic, "submitted_by", "") or "",
        event_type="research_published",
        title=f"Research published: {topic.title}",
        description=getattr(topic, "description", "") or "",
        metadata={"topic_id": topic.id},
    )

    # Auto-ingest published research into KB
    _kb_ingest_research(topic.id)

    return topic.to_dict()


@router.get("/research/{topic_id}/export")
async def export_research(topic_id: int, format: str = "md"):
    """Export a research brief as MD or HTML file download."""
    detail = _research.get_topic_detail(topic_id)
    if not detail:
        raise HTTPException(404, "Topic not found")
    briefs = detail.get("briefs", [])
    if not briefs:
        raise HTTPException(404, "No briefs found for this topic")
    brief = briefs[-1]  # Latest version
    reviews = detail.get("reviews", [])
    topic_data = detail["topic"]

    if format == "pdf":
        path = export_brief_pdf(topic_data, brief, reviews)
        return FileResponse(
            path,
            media_type="application/pdf",
            filename=os.path.basename(path),
        )
    elif format == "html":
        path = export_brief_html(topic_data, brief, reviews)
        return FileResponse(
            path,
            media_type="text/html",
            filename=os.path.basename(path),
        )
    else:
        path = export_brief_markdown(topic_data, brief, reviews)
        return FileResponse(
            path,
            media_type="text/markdown",
            filename=os.path.basename(path),
        )


@router.get("/research/{topic_id}/export/content")
async def export_research_content(topic_id: int, format: str = "md"):
    """Get export content inline (not as file download)."""
    detail = _research.get_topic_detail(topic_id)
    if not detail:
        raise HTTPException(404, "Topic not found")
    briefs = detail.get("briefs", [])
    if not briefs:
        raise HTTPException(404, "No briefs found for this topic")
    brief = briefs[-1]
    reviews = detail.get("reviews", [])
    topic_data = detail["topic"]

    content = get_export_content_markdown(topic_data, brief, reviews)
    return {"content": content, "format": format, "topic_id": topic_id}
