"""Presentation Store — SQLite-backed versioned HTML presentations.

Agents create fully self-contained HTML presentations, which are stored here
with full version history and a public share token. The public viewer at
/p/{share_token} requires no authentication.

Schema:
    presentations(id, slug, title, description, created_by, tags, research_topic_id,
                  current_version, share_token, created_at, updated_at)
    presentation_versions(id, presentation_id, version, html_content,
                          description, created_by, created_at)
"""

from __future__ import annotations

import json
import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _slugify(text: str) -> str:
    """Convert title to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")[:80] or "presentation"


@dataclass
class Presentation:
    id: int
    slug: str
    title: str
    description: str
    created_by: str
    tags: list[str]
    research_topic_id: int | None
    current_version: int
    share_token: str
    created_at: float
    updated_at: float
    current_html: str = ""  # populated by get_with_content()

    def to_dict(self, *, include_html: bool = False) -> dict:
        d = {
            "id": self.id,
            "slug": self.slug,
            "title": self.title,
            "description": self.description,
            "created_by": self.created_by,
            "tags": self.tags,
            "research_topic_id": self.research_topic_id,
            "current_version": self.current_version,
            "share_token": self.share_token,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_html:
            d["html_content"] = self.current_html
        return d


@dataclass
class PresentationVersion:
    id: int
    presentation_id: int
    version: int
    html_content: str
    description: str
    created_by: str
    created_at: float

    def to_dict(self, *, include_html: bool = True) -> dict:
        d = {
            "id": self.id,
            "presentation_id": self.presentation_id,
            "version": self.version,
            "description": self.description,
            "created_by": self.created_by,
            "created_at": self.created_at,
        }
        if include_html:
            d["html_content"] = self.html_content
        return d


class PresentationStore:
    """SQLite-backed presentation storage with versioning."""

    def __init__(self, db_path: str = "data/presentations.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS presentations (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                slug              TEXT    NOT NULL UNIQUE,
                title             TEXT    NOT NULL,
                description       TEXT    NOT NULL DEFAULT '',
                created_by        TEXT    NOT NULL DEFAULT '',
                tags              TEXT    NOT NULL DEFAULT '[]',
                research_topic_id INTEGER,
                current_version   INTEGER NOT NULL DEFAULT 1,
                share_token       TEXT    NOT NULL UNIQUE,
                created_at        REAL    NOT NULL,
                updated_at        REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS presentation_versions (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                presentation_id INTEGER NOT NULL REFERENCES presentations(id) ON DELETE CASCADE,
                version         INTEGER NOT NULL,
                html_content    TEXT    NOT NULL DEFAULT '',
                description     TEXT    NOT NULL DEFAULT '',
                created_by      TEXT    NOT NULL DEFAULT '',
                created_at      REAL    NOT NULL,
                UNIQUE(presentation_id, version)
            );

            CREATE INDEX IF NOT EXISTS idx_pv_presentation ON presentation_versions(presentation_id);
            CREATE INDEX IF NOT EXISTS idx_p_slug           ON presentations(slug);
            CREATE INDEX IF NOT EXISTS idx_p_share          ON presentations(share_token);
            CREATE INDEX IF NOT EXISTS idx_p_topic          ON presentations(research_topic_id);
            CREATE INDEX IF NOT EXISTS idx_p_agent          ON presentations(created_by);
        """)
        self._db.commit()

    # ── Slug helpers ──────────────────────────────────────────

    def _unique_slug(self, title: str, exclude_id: int = 0) -> str:
        base = _slugify(title)
        slug = base
        n = 2
        while True:
            row = self._db.execute(
                "SELECT id FROM presentations WHERE slug=?", (slug,)
            ).fetchone()
            if not row or row[0] == exclude_id:
                return slug
            slug = f"{base}-{n}"
            n += 1

    # ── Create ────────────────────────────────────────────────

    def create(
        self,
        title: str,
        html_content: str,
        *,
        description: str = "",
        created_by: str = "",
        tags: list[str] | None = None,
        research_topic_id: int | None = None,
    ) -> Presentation:
        now = time.time()
        slug = self._unique_slug(title)
        share_token = secrets.token_hex(16)
        tags_json = json.dumps(tags or [])

        cursor = self._db.execute(
            """INSERT INTO presentations
               (slug, title, description, created_by, tags, research_topic_id,
                current_version, share_token, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?, ?)""",
            (slug, title, description, created_by, tags_json,
             research_topic_id, share_token, now, now),
        )
        presentation_id = cursor.lastrowid
        self._db.execute(
            """INSERT INTO presentation_versions
               (presentation_id, version, html_content, description, created_by, created_at)
               VALUES (?, 1, ?, ?, ?, ?)""",
            (presentation_id, html_content, description, created_by, now),
        )
        self._db.commit()
        pres = self.get(presentation_id)
        pres.current_html = html_content
        return pres

    # ── Read ──────────────────────────────────────────────────

    _P_COLS = (
        "id, slug, title, description, created_by, tags, "
        "research_topic_id, current_version, share_token, created_at, updated_at"
    )

    def _row_to_presentation(self, row: tuple) -> Presentation:
        return Presentation(
            id=row[0],
            slug=row[1],
            title=row[2],
            description=row[3],
            created_by=row[4],
            tags=json.loads(row[5] or "[]"),
            research_topic_id=row[6],
            current_version=row[7],
            share_token=row[8],
            created_at=row[9],
            updated_at=row[10],
        )

    def get(self, presentation_id: int) -> Presentation | None:
        row = self._db.execute(
            f"SELECT {self._P_COLS} FROM presentations WHERE id=?",
            (presentation_id,),
        ).fetchone()
        return self._row_to_presentation(row) if row else None

    def get_by_slug(self, slug: str) -> Presentation | None:
        row = self._db.execute(
            f"SELECT {self._P_COLS} FROM presentations WHERE slug=?",
            (slug,),
        ).fetchone()
        return self._row_to_presentation(row) if row else None

    def get_by_share_token(self, token: str) -> Presentation | None:
        row = self._db.execute(
            f"SELECT {self._P_COLS} FROM presentations WHERE share_token=?",
            (token,),
        ).fetchone()
        return self._row_to_presentation(row) if row else None

    def get_with_content(self, presentation_id: int) -> Presentation | None:
        pres = self.get(presentation_id)
        if not pres:
            return None
        ver = self.get_version(presentation_id, pres.current_version)
        pres.current_html = ver.html_content if ver else ""
        return pres

    def get_by_share_token_with_content(self, token: str) -> Presentation | None:
        pres = self.get_by_share_token(token)
        if not pres:
            return None
        ver = self.get_version(pres.id, pres.current_version)
        pres.current_html = ver.html_content if ver else ""
        return pres

    def list(
        self,
        *,
        tag: str = "",
        created_by: str = "",
        research_topic_id: int | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Presentation]:
        conditions = []
        params: list = []
        if created_by:
            conditions.append("created_by=?")
            params.append(created_by)
        if research_topic_id is not None:
            conditions.append("research_topic_id=?")
            params.append(research_topic_id)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._db.execute(
            f"SELECT {self._P_COLS} FROM presentations {where} "
            f"ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        ).fetchall()
        results = [self._row_to_presentation(r) for r in rows]
        # Filter by tag in Python (tags stored as JSON array)
        if tag:
            results = [p for p in results if tag in p.tags]
        return results

    # ── Update ────────────────────────────────────────────────

    def update(
        self,
        presentation_id: int,
        html_content: str,
        *,
        description: str = "",
        created_by: str = "",
        title: str | None = None,
        tags: list[str] | None = None,
    ) -> Presentation | None:
        pres = self.get(presentation_id)
        if not pres:
            return None
        now = time.time()
        new_version = pres.current_version + 1

        self._db.execute(
            """INSERT INTO presentation_versions
               (presentation_id, version, html_content, description, created_by, created_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (presentation_id, new_version, html_content, description, created_by, now),
        )

        updates = ["current_version=?", "updated_at=?"]
        params: list = [new_version, now]
        if title is not None:
            updates.append("title=?")
            params.append(title)
            updates.append("slug=?")
            params.append(self._unique_slug(title, exclude_id=presentation_id))
        if tags is not None:
            updates.append("tags=?")
            params.append(json.dumps(tags))

        params.append(presentation_id)
        self._db.execute(
            f"UPDATE presentations SET {', '.join(updates)} WHERE id=?",
            params,
        )
        self._db.commit()
        updated = self.get(presentation_id)
        updated.current_html = html_content
        return updated

    def restore_version(self, presentation_id: int, version: int) -> Presentation | None:
        """Set current_version to a previous version (no new row inserted)."""
        ver = self.get_version(presentation_id, version)
        if not ver:
            return None
        self._db.execute(
            "UPDATE presentations SET current_version=?, updated_at=? WHERE id=?",
            (version, time.time(), presentation_id),
        )
        self._db.commit()
        return self.get_with_content(presentation_id)

    # ── Delete ────────────────────────────────────────────────

    def delete(self, presentation_id: int) -> bool:
        cursor = self._db.execute(
            "DELETE FROM presentations WHERE id=?", (presentation_id,)
        )
        self._db.commit()
        return cursor.rowcount > 0

    # ── Versions ──────────────────────────────────────────────

    _V_COLS = (
        "id, presentation_id, version, html_content, description, created_by, created_at"
    )

    def _row_to_version(self, row: tuple) -> PresentationVersion:
        return PresentationVersion(
            id=row[0],
            presentation_id=row[1],
            version=row[2],
            html_content=row[3],
            description=row[4],
            created_by=row[5],
            created_at=row[6],
        )

    def get_versions(self, presentation_id: int) -> list[PresentationVersion]:
        rows = self._db.execute(
            f"SELECT {self._V_COLS} FROM presentation_versions "
            f"WHERE presentation_id=? ORDER BY version ASC",
            (presentation_id,),
        ).fetchall()
        return [self._row_to_version(r) for r in rows]

    def get_version(self, presentation_id: int, version: int) -> PresentationVersion | None:
        row = self._db.execute(
            f"SELECT {self._V_COLS} FROM presentation_versions "
            f"WHERE presentation_id=? AND version=?",
            (presentation_id, version),
        ).fetchone()
        return self._row_to_version(row) if row else None

    # ── Stats ─────────────────────────────────────────────────

    def get_stats(self) -> dict:
        total = self._db.execute("SELECT COUNT(*) FROM presentations").fetchone()[0]
        by_agent = self._db.execute(
            "SELECT created_by, COUNT(*) FROM presentations GROUP BY created_by ORDER BY COUNT(*) DESC"
        ).fetchall()
        return {
            "total": total,
            "by_agent": {row[0]: row[1] for row in by_agent},
        }

    def close(self) -> None:
        self._db.close()
