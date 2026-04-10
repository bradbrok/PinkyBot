"""Knowledge Base Store — flat-file raw sources with SQLite FTS5 index.

Inspired by Karpathy's LLM Knowledge Bases approach: separate authoritative
raw sources from derived wiki pages. This module handles:
- Filing raw sources (URLs, text, files) as markdown with YAML frontmatter
- SQLite index with FTS5 for full-text search
- Content dedup (URL exact + content preview fuzzy)
- Wiki page registry (for phase 3)

Storage layout:
    data/kb/
    ├── raw/           # Markdown files with YAML frontmatter
    ├── wiki/          # LLM-generated wiki pages
    │   ├── topics/         # Concepts, technologies, trends
    │   ├── people/         # Individuals
    │   ├── projects/       # Projects, products, initiatives
    │   ├── places/         # Locations, venues
    │   ├── events/         # Conferences, milestones
    │   ├── organizations/  # Companies, teams, institutions
    │   └── index.md
    └── kb.db          # SQLite search index
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

import yaml


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _slugify(text: str) -> str:
    """Convert text to a filesystem-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")[:80] or "untitled"


def _content_hash(content: str) -> str:
    """SHA-256 hash of content for change detection."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]


def _content_preview(content: str) -> str:
    """First ~500 chars of content body (after frontmatter) for fuzzy dedup."""
    # Strip YAML frontmatter
    match = re.match(r"^---\s*\n.*?\n---\s*\n?(.*)", content, re.DOTALL)
    body = match.group(1) if match else content
    # Normalize whitespace
    body = re.sub(r"\s+", " ", body).strip()
    return body[:500]


_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n?(.*)", re.DOTALL)


@dataclass
class RawSource:
    id: str
    title: str
    source_url: str | None
    source_type: str
    filed_at: str
    filed_by: str
    tags: list[str]
    file_path: str
    content_hash: str
    content_preview: str

    def to_dict(self, *, include_preview: bool = False) -> dict:
        d = {
            "id": self.id,
            "title": self.title,
            "source_url": self.source_url,
            "source_type": self.source_type,
            "filed_at": self.filed_at,
            "filed_by": self.filed_by,
            "tags": self.tags,
            "file_path": self.file_path,
        }
        if include_preview:
            d["content_preview"] = self.content_preview
        return d


@dataclass
class WikiPage:
    slug: str
    title: str
    last_updated: str
    sources: list[str]
    related: list[str]
    file_path: str
    content_hash: str

    def to_dict(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "last_updated": self.last_updated,
            "sources": self.sources,
            "related": self.related,
            "file_path": self.file_path,
        }


@dataclass
class KBStats:
    raw_count: int = 0
    wiki_count: int = 0
    total_tags: int = 0
    top_tags: list[tuple[str, int]] = field(default_factory=list)
    last_filed: str | None = None
    last_wiki_update: str | None = None

    def to_dict(self) -> dict:
        return {
            "raw_count": self.raw_count,
            "wiki_count": self.wiki_count,
            "total_tags": self.total_tags,
            "top_tags": [{"tag": t, "count": c} for t, c in self.top_tags],
            "last_filed": self.last_filed,
            "last_wiki_update": self.last_wiki_update,
        }


class KBStore:
    """Knowledge Base store — flat files + SQLite FTS5 index."""

    def __init__(self, data_dir: str | Path) -> None:
        self.data_dir = Path(data_dir)
        self.kb_dir = self.data_dir / "kb"
        self.raw_dir = self.kb_dir / "raw"
        self.wiki_dir = self.kb_dir / "wiki"
        self.db_path = self.kb_dir / "kb.db"

        # Ensure directories exist
        self.raw_dir.mkdir(parents=True, exist_ok=True)
        for node_type in ("topics", "people", "projects", "places", "events", "organizations"):
            (self.wiki_dir / node_type).mkdir(parents=True, exist_ok=True)

        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA foreign_keys=ON")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS raw_sources (
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    source_url TEXT,
                    source_type TEXT NOT NULL,
                    filed_at TEXT NOT NULL,
                    filed_by TEXT NOT NULL,
                    tags TEXT DEFAULT '[]',
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    content_preview TEXT DEFAULT ''
                );

                CREATE UNIQUE INDEX IF NOT EXISTS idx_raw_url
                    ON raw_sources(source_url) WHERE source_url IS NOT NULL;

                CREATE TABLE IF NOT EXISTS wiki_pages (
                    slug TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    last_updated TEXT NOT NULL,
                    sources TEXT DEFAULT '[]',
                    related TEXT DEFAULT '[]',
                    file_path TEXT NOT NULL,
                    content_hash TEXT NOT NULL
                );
            """)

            # FTS5 virtual table — create only if it doesn't exist
            try:
                conn.execute("SELECT 1 FROM fts_content LIMIT 0")
            except sqlite3.OperationalError:
                conn.execute("""
                    CREATE VIRTUAL TABLE fts_content USING fts5(
                        ref_id,
                        kind,
                        title,
                        body,
                        tags
                    )
                """)

            conn.commit()
        finally:
            conn.close()

    # ── Raw Source Management ─────────────────────────────

    def _next_raw_id(self) -> str:
        """Generate next raw source ID like 'raw-2026-04-07-001'."""
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        prefix = f"raw-{today}-"
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT id FROM raw_sources WHERE id LIKE ? ORDER BY id DESC LIMIT 1",
                (f"{prefix}%",),
            ).fetchone()
            if row:
                last_num = int(row["id"].split("-")[-1])
                return f"{prefix}{last_num + 1:03d}"
            return f"{prefix}001"
        finally:
            conn.close()

    def check_duplicate(
        self, source_url: str | None = None, content: str | None = None
    ) -> RawSource | None:
        """Check if a source already exists (by URL or content similarity)."""
        conn = self._conn()
        try:
            # Exact URL match
            if source_url:
                row = conn.execute(
                    "SELECT * FROM raw_sources WHERE source_url = ?",
                    (source_url,),
                ).fetchone()
                if row:
                    return self._row_to_source(row)

            # Content preview similarity (simple: exact match on normalized preview)
            if content:
                preview = _content_preview(content)
                if len(preview) > 50:  # Only dedup on substantial content
                    row = conn.execute(
                        "SELECT * FROM raw_sources WHERE content_preview = ?",
                        (preview,),
                    ).fetchone()
                    if row:
                        return self._row_to_source(row)

            return None
        finally:
            conn.close()

    def ingest(
        self,
        *,
        title: str,
        content: str,
        source_url: str | None = None,
        source_type: str = "note",
        filed_by: str = "unknown",
        tags: list[str] | None = None,
        owner_notes: str = "",
    ) -> RawSource:
        """File a new raw source. Returns the created source or raises on duplicate."""
        tags = tags or []
        source_id = self._next_raw_id()
        now = datetime.now(timezone.utc).isoformat()

        # Build markdown with frontmatter
        slug = _slugify(title)
        date_prefix = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        filename = f"{date_prefix}-{slug}.md"
        file_path = f"raw/{filename}"
        full_path = self.kb_dir / file_path

        # Handle filename collision
        counter = 1
        while full_path.exists():
            filename = f"{date_prefix}-{slug}-{counter}.md"
            file_path = f"raw/{filename}"
            full_path = self.kb_dir / file_path
            counter += 1

        # Build frontmatter
        frontmatter = {
            "id": source_id,
            "title": title,
            "source_type": source_type,
            "filed_at": now,
            "filed_by": filed_by,
            "tags": tags,
        }
        if source_url:
            frontmatter["source_url"] = source_url
        if owner_notes:
            frontmatter["owner_notes"] = owner_notes

        fm_yaml = yaml.dump(frontmatter, default_flow_style=False, allow_unicode=True)
        full_content = f"---\n{fm_yaml}---\n\n# {title}\n\n{content}"

        # Write file
        full_path.write_text(full_content, encoding="utf-8")

        c_hash = _content_hash(full_content)
        c_preview = _content_preview(full_content)

        # Index in SQLite
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO raw_sources
                   (id, title, source_url, source_type, filed_at, filed_by,
                    tags, file_path, content_hash, content_preview)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    source_id, title, source_url, source_type, now, filed_by,
                    json.dumps(tags), file_path, c_hash, c_preview,
                ),
            )

            # Index in FTS
            conn.execute(
                "INSERT INTO fts_content (ref_id, kind, title, body, tags) VALUES (?, ?, ?, ?, ?)",
                (source_id, "raw", title, content, " ".join(tags)),
            )

            conn.commit()
        finally:
            conn.close()

        _log(f"[KB] Filed raw source: {source_id} — {title}")

        return RawSource(
            id=source_id,
            title=title,
            source_url=source_url,
            source_type=source_type,
            filed_at=now,
            filed_by=filed_by,
            tags=tags,
            file_path=file_path,
            content_hash=c_hash,
            content_preview=c_preview,
        )

    def get_raw(self, source_id: str) -> RawSource | None:
        """Get a raw source by ID."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM raw_sources WHERE id = ?", (source_id,)
            ).fetchone()
            return self._row_to_source(row) if row else None
        finally:
            conn.close()

    def get_raw_content(self, source_id: str) -> str | None:
        """Read the full markdown content of a raw source."""
        source = self.get_raw(source_id)
        if not source:
            return None
        full_path = self.kb_dir / source.file_path
        if not full_path.exists():
            return None
        return full_path.read_text(encoding="utf-8")

    def list_raw(
        self,
        *,
        tag: str | None = None,
        source_type: str | None = None,
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[RawSource]:
        """List raw sources with optional filters.

        Args:
            tag: Filter by tag substring.
            source_type: Filter by source type.
            since: ISO timestamp — only return sources filed after this time.
            limit: Max results.
            offset: Pagination offset.
        """
        conn = self._conn()
        try:
            query = "SELECT * FROM raw_sources WHERE 1=1"
            params: list = []

            if tag:
                query += " AND tags LIKE ?"
                params.append(f'%"{tag}"%')

            if source_type:
                query += " AND source_type = ?"
                params.append(source_type)

            if since:
                query += " AND filed_at > ?"
                params.append(since)

            query += " ORDER BY filed_at DESC LIMIT ? OFFSET ?"
            params.extend([limit, offset])

            rows = conn.execute(query, params).fetchall()
            return [self._row_to_source(r) for r in rows]
        finally:
            conn.close()

    def search(
        self,
        query: str,
        *,
        scope: str = "all",
        limit: int = 20,
    ) -> list[dict]:
        """Full-text search across raw sources and/or wiki pages."""
        conn = self._conn()
        try:
            kind_filter = ""
            params: list = [query]

            if scope == "raw":
                kind_filter = " AND kind = 'raw'"
            elif scope == "wiki":
                kind_filter = " AND kind = 'wiki'"

            rows = conn.execute(
                f"""SELECT ref_id, kind, title,
                           snippet(fts_content, 3, '<mark>', '</mark>', '...', 40) as snippet,
                           rank
                    FROM fts_content
                    WHERE fts_content MATCH ?{kind_filter}
                    ORDER BY rank
                    LIMIT ?""",
                params + [limit],
            ).fetchall()

            return [
                {
                    "ref_id": r["ref_id"],
                    "kind": r["kind"],
                    "title": r["title"],
                    "snippet": r["snippet"],
                    "rank": r["rank"],
                }
                for r in rows
            ]
        finally:
            conn.close()

    def stats(self) -> KBStats:
        """Get KB statistics."""
        conn = self._conn()
        try:
            raw_count = conn.execute("SELECT COUNT(*) FROM raw_sources").fetchone()[0]
            wiki_count = conn.execute("SELECT COUNT(*) FROM wiki_pages").fetchone()[0]

            # Last filed
            last_row = conn.execute(
                "SELECT filed_at FROM raw_sources ORDER BY filed_at DESC LIMIT 1"
            ).fetchone()
            last_filed = last_row["filed_at"] if last_row else None

            # Last wiki update
            last_wiki = conn.execute(
                "SELECT last_updated FROM wiki_pages ORDER BY last_updated DESC LIMIT 1"
            ).fetchone()
            last_wiki_update = last_wiki["last_updated"] if last_wiki else None

            # Tag counts
            all_tags: dict[str, int] = {}
            for row in conn.execute("SELECT tags FROM raw_sources").fetchall():
                for tag in json.loads(row["tags"]):
                    all_tags[tag] = all_tags.get(tag, 0) + 1

            top_tags = sorted(all_tags.items(), key=lambda x: -x[1])[:15]

            return KBStats(
                raw_count=raw_count,
                wiki_count=wiki_count,
                total_tags=len(all_tags),
                top_tags=top_tags,
                last_filed=last_filed,
                last_wiki_update=last_wiki_update,
            )
        finally:
            conn.close()

    # ── Wiki Management ─────────────────────────────────────

    def save_wiki(
        self,
        slug: str,
        title: str,
        content: str,
        sources: list[str] | None = None,
        related: list[str] | None = None,
    ) -> WikiPage:
        """Create or update a wiki page.

        Writes markdown file with YAML frontmatter, upserts the DB row,
        and updates the FTS5 index for this page.

        Args:
            slug: Page slug (e.g. "topics/claude-code" or "people/boris-cherny").
            title: Page title.
            content: Full markdown body (no frontmatter — we add it).
            sources: Raw source IDs used to generate this page.
            related: Slugs of related wiki pages.

        Returns:
            The saved WikiPage.
        """
        sources = sources or []
        related = related or []
        now = datetime.now(timezone.utc).isoformat()
        file_path = f"wiki/{slug}.md"
        full_path = self.kb_dir / file_path

        # Ensure parent directory exists
        full_path.parent.mkdir(parents=True, exist_ok=True)

        # Build frontmatter + content
        frontmatter = {
            "slug": slug,
            "title": title,
            "last_updated": now,
            "sources": sources,
            "related": related,
        }
        file_content = f"---\n{yaml.dump(frontmatter, default_flow_style=False)}---\n\n{content}"
        full_path.write_text(file_content, encoding="utf-8")

        chash = _content_hash(content)

        # Upsert DB row
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO wiki_pages (slug, title, last_updated, sources, related,
                   file_path, content_hash)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(slug) DO UPDATE SET
                       title = excluded.title,
                       last_updated = excluded.last_updated,
                       sources = excluded.sources,
                       related = excluded.related,
                       file_path = excluded.file_path,
                       content_hash = excluded.content_hash
                """,
                (slug, title, now, json.dumps(sources), json.dumps(related),
                 file_path, chash),
            )

            # Update FTS index (delete old + insert new)
            conn.execute("DELETE FROM fts_content WHERE ref_id = ? AND kind = 'wiki'", (slug,))
            conn.execute(
                "INSERT INTO fts_content (ref_id, kind, title, body, tags) "
                "VALUES (?, ?, ?, ?, ?)",
                (slug, "wiki", title, content, ""),
            )
            conn.commit()
            _log(f"[KB] Saved wiki page: {slug}")
        finally:
            conn.close()

        return WikiPage(
            slug=slug, title=title, last_updated=now,
            sources=sources, related=related,
            file_path=file_path, content_hash=chash,
        )

    def delete_wiki(self, slug: str) -> bool:
        """Delete a wiki page (DB row, FTS entry, and file).

        Args:
            slug: The wiki page slug to delete.

        Returns:
            True if the page existed and was deleted.
        """
        page = self.get_wiki(slug)
        if not page:
            return False

        # Remove file
        full_path = self.kb_dir / page.file_path
        if full_path.exists():
            full_path.unlink()

        # Remove DB + FTS
        conn = self._conn()
        try:
            conn.execute("DELETE FROM wiki_pages WHERE slug = ?", (slug,))
            conn.execute("DELETE FROM fts_content WHERE ref_id = ? AND kind = 'wiki'", (slug,))
            conn.commit()
            _log(f"[KB] Deleted wiki page: {slug}")
        finally:
            conn.close()

        return True

    def get_wiki(self, slug: str) -> WikiPage | None:
        """Get a wiki page by slug."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM wiki_pages WHERE slug = ?", (slug,)
            ).fetchone()
            return self._row_to_wiki(row) if row else None
        finally:
            conn.close()

    def get_wiki_content(self, slug: str) -> str | None:
        """Read the full markdown content of a wiki page."""
        page = self.get_wiki(slug)
        if not page:
            return None
        full_path = self.kb_dir / page.file_path
        if not full_path.exists():
            return None
        return full_path.read_text(encoding="utf-8")

    def list_wiki(self, *, limit: int = 100, offset: int = 0) -> list[WikiPage]:
        """List wiki pages."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT * FROM wiki_pages ORDER BY title LIMIT ? OFFSET ?",
                (limit, offset),
            ).fetchall()
            return [self._row_to_wiki(r) for r in rows]
        finally:
            conn.close()

    # ── Reindex ────────────────────────────────────────────

    def reindex(self) -> dict:
        """Rebuild SQLite index from disk files. Safe to run anytime."""
        conn = self._conn()
        try:
            # Clear FTS
            conn.execute("DELETE FROM fts_content")

            indexed_raw = 0
            indexed_wiki = 0

            # Reindex raw sources
            rows = conn.execute(
                "SELECT id, title, tags, file_path FROM raw_sources"
            ).fetchall()
            for row in rows:
                full_path = self.kb_dir / row["file_path"]
                if full_path.exists():
                    content = full_path.read_text(encoding="utf-8")
                    body = _content_preview(content)  # Use preview for FTS body
                    # Actually use full body after frontmatter
                    match = _FRONTMATTER_RE.match(content)
                    body = match.group(2) if match else content
                    conn.execute(
                        "INSERT INTO fts_content (ref_id, kind, title, body, tags) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (row["id"], "raw", row["title"], body, row["tags"]),
                    )
                    indexed_raw += 1

            # Reindex wiki pages
            for row in conn.execute("SELECT slug, title, file_path FROM wiki_pages").fetchall():
                full_path = self.kb_dir / row["file_path"]
                if full_path.exists():
                    content = full_path.read_text(encoding="utf-8")
                    match = _FRONTMATTER_RE.match(content)
                    body = match.group(2) if match else content
                    conn.execute(
                        "INSERT INTO fts_content (ref_id, kind, title, body, tags) "
                        "VALUES (?, ?, ?, ?, ?)",
                        (row["slug"], "wiki", row["title"], body, ""),
                    )
                    indexed_wiki += 1

            conn.commit()
            _log(f"[KB] Reindexed: {indexed_raw} raw, {indexed_wiki} wiki")
            return {"raw": indexed_raw, "wiki": indexed_wiki}
        finally:
            conn.close()

    # ── Helpers ────────────────────────────────────────────

    def _row_to_source(self, row: sqlite3.Row) -> RawSource:
        return RawSource(
            id=row["id"],
            title=row["title"],
            source_url=row["source_url"],
            source_type=row["source_type"],
            filed_at=row["filed_at"],
            filed_by=row["filed_by"],
            tags=json.loads(row["tags"]),
            file_path=row["file_path"],
            content_hash=row["content_hash"],
            content_preview=row["content_preview"] or "",
        )

    def _row_to_wiki(self, row: sqlite3.Row) -> WikiPage:
        return WikiPage(
            slug=row["slug"],
            title=row["title"],
            last_updated=row["last_updated"],
            sources=json.loads(row["sources"]),
            related=json.loads(row["related"]),
            file_path=row["file_path"],
            content_hash=row["content_hash"],
        )
