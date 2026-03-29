"""Research Store — SQLite-backed research pipeline for multi-agent peer review.

Topics > Briefs > Reviews hierarchy with versioned drafts and peer review.
Designed for fleet coordination: agents research topics, submit briefs,
and peer-review each other's findings before publication.

Schema:
    research_topics(id, title, description, submitted_by, status, assigned_agent,
                    reviewer_agents, priority, tags, scope, created_at, updated_at)
    research_briefs(id, topic_id, author_agent, version, content, summary,
                    sources, key_findings, status, created_at, published_at)
    peer_reviews(id, brief_id, topic_id, reviewer_agent, verdict, comments,
                 suggested_additions, corrections, confidence, created_at)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


VALID_TOPIC_STATUSES = (
    "open", "assigned", "researching", "in_review", "revising", "published", "cancelled",
)
VALID_PRIORITIES = ("low", "normal", "high", "urgent")
VALID_VERDICTS = ("approve", "request_changes", "reject")
VALID_BRIEF_STATUSES = ("draft", "in_review", "revised", "published")


@dataclass
class ResearchTopic:
    """A research topic that groups briefs and reviews."""

    id: int = 0
    title: str = ""
    description: str = ""
    submitted_by: str = ""
    status: str = "open"
    assigned_agent: str = ""
    reviewer_agents: list[str] = field(default_factory=list)
    priority: str = "normal"
    tags: list[str] = field(default_factory=list)
    scope: str = ""
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "submitted_by": self.submitted_by,
            "status": self.status,
            "assigned_agent": self.assigned_agent,
            "reviewer_agents": self.reviewer_agents,
            "priority": self.priority,
            "tags": self.tags,
            "scope": self.scope,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


@dataclass
class ResearchBrief:
    """A research brief — the deliverable attached to a topic."""

    id: int = 0
    topic_id: int = 0
    author_agent: str = ""
    version: int = 1
    content: str = ""
    summary: str = ""
    sources: list[str] = field(default_factory=list)
    key_findings: list[str] = field(default_factory=list)
    status: str = "draft"
    created_at: float = 0.0
    published_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "topic_id": self.topic_id,
            "author_agent": self.author_agent,
            "version": self.version,
            "content": self.content,
            "summary": self.summary,
            "sources": self.sources,
            "key_findings": self.key_findings,
            "status": self.status,
            "created_at": self.created_at,
            "published_at": self.published_at,
        }


@dataclass
class PeerReview:
    """A peer review of a research brief."""

    id: int = 0
    brief_id: int = 0
    topic_id: int = 0
    reviewer_agent: str = ""
    verdict: str = ""
    comments: str = ""
    suggested_additions: list[str] = field(default_factory=list)
    corrections: list[str] = field(default_factory=list)
    confidence: int = 3
    created_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "brief_id": self.brief_id,
            "topic_id": self.topic_id,
            "reviewer_agent": self.reviewer_agent,
            "verdict": self.verdict,
            "comments": self.comments,
            "suggested_additions": self.suggested_additions,
            "corrections": self.corrections,
            "confidence": self.confidence,
            "created_at": self.created_at,
        }


class ResearchStore:
    """SQLite-backed research pipeline management."""

    def __init__(self, db_path: str = "data/research.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS research_topics (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                title TEXT NOT NULL,
                description TEXT NOT NULL DEFAULT '',
                submitted_by TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'open',
                assigned_agent TEXT NOT NULL DEFAULT '',
                reviewer_agents TEXT NOT NULL DEFAULT '[]',
                priority TEXT NOT NULL DEFAULT 'normal',
                tags TEXT NOT NULL DEFAULT '[]',
                scope TEXT NOT NULL DEFAULT '',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );

            CREATE TABLE IF NOT EXISTS research_briefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                topic_id INTEGER NOT NULL,
                author_agent TEXT NOT NULL DEFAULT '',
                version INTEGER NOT NULL DEFAULT 1,
                content TEXT NOT NULL DEFAULT '',
                summary TEXT NOT NULL DEFAULT '',
                sources TEXT NOT NULL DEFAULT '[]',
                key_findings TEXT NOT NULL DEFAULT '[]',
                status TEXT NOT NULL DEFAULT 'draft',
                created_at REAL NOT NULL,
                published_at REAL NOT NULL DEFAULT 0.0,
                FOREIGN KEY (topic_id) REFERENCES research_topics(id)
            );

            CREATE TABLE IF NOT EXISTS peer_reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                brief_id INTEGER NOT NULL,
                topic_id INTEGER NOT NULL,
                reviewer_agent TEXT NOT NULL DEFAULT '',
                verdict TEXT NOT NULL DEFAULT '',
                comments TEXT NOT NULL DEFAULT '',
                suggested_additions TEXT NOT NULL DEFAULT '[]',
                corrections TEXT NOT NULL DEFAULT '[]',
                confidence INTEGER NOT NULL DEFAULT 3,
                created_at REAL NOT NULL,
                FOREIGN KEY (brief_id) REFERENCES research_briefs(id),
                FOREIGN KEY (topic_id) REFERENCES research_topics(id)
            );

            CREATE INDEX IF NOT EXISTS idx_topics_status ON research_topics(status);
            CREATE INDEX IF NOT EXISTS idx_topics_agent ON research_topics(assigned_agent);
            CREATE INDEX IF NOT EXISTS idx_briefs_topic ON research_briefs(topic_id);
            CREATE INDEX IF NOT EXISTS idx_reviews_brief ON peer_reviews(brief_id);
            CREATE INDEX IF NOT EXISTS idx_reviews_topic ON peer_reviews(topic_id);
        """)
        self._db.commit()

    # ── Topics ────────────────────────────────────────────

    def create_topic(
        self,
        title: str,
        *,
        description: str = "",
        submitted_by: str = "",
        priority: str = "normal",
        tags: list[str] | None = None,
        scope: str = "",
    ) -> ResearchTopic:
        """Create a new research topic."""
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO research_topics
               (title, description, submitted_by, status, assigned_agent,
                reviewer_agents, priority, tags, scope, created_at, updated_at)
               VALUES (?, ?, ?, 'open', '', '[]', ?, ?, ?, ?, ?)""",
            (title, description, submitted_by, priority,
             json.dumps(tags or []), scope, now, now),
        )
        self._db.commit()
        _log(f"research: created topic #{cursor.lastrowid} '{title}'")
        return self.get_topic(cursor.lastrowid)  # type: ignore

    def get_topic(self, topic_id: int) -> ResearchTopic | None:
        """Get a topic by ID."""
        row = self._db.execute(
            "SELECT * FROM research_topics WHERE id=?", (topic_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_topic(row)

    def list_topics(
        self,
        *,
        status: str | None = None,
        assigned_agent: str = "",
        tag: str = "",
        include_cancelled: bool = False,
        limit: int = 100,
        offset: int = 0,
    ) -> list[ResearchTopic]:
        """List topics with optional filters."""
        conditions: list[str] = []
        params: list = []

        if status:
            conditions.append("status=?")
            params.append(status)
        elif not include_cancelled:
            conditions.append("status != 'cancelled'")

        if assigned_agent:
            conditions.append("assigned_agent=?")
            params.append(assigned_agent)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        rows = self._db.execute(
            f"""SELECT * FROM research_topics {where}
                ORDER BY
                    CASE priority WHEN 'urgent' THEN 0 WHEN 'high' THEN 1
                                  WHEN 'normal' THEN 2 WHEN 'low' THEN 3 END,
                    created_at DESC
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()

        topics = [self._row_to_topic(r) for r in rows]

        # Filter by tag in Python (JSON array)
        if tag:
            topics = [t for t in topics if tag in t.tags]

        return topics

    def update_topic(self, topic_id: int, **kwargs) -> ResearchTopic | None:
        """Update topic fields. Only provided kwargs are changed."""
        topic = self.get_topic(topic_id)
        if not topic:
            return None

        updates: dict = {}
        for key in ("title", "description", "status", "priority",
                     "assigned_agent", "scope", "submitted_by"):
            if key in kwargs:
                updates[key] = kwargs[key]

        if "tags" in kwargs:
            updates["tags"] = json.dumps(kwargs["tags"])
        if "reviewer_agents" in kwargs:
            updates["reviewer_agents"] = json.dumps(kwargs["reviewer_agents"])

        if not updates:
            return topic

        updates["updated_at"] = time.time()
        set_clause = ", ".join(f"{k}=?" for k in updates)
        self._db.execute(
            f"UPDATE research_topics SET {set_clause} WHERE id=?",
            list(updates.values()) + [topic_id],
        )
        self._db.commit()
        return self.get_topic(topic_id)

    def get_stats(self) -> dict[str, int]:
        """Get topic counts grouped by status."""
        rows = self._db.execute(
            "SELECT status, COUNT(*) FROM research_topics GROUP BY status"
        ).fetchall()
        return {r[0]: r[1] for r in rows}

    def assign_topic(self, topic_id: int, agent_name: str) -> ResearchTopic | None:
        """Assign a researcher to a topic."""
        return self.update_topic(
            topic_id, assigned_agent=agent_name, status="assigned",
        )

    # ── Briefs ────────────────────────────────────────────

    def submit_brief(
        self,
        topic_id: int,
        author_agent: str,
        *,
        content: str,
        summary: str = "",
        sources: list[str] | None = None,
        key_findings: list[str] | None = None,
    ) -> ResearchBrief:
        """Submit a draft or revised brief for a topic."""
        # Auto-increment version
        latest = self.get_latest_brief(topic_id)
        version = (latest.version + 1) if latest else 1

        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO research_briefs
               (topic_id, author_agent, version, content, summary,
                sources, key_findings, status, created_at, published_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, 0.0)""",
            (topic_id, author_agent, version, content, summary,
             json.dumps(sources or []), json.dumps(key_findings or []),
             now),
        )
        self._db.commit()
        _log(f"research: brief v{version} submitted for topic #{topic_id} by {author_agent}")
        return self.get_brief(cursor.lastrowid)  # type: ignore

    def get_brief(self, brief_id: int) -> ResearchBrief | None:
        """Get a brief by ID."""
        row = self._db.execute(
            "SELECT * FROM research_briefs WHERE id=?", (brief_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_brief(row)

    def get_latest_brief(self, topic_id: int) -> ResearchBrief | None:
        """Get the latest brief version for a topic."""
        row = self._db.execute(
            "SELECT * FROM research_briefs WHERE topic_id=? ORDER BY version DESC LIMIT 1",
            (topic_id,),
        ).fetchone()
        if not row:
            return None
        return self._row_to_brief(row)

    def get_briefs(self, topic_id: int) -> list[ResearchBrief]:
        """List all brief versions for a topic."""
        rows = self._db.execute(
            "SELECT * FROM research_briefs WHERE topic_id=? ORDER BY version ASC",
            (topic_id,),
        ).fetchall()
        return [self._row_to_brief(r) for r in rows]

    def update_brief(self, brief_id: int, **kwargs) -> ResearchBrief | None:
        """Update brief fields."""
        brief = self.get_brief(brief_id)
        if not brief:
            return None

        updates: dict = {}
        for key in ("status", "published_at"):
            if key in kwargs:
                updates[key] = kwargs[key]

        if "sources" in kwargs:
            updates["sources"] = json.dumps(kwargs["sources"])
        if "key_findings" in kwargs:
            updates["key_findings"] = json.dumps(kwargs["key_findings"])

        if not updates:
            return brief

        set_clause = ", ".join(f"{k}=?" for k in updates)
        self._db.execute(
            f"UPDATE research_briefs SET {set_clause} WHERE id=?",
            list(updates.values()) + [brief_id],
        )
        self._db.commit()
        return self.get_brief(brief_id)

    # ── Reviews ───────────────────────────────────────────

    def submit_review(
        self,
        brief_id: int,
        topic_id: int,
        reviewer_agent: str,
        *,
        verdict: str,
        comments: str = "",
        confidence: int = 3,
        suggested_additions: list[str] | None = None,
        corrections: list[str] | None = None,
    ) -> PeerReview:
        """Submit a peer review for a brief."""
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO peer_reviews
               (brief_id, topic_id, reviewer_agent, verdict, comments,
                suggested_additions, corrections, confidence, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (brief_id, topic_id, reviewer_agent, verdict, comments,
             json.dumps(suggested_additions or []),
             json.dumps(corrections or []),
             confidence, now),
        )
        self._db.commit()
        _log(f"research: review submitted for brief #{brief_id} by {reviewer_agent}: {verdict}")
        return self._get_review(cursor.lastrowid)  # type: ignore

    def _get_review(self, review_id: int) -> PeerReview | None:
        """Get a review by ID."""
        row = self._db.execute(
            "SELECT * FROM peer_reviews WHERE id=?", (review_id,)
        ).fetchone()
        if not row:
            return None
        return self._row_to_review(row)

    def get_reviews(
        self,
        *,
        topic_id: int | None = None,
        brief_id: int | None = None,
    ) -> list[PeerReview]:
        """Get reviews filtered by topic_id and/or brief_id."""
        conditions: list[str] = []
        params: list = []

        if topic_id is not None:
            conditions.append("topic_id=?")
            params.append(topic_id)
        if brief_id is not None:
            conditions.append("brief_id=?")
            params.append(brief_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._db.execute(
            f"SELECT * FROM peer_reviews {where} ORDER BY created_at ASC",
            params,
        ).fetchall()
        return [self._row_to_review(r) for r in rows]

    # ── Composite Queries ─────────────────────────────────

    def get_topic_detail(self, topic_id: int) -> dict | None:
        """Get full topic detail: topic + briefs + reviews + timeline."""
        topic = self.get_topic(topic_id)
        if not topic:
            return None

        briefs = self.get_briefs(topic_id)
        reviews = self.get_reviews(topic_id=topic_id)

        # Build timeline from all events
        timeline: list[dict] = []
        timeline.append({
            "event": "created",
            "timestamp": topic.created_at,
            "actor": topic.submitted_by,
            "detail": f"Topic created: {topic.title}",
        })
        if topic.assigned_agent:
            timeline.append({
                "event": "assigned",
                "timestamp": topic.updated_at,
                "actor": topic.assigned_agent,
                "detail": f"Assigned to {topic.assigned_agent}",
            })
        for brief in briefs:
            timeline.append({
                "event": "brief_submitted",
                "timestamp": brief.created_at,
                "actor": brief.author_agent,
                "detail": f"Brief v{brief.version} submitted",
            })
            if brief.published_at > 0:
                timeline.append({
                    "event": "brief_published",
                    "timestamp": brief.published_at,
                    "actor": brief.author_agent,
                    "detail": f"Brief v{brief.version} published",
                })
        for review in reviews:
            timeline.append({
                "event": "review_submitted",
                "timestamp": review.created_at,
                "actor": review.reviewer_agent,
                "detail": f"Review by {review.reviewer_agent}: {review.verdict}",
            })

        timeline.sort(key=lambda e: e["timestamp"])

        return {
            "topic": topic.to_dict(),
            "briefs": [b.to_dict() for b in briefs],
            "reviews": [r.to_dict() for r in reviews],
            "timeline": timeline,
        }

    def publish_topic(self, topic_id: int) -> ResearchTopic | None:
        """Publish a topic — marks latest brief as published and updates topic status."""
        topic = self.get_topic(topic_id)
        if not topic:
            return None

        latest_brief = self.get_latest_brief(topic_id)
        if latest_brief:
            self.update_brief(
                latest_brief.id,
                status="published",
                published_at=time.time(),
            )

        return self.update_topic(topic_id, status="published")

    # ── Row Converters ────────────────────────────────────

    def _row_to_topic(self, row: tuple) -> ResearchTopic:
        return ResearchTopic(
            id=row[0],
            title=row[1],
            description=row[2],
            submitted_by=row[3],
            status=row[4],
            assigned_agent=row[5],
            reviewer_agents=json.loads(row[6]),
            priority=row[7],
            tags=json.loads(row[8]),
            scope=row[9],
            created_at=row[10],
            updated_at=row[11],
        )

    def _row_to_brief(self, row: tuple) -> ResearchBrief:
        return ResearchBrief(
            id=row[0],
            topic_id=row[1],
            author_agent=row[2],
            version=row[3],
            content=row[4],
            summary=row[5],
            sources=json.loads(row[6]),
            key_findings=json.loads(row[7]),
            status=row[8],
            created_at=row[9],
            published_at=row[10],
        )

    def _row_to_review(self, row: tuple) -> PeerReview:
        return PeerReview(
            id=row[0],
            brief_id=row[1],
            topic_id=row[2],
            reviewer_agent=row[3],
            verdict=row[4],
            comments=row[5],
            suggested_additions=json.loads(row[6]),
            corrections=json.loads(row[7]),
            confidence=row[8],
            created_at=row[9],
        )

    def close(self) -> None:
        self._db.close()
