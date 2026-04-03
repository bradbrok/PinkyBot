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
class PresentationTemplate:
    id: int
    name: str
    description: str
    tags: list[str]
    thumbnail_css: str
    html_content: str
    is_builtin: bool
    created_at: float

    def to_dict(self, *, include_html: bool = True) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "tags": self.tags,
            "thumbnail_css": self.thumbnail_css,
            "is_builtin": self.is_builtin,
            "created_at": self.created_at,
        }
        if include_html:
            d["html_content"] = self.html_content
        return d


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

            CREATE TABLE IF NOT EXISTS presentation_templates (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL,
                description     TEXT    NOT NULL DEFAULT '',
                tags            TEXT    NOT NULL DEFAULT '[]',
                thumbnail_css   TEXT    NOT NULL DEFAULT '',
                html_content    TEXT    NOT NULL DEFAULT '',
                is_builtin      INTEGER NOT NULL DEFAULT 0,
                created_at      REAL    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_pt_builtin ON presentation_templates(is_builtin);
        """)
        self._db.commit()
        self._seed_templates()

    # ── Template helpers ──────────────────────────────────────

    _BUILTIN_TEMPLATES: list[dict] = []  # populated below class definition

    _T_COLS = "id, name, description, tags, thumbnail_css, html_content, is_builtin, created_at"

    def _row_to_template(self, row: tuple) -> PresentationTemplate:
        return PresentationTemplate(
            id=row[0],
            name=row[1],
            description=row[2],
            tags=json.loads(row[3] or "[]"),
            thumbnail_css=row[4],
            html_content=row[5],
            is_builtin=bool(row[6]),
            created_at=row[7],
        )

    def _seed_templates(self) -> None:
        count = self._db.execute(
            "SELECT COUNT(*) FROM presentation_templates WHERE is_builtin=1"
        ).fetchone()[0]
        if count >= len(self._BUILTIN_TEMPLATES):
            return
        now = time.time()
        for t in self._BUILTIN_TEMPLATES:
            existing = self._db.execute(
                "SELECT id FROM presentation_templates WHERE name=? AND is_builtin=1",
                (t["name"],),
            ).fetchone()
            if existing:
                continue
            self._db.execute(
                """INSERT INTO presentation_templates
                   (name, description, tags, thumbnail_css, html_content, is_builtin, created_at)
                   VALUES (?, ?, ?, ?, ?, 1, ?)""",
                (t["name"], t["description"], json.dumps(t["tags"]),
                 t["thumbnail_css"], t["html_content"], now),
            )
        self._db.commit()
        _log(f"[presentation_store] seeded {len(self._BUILTIN_TEMPLATES)} built-in templates")

    def list_templates(self, tag: str = "") -> list[PresentationTemplate]:
        rows = self._db.execute(
            f"SELECT {self._T_COLS} FROM presentation_templates ORDER BY is_builtin DESC, id ASC"
        ).fetchall()
        results = [self._row_to_template(r) for r in rows]
        if tag:
            results = [t for t in results if tag in t.tags]
        return results

    def get_template(self, template_id: int) -> PresentationTemplate | None:
        row = self._db.execute(
            f"SELECT {self._T_COLS} FROM presentation_templates WHERE id=?",
            (template_id,),
        ).fetchone()
        return self._row_to_template(row) if row else None

    def create_template(
        self,
        name: str,
        html_content: str,
        *,
        description: str = "",
        tags: list[str] | None = None,
        thumbnail_css: str = "",
    ) -> PresentationTemplate:
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO presentation_templates
               (name, description, tags, thumbnail_css, html_content, is_builtin, created_at)
               VALUES (?, ?, ?, ?, ?, 0, ?)""",
            (name, description, json.dumps(tags or []), thumbnail_css, html_content, now),
        )
        self._db.commit()
        return self.get_template(cursor.lastrowid)

    def delete_template(self, template_id: int) -> bool:
        tmpl = self.get_template(template_id)
        if not tmpl or tmpl.is_builtin:
            return False
        cursor = self._db.execute(
            "DELETE FROM presentation_templates WHERE id=? AND is_builtin=0",
            (template_id,),
        )
        self._db.commit()
        return cursor.rowcount > 0

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


# ── Built-in template definitions ────────────────────────────────────────────

_TEMPLATE_DEFAULT_DARK = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@300;400;500;600;700&family=Space+Mono:wght@400;700&display=swap');
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #0d0d0f; --surface: #141417; --surface2: #1c1c21;
    --border: #2a2a32; --text: #e8e8f0; --muted: #666680;
    --accent: #7c6af7; --accent2: #f7c56a; --accent3: #6af7b8;
    --font: 'Space Grotesk', system-ui, sans-serif;
    --mono: 'Space Mono', monospace;
  }
  html, body { width:100%; height:100%; overflow:hidden; background:var(--bg); color:var(--text); font-family:var(--font); }
  .deck { width:100%; height:100%; position:relative; }
  .slide {
    position:absolute; inset:0; display:flex; flex-direction:column;
    justify-content:center; align-items:center; padding:4rem;
    opacity:0; pointer-events:none;
    transition:opacity 0.4s ease, transform 0.4s ease;
    transform:translateX(40px);
  }
  .slide.active { opacity:1; pointer-events:all; transform:translateX(0); }
  .slide.prev { transform:translateX(-40px); }
  .tag {
    font-family:var(--mono); font-size:0.7rem; letter-spacing:0.15em;
    text-transform:uppercase; color:var(--accent);
    background:rgba(124,106,247,0.12); border:1px solid rgba(124,106,247,0.25);
    padding:0.25rem 0.75rem; border-radius:999px; margin-bottom:1.5rem;
  }
  h1 { font-size:clamp(2rem,5vw,3.5rem); font-weight:700; line-height:1.1; text-align:center; }
  h2 { font-size:clamp(1.5rem,3vw,2.2rem); font-weight:600; line-height:1.2; margin-bottom:1rem; }
  .sub { color:var(--muted); font-size:1.05rem; text-align:center; margin-top:1rem; max-width:540px; line-height:1.6; }
  .highlight  { color:var(--accent);  }
  .highlight2 { color:var(--accent2); }
  .highlight3 { color:var(--accent3); }
  .grid { display:grid; gap:1rem; width:100%; max-width:800px; }
  .grid-3 { grid-template-columns:1fr 1fr 1fr; }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.25rem 1.5rem; }
  .card .icon { font-size:1.5rem; margin-bottom:0.5rem; }
  .card h3 { font-size:0.95rem; font-weight:600; margin-bottom:0.4rem; }
  .card p { font-size:0.82rem; color:var(--muted); line-height:1.55; }
  .title-slide { background:radial-gradient(ellipse at 60% 40%,rgba(124,106,247,0.12) 0%,transparent 60%),radial-gradient(ellipse at 20% 80%,rgba(106,247,184,0.06) 0%,transparent 50%); }
  .wordmark { font-family:var(--mono); font-size:0.85rem; color:var(--muted); margin-bottom:2rem; letter-spacing:0.2em; }
  .nav {
    position:fixed; bottom:2rem; left:50%; transform:translateX(-50%);
    display:flex; align-items:center; gap:1rem; z-index:100;
    background:var(--surface2); border:1px solid var(--border);
    border-radius:999px; padding:0.5rem 1rem;
  }
  .nav button { background:none; border:none; color:var(--text); cursor:pointer; font-size:1.1rem; padding:0.3rem 0.6rem; border-radius:6px; transition:background 0.15s; }
  .nav button:hover { background:var(--border); }
  .nav button:disabled { color:var(--muted); cursor:default; }
  .counter { font-family:var(--mono); font-size:0.8rem; color:var(--muted); min-width:3rem; text-align:center; }
  .dots { display:flex; gap:0.4rem; align-items:center; }
  .dot { width:6px; height:6px; border-radius:50%; background:var(--border); transition:background 0.2s,transform 0.2s; cursor:pointer; }
  .dot.active { background:var(--accent); transform:scale(1.4); }
</style>
</head>
<body>
<div class="deck" id="deck">

  <!-- SLIDE 1: Title -->
  <div class="slide title-slide active" data-index="0">
    <div class="wordmark">PINKYBOT</div>
    <div class="tag">{{TAG_1}}</div>
    <h1>{{TITLE_LINE_1}}<br><span class="highlight">{{TITLE_LINE_2}}</span></h1>
    <p class="sub">{{SUBTITLE}}</p>
  </div>

  <!-- SLIDE 2: Section with cards -->
  <div class="slide" data-index="1">
    <div class="tag">{{TAG_2}}</div>
    <h2>{{HEADING_2}} with a <span class="highlight">key word</span></h2>
    <div class="grid grid-3" style="margin-top:2rem;">
      <div class="card"><div class="icon">📌</div><h3>Point 1</h3><p>Description of the first key idea goes here.</p></div>
      <div class="card"><div class="icon">📌</div><h3>Point 2</h3><p>Description of the second key idea goes here.</p></div>
      <div class="card"><div class="icon">📌</div><h3>Point 3</h3><p>Description of the third key idea goes here.</p></div>
    </div>
  </div>

  <!-- SLIDE 3: Closing -->
  <div class="slide title-slide" data-index="2">
    <div class="tag">{{CLOSING_TAG}}</div>
    <h1>{{CLOSING_LINE_1}}<br><span class="highlight3">{{CLOSING_LINE_2}}</span></h1>
    <p class="sub">{{CLOSING_SUBTITLE}}</p>
  </div>

</div>
<div class="nav">
  <button id="prev" onclick="go(-1)" disabled>←</button>
  <div class="dots" id="dots"></div>
  <span class="counter" id="counter"></span>
  <button id="next" onclick="go(1)">→</button>
</div>
<script>
  const slides = document.querySelectorAll('.slide');
  const dotsEl = document.getElementById('dots');
  const counter = document.getElementById('counter');
  let current = 0;
  slides.forEach((_,i) => {
    const d = document.createElement('div');
    d.className = 'dot' + (i===0?' active':'');
    d.onclick = () => goTo(i);
    dotsEl.appendChild(d);
  });
  counter.textContent = '1 / ' + slides.length;
  function goTo(n) {
    slides[current].classList.remove('active');
    slides[current].classList.add('prev');
    setTimeout(() => slides[current].classList.remove('prev'), 400);
    current = n;
    slides[current].classList.add('active');
    document.querySelectorAll('.dot').forEach((d,i) => d.classList.toggle('active',i===current));
    counter.textContent = (current+1) + ' / ' + slides.length;
    document.getElementById('prev').disabled = current===0;
    document.getElementById('next').disabled = current===slides.length-1;
  }
  function go(dir) { const n=current+dir; if(n>=0&&n<slides.length) goTo(n); }
  document.addEventListener('keydown', e => {
    if(e.key==='ArrowRight'||e.key===' ') go(1);
    if(e.key==='ArrowLeft') go(-1);
  });
</script>
</body>
</html>'''

_TEMPLATE_MINIMAL = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<style>
  @import url('https://fonts.googleapis.com/css2?family=Space+Grotesk:wght@400;600;700&family=Space+Mono&display=swap');
  :root { --bg:#0d0d0f; --surface:#141417; --border:#2a2a32; --text:#e8e8f0; --muted:#666680; --accent:#7c6af7; --accent2:#f7c56a; --accent3:#6af7b8; }
  *, *::before, *::after { box-sizing:border-box; margin:0; padding:0; }
  body { font-family:'Space Grotesk',sans-serif; background:var(--bg); color:var(--text); }
  .slide { min-height:100vh; display:flex; flex-direction:column; justify-content:center; align-items:center; padding:4rem; border-bottom:1px solid var(--border); }
  h1 { font-size:2.5rem; font-weight:700; text-align:center; }
  h2 { font-size:1.8rem; font-weight:600; margin-bottom:1rem; }
  .sub { color:var(--muted); font-size:1rem; text-align:center; margin-top:1rem; max-width:540px; line-height:1.6; }
  .tag { font-family:'Space Mono',monospace; font-size:0.7rem; letter-spacing:0.15em; text-transform:uppercase; color:var(--accent); background:rgba(124,106,247,0.12); border:1px solid rgba(124,106,247,0.25); padding:0.25rem 0.75rem; border-radius:999px; margin-bottom:1.5rem; }
  .highlight { color:var(--accent); } .highlight2 { color:var(--accent2); } .highlight3 { color:var(--accent3); }
  .card { background:var(--surface); border:1px solid var(--border); border-radius:12px; padding:1.25rem 1.5rem; }
  .grid { display:grid; gap:1rem; width:100%; max-width:800px; }
  .grid-2 { grid-template-columns:1fr 1fr; } .grid-3 { grid-template-columns:1fr 1fr 1fr; }
</style>
</head>
<body>
  <!-- Scrollable slides — print-safe, no JS transitions -->
  <div class="slide">
    <div class="tag">{{TAG_1}}</div>
    <h1>{{TITLE_LINE_1}}<br><span class="highlight">{{TITLE_LINE_2}}</span></h1>
    <p class="sub">{{SUBTITLE}}</p>
  </div>

  <div class="slide">
    <div class="tag">{{TAG_2}}</div>
    <h2>{{HEADING_2}} with a <span class="highlight">key point</span></h2>
    <div class="grid grid-3" style="margin-top:2rem;">
      <div class="card"><h3>Point 1</h3><p style="font-size:0.82rem;color:var(--muted);margin-top:0.4rem;">Description here.</p></div>
      <div class="card"><h3>Point 2</h3><p style="font-size:0.82rem;color:var(--muted);margin-top:0.4rem;">Description here.</p></div>
      <div class="card"><h3>Point 3</h3><p style="font-size:0.82rem;color:var(--muted);margin-top:0.4rem;">Description here.</p></div>
    </div>
  </div>

  <div class="slide">
    <div class="tag">{{CLOSING_TAG}}</div>
    <h1>{{CLOSING_LINE_1}}<br><span class="highlight3">{{CLOSING_LINE_2}}</span></h1>
    <p class="sub">{{CLOSING_SUBTITLE}}</p>
  </div>
</body>
</html>'''

_TEMPLATE_FIGMA = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@300;400;500;600;700&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #FFFFFF; --surface: #F9FAFB; --surface2: #F3F4F6;
    --border: #E5E7EB; --text: #111827; --muted: #6B7280;
    --accent: #7C3AED; --accent-light: rgba(124,58,237,0.08);
    --accent-border: rgba(124,58,237,0.2);
    --font: 'Inter', system-ui, sans-serif;
  }
  html, body { width:100%; height:100%; overflow:hidden; background:var(--bg); color:var(--text); font-family:var(--font); }
  .deck { width:100%; height:100%; position:relative; }
  .slide {
    position:absolute; inset:0; display:flex; flex-direction:column;
    justify-content:center; align-items:center; padding:4rem;
    opacity:0; pointer-events:none;
    transition:opacity 0.35s ease, transform 0.35s ease;
    transform:translateX(32px);
  }
  .slide.active { opacity:1; pointer-events:all; transform:translateX(0); }
  .slide.prev { transform:translateX(-32px); }
  .slide-inner { width:100%; max-width:860px; }
  .tag {
    display:inline-block; font-family:var(--font); font-size:0.7rem; font-weight:500;
    letter-spacing:0.08em; text-transform:uppercase; color:var(--accent);
    background:var(--accent-light); border:1px solid var(--accent-border);
    padding:0.2rem 0.65rem; border-radius:4px; margin-bottom:1.25rem;
  }
  h1 { font-size:clamp(1.8rem,4vw,3rem); font-weight:700; line-height:1.15; color:var(--text); }
  h2 { font-size:clamp(1.4rem,2.5vw,2rem); font-weight:600; line-height:1.2; color:var(--text); margin-bottom:0.75rem; }
  .sub { color:var(--muted); font-size:1rem; margin-top:0.75rem; max-width:520px; line-height:1.65; }
  .highlight { color:var(--accent); }
  .wordmark { font-size:0.78rem; font-weight:600; letter-spacing:0.12em; text-transform:uppercase; color:var(--muted); margin-bottom:2rem; }
  .title-divider { width:48px; height:3px; background:var(--accent); border-radius:2px; margin:1.25rem 0; }
  .grid { display:grid; gap:1rem; width:100%; margin-top:1.75rem; }
  .grid-3 { grid-template-columns:1fr 1fr 1fr; }
  .card {
    background:var(--bg); border:1.5px solid var(--border);
    border-radius:8px; padding:1.25rem 1.5rem;
    transition:border-color 0.15s, box-shadow 0.15s;
  }
  .card:hover { border-color:var(--accent); box-shadow:0 0 0 3px var(--accent-light); }
  .card .icon { font-size:1.4rem; margin-bottom:0.6rem; }
  .card h3 { font-size:0.9rem; font-weight:600; color:var(--text); margin-bottom:0.35rem; }
  .card p { font-size:0.82rem; color:var(--muted); line-height:1.55; }
  .title-bg { background:linear-gradient(135deg, #faf5ff 0%, #ede9fe 100%); }
  /* Nav */
  .nav {
    position:fixed; bottom:1.75rem; left:50%; transform:translateX(-50%);
    display:flex; align-items:center; gap:0.75rem; z-index:100;
    background:var(--bg); border:1.5px solid var(--border);
    border-radius:8px; padding:0.45rem 0.9rem;
    box-shadow:0 4px 12px rgba(0,0,0,0.08);
  }
  .nav button { background:none; border:none; color:var(--muted); cursor:pointer; font-size:1rem; padding:0.25rem 0.5rem; border-radius:4px; transition:background 0.15s, color 0.15s; }
  .nav button:hover { background:var(--surface2); color:var(--text); }
  .nav button:disabled { color:var(--border); cursor:default; }
  .counter { font-size:0.78rem; color:var(--muted); min-width:2.5rem; text-align:center; }
  .dots { display:flex; gap:0.35rem; align-items:center; }
  .dot { width:5px; height:5px; border-radius:50%; background:var(--border); transition:background 0.2s,transform 0.2s; cursor:pointer; }
  .dot.active { background:var(--accent); transform:scale(1.5); }
</style>
</head>
<body>
<div class="deck" id="deck">

  <!-- SLIDE 1: Title -->
  <div class="slide title-bg active" data-index="0">
    <div class="slide-inner">
      <div class="wordmark">{{WORDMARK}}</div>
      <div class="tag">{{TAG_1}}</div>
      <h1>{{TITLE_LINE_1}}<br><span class="highlight">{{TITLE_LINE_2}}</span></h1>
      <div class="title-divider"></div>
      <p class="sub">{{SUBTITLE}}</p>
    </div>
  </div>

  <!-- SLIDE 2: Section with cards -->
  <div class="slide" data-index="1">
    <div class="slide-inner">
      <div class="tag">{{TAG_2}}</div>
      <h2>{{HEADING_2}} — <span class="highlight">{{HEADING_2_ACCENT}}</span></h2>
      <div class="grid grid-3">
        <div class="card"><div class="icon">🔷</div><h3>Component A</h3><p>Describe the first component or concept clearly and concisely here.</p></div>
        <div class="card"><div class="icon">🔶</div><h3>Component B</h3><p>Describe the second component or concept clearly and concisely here.</p></div>
        <div class="card"><div class="icon">🔵</div><h3>Component C</h3><p>Describe the third component or concept clearly and concisely here.</p></div>
      </div>
    </div>
  </div>

  <!-- SLIDE 3: Closing -->
  <div class="slide title-bg" data-index="2">
    <div class="slide-inner">
      <div class="tag">{{CLOSING_TAG}}</div>
      <h1>{{CLOSING_LINE_1}}<br><span class="highlight">{{CLOSING_LINE_2}}</span></h1>
      <div class="title-divider"></div>
      <p class="sub">{{CLOSING_SUBTITLE}}</p>
    </div>
  </div>

</div>
<div class="nav">
  <button id="prev" onclick="go(-1)" disabled>←</button>
  <div class="dots" id="dots"></div>
  <span class="counter" id="counter"></span>
  <button id="next" onclick="go(1)">→</button>
</div>
<script>
  const slides = document.querySelectorAll('.slide');
  const dotsEl = document.getElementById('dots');
  const counter = document.getElementById('counter');
  let current = 0;
  slides.forEach((_,i) => {
    const d = document.createElement('div');
    d.className = 'dot' + (i===0?' active':'');
    d.onclick = () => goTo(i);
    dotsEl.appendChild(d);
  });
  counter.textContent = '1 / ' + slides.length;
  function goTo(n) {
    slides[current].classList.remove('active');
    slides[current].classList.add('prev');
    setTimeout(() => slides[current].classList.remove('prev'), 350);
    current = n;
    slides[current].classList.add('active');
    document.querySelectorAll('.dot').forEach((d,i) => d.classList.toggle('active',i===current));
    counter.textContent = (current+1) + ' / ' + slides.length;
    document.getElementById('prev').disabled = current===0;
    document.getElementById('next').disabled = current===slides.length-1;
  }
  function go(dir) { const n=current+dir; if(n>=0&&n<slides.length) goTo(n); }
  document.addEventListener('keydown', e => {
    if(e.key==='ArrowRight'||e.key===' ') go(1);
    if(e.key==='ArrowLeft') go(-1);
  });
</script>
</body>
</html>'''

_TEMPLATE_STITCH = '''<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{{TITLE}}</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Google+Sans:wght@400;500;600;700&family=Roboto:wght@300;400;500&display=swap" rel="stylesheet">
<style>
  *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
  :root {
    --bg: #FAFAFA; --surface: #FFFFFF; --surface2: #F1F3F4;
    --border: #E8EAED; --text: #202124; --muted: #5F6368;
    --primary: #1A73E8; --primary-light: rgba(26,115,232,0.1);
    --accent-red: #EA4335; --accent-green: #34A853; --accent-yellow: #FBBC04;
    --shadow-1: 0 1px 3px rgba(0,0,0,0.12), 0 1px 2px rgba(0,0,0,0.08);
    --shadow-2: 0 3px 8px rgba(0,0,0,0.12), 0 1px 3px rgba(0,0,0,0.08);
    --shadow-3: 0 8px 24px rgba(0,0,0,0.12), 0 2px 6px rgba(0,0,0,0.08);
    --font: 'Google Sans', 'Roboto', system-ui, sans-serif;
    --font-body: 'Roboto', system-ui, sans-serif;
    --radius: 16px;
  }
  html, body { width:100%; height:100%; overflow:hidden; background:var(--bg); color:var(--text); font-family:var(--font); }
  .deck { width:100%; height:100%; position:relative; }
  .slide {
    position:absolute; inset:0; display:flex; flex-direction:column;
    justify-content:center; align-items:center; padding:4rem;
    opacity:0; pointer-events:none;
    transition:opacity 0.4s cubic-bezier(0.4,0,0.2,1), transform 0.4s cubic-bezier(0.4,0,0.2,1);
    transform:translateX(32px);
  }
  .slide.active { opacity:1; pointer-events:all; transform:translateX(0); }
  .slide.prev { transform:translateX(-32px); }
  .slide-inner { width:100%; max-width:880px; }
  /* Pill badge */
  .badge {
    display:inline-flex; align-items:center; gap:0.35rem;
    font-family:var(--font-body); font-size:0.72rem; font-weight:500;
    letter-spacing:0.04em; text-transform:uppercase;
    color:var(--primary); background:var(--primary-light);
    padding:0.3rem 0.9rem; border-radius:999px; margin-bottom:1.5rem;
  }
  h1 { font-size:clamp(2rem,4.5vw,3.2rem); font-weight:700; line-height:1.15; color:var(--text); }
  h2 { font-size:clamp(1.5rem,3vw,2.2rem); font-weight:600; line-height:1.2; color:var(--text); margin-bottom:0.75rem; }
  .sub { font-family:var(--font-body); color:var(--muted); font-size:1.05rem; margin-top:1rem; max-width:560px; line-height:1.7; font-weight:300; }
  .highlight { color:var(--primary); }
  .highlight-red { color:var(--accent-red); }
  .highlight-green { color:var(--accent-green); }
  /* Material cards */
  .grid { display:grid; gap:1.25rem; width:100%; margin-top:2rem; }
  .grid-3 { grid-template-columns:1fr 1fr 1fr; }
  .card {
    background:var(--surface); border-radius:var(--radius);
    padding:1.5rem; box-shadow:var(--shadow-1);
    transition:box-shadow 0.2s cubic-bezier(0.4,0,0.2,1), transform 0.2s;
  }
  .card:hover { box-shadow:var(--shadow-3); transform:translateY(-2px); }
  .card .icon { font-size:1.6rem; margin-bottom:0.75rem; }
  .card h3 { font-size:0.95rem; font-weight:600; color:var(--text); margin-bottom:0.4rem; }
  .card p { font-family:var(--font-body); font-size:0.83rem; color:var(--muted); line-height:1.6; font-weight:400; }
  /* Title slide background */
  .title-bg {
    background:linear-gradient(160deg, #e8f0fe 0%, #fce8e6 40%, #e6f4ea 100%);
  }
  .wordmark { font-size:0.8rem; font-weight:600; letter-spacing:0.1em; text-transform:uppercase; color:var(--muted); margin-bottom:2rem; font-family:var(--font-body); }
  /* Colored indicator bar under heading */
  .color-bar { display:flex; gap:6px; margin:1.25rem 0 0; }
  .color-bar span { height:4px; border-radius:2px; flex:1; }
  .bar-blue { background:var(--primary); }
  .bar-red { background:var(--accent-red); }
  .bar-yellow { background:var(--accent-yellow); }
  .bar-green { background:var(--accent-green); }
  /* Nav */
  .nav {
    position:fixed; bottom:1.75rem; left:50%; transform:translateX(-50%);
    display:flex; align-items:center; gap:0.75rem; z-index:100;
    background:var(--surface); border-radius:999px; padding:0.5rem 1.1rem;
    box-shadow:var(--shadow-2);
  }
  .nav button {
    background:none; border:none; color:var(--muted); cursor:pointer;
    font-size:1.05rem; padding:0.3rem 0.6rem; border-radius:999px;
    transition:background 0.15s, color 0.15s;
  }
  .nav button:hover { background:var(--primary-light); color:var(--primary); }
  .nav button:disabled { color:var(--border); cursor:default; }
  .counter { font-family:var(--font-body); font-size:0.78rem; color:var(--muted); min-width:2.5rem; text-align:center; }
  .dots { display:flex; gap:0.4rem; align-items:center; }
  .dot { width:6px; height:6px; border-radius:50%; background:var(--border); transition:background 0.2s,transform 0.2s; cursor:pointer; }
  .dot.active { background:var(--primary); transform:scale(1.4); }
</style>
</head>
<body>
<div class="deck" id="deck">

  <!-- SLIDE 1: Title -->
  <div class="slide title-bg active" data-index="0">
    <div class="slide-inner">
      <div class="wordmark">{{WORDMARK}}</div>
      <div class="badge">{{TAG_1}}</div>
      <h1>{{TITLE_LINE_1}}<br><span class="highlight">{{TITLE_LINE_2}}</span></h1>
      <div class="color-bar">
        <span class="bar-blue"></span>
        <span class="bar-red"></span>
        <span class="bar-yellow"></span>
        <span class="bar-green"></span>
      </div>
      <p class="sub">{{SUBTITLE}}</p>
    </div>
  </div>

  <!-- SLIDE 2: Section with Material cards -->
  <div class="slide" data-index="1">
    <div class="slide-inner">
      <div class="badge">{{TAG_2}}</div>
      <h2>{{HEADING_2}} — <span class="highlight">{{HEADING_2_ACCENT}}</span></h2>
      <div class="grid grid-3">
        <div class="card"><div class="icon">🔵</div><h3>Feature One</h3><p>A clear, concise description of this feature or concept.</p></div>
        <div class="card"><div class="icon">🔴</div><h3>Feature Two</h3><p>A clear, concise description of this feature or concept.</p></div>
        <div class="card"><div class="icon">🟢</div><h3>Feature Three</h3><p>A clear, concise description of this feature or concept.</p></div>
      </div>
    </div>
  </div>

  <!-- SLIDE 3: Closing -->
  <div class="slide title-bg" data-index="2">
    <div class="slide-inner">
      <div class="badge">{{CLOSING_TAG}}</div>
      <h1>{{CLOSING_LINE_1}}<br><span class="highlight-green">{{CLOSING_LINE_2}}</span></h1>
      <div class="color-bar">
        <span class="bar-blue"></span>
        <span class="bar-red"></span>
        <span class="bar-yellow"></span>
        <span class="bar-green"></span>
      </div>
      <p class="sub">{{CLOSING_SUBTITLE}}</p>
    </div>
  </div>

</div>
<div class="nav">
  <button id="prev" onclick="go(-1)" disabled>←</button>
  <div class="dots" id="dots"></div>
  <span class="counter" id="counter"></span>
  <button id="next" onclick="go(1)">→</button>
</div>
<script>
  const slides = document.querySelectorAll('.slide');
  const dotsEl = document.getElementById('dots');
  const counter = document.getElementById('counter');
  let current = 0;
  slides.forEach((_,i) => {
    const d = document.createElement('div');
    d.className = 'dot' + (i===0?' active':'');
    d.onclick = () => goTo(i);
    dotsEl.appendChild(d);
  });
  counter.textContent = '1 / ' + slides.length;
  function goTo(n) {
    slides[current].classList.remove('active');
    slides[current].classList.add('prev');
    setTimeout(() => slides[current].classList.remove('prev'), 400);
    current = n;
    slides[current].classList.add('active');
    document.querySelectorAll('.dot').forEach((d,i) => d.classList.toggle('active',i===current));
    counter.textContent = (current+1) + ' / ' + slides.length;
    document.getElementById('prev').disabled = current===0;
    document.getElementById('next').disabled = current===slides.length-1;
  }
  function go(dir) { const n=current+dir; if(n>=0&&n<slides.length) goTo(n); }
  document.addEventListener('keydown', e => {
    if(e.key==='ArrowRight'||e.key===' ') go(1);
    if(e.key==='ArrowLeft') go(-1);
  });
</script>
</body>
</html>'''

PresentationStore._BUILTIN_TEMPLATES = [
    {
        "name": "Default Dark",
        "description": "Pinky brand style. Purple accents, Space Grotesk/Mono fonts, dark background, slide transitions.",
        "tags": ["dark", "brand", "animated"],
        "thumbnail_css": (
            "background:#0d0d0f; border-top:3px solid #7c6af7; "
            "font-family:'Space Grotesk',sans-serif; color:#e8e8f0;"
        ),
        "html_content": _TEMPLATE_DEFAULT_DARK,
    },
    {
        "name": "Minimal",
        "description": "Scrollable sections, no JS transitions, print-safe. Same dark colors but simpler.",
        "tags": ["dark", "minimal", "print"],
        "thumbnail_css": (
            "background:#0d0d0f; border-top:3px solid #666680; "
            "font-family:'Space Grotesk',sans-serif; color:#e8e8f0;"
        ),
        "html_content": _TEMPLATE_MINIMAL,
    },
    {
        "name": "Figma Style",
        "description": "Light background, Inter font, Figma design-tool aesthetic with clean borders and tight cards.",
        "tags": ["light", "figma", "design"],
        "thumbnail_css": (
            "background:#FFFFFF; border-top:3px solid #7C3AED; "
            "font-family:'Inter',sans-serif; color:#111827;"
        ),
        "html_content": _TEMPLATE_FIGMA,
    },
    {
        "name": "Google Stitch",
        "description": "Material You aesthetic. Google Blue primary, rounded corners, elevation shadows, pill badges.",
        "tags": ["light", "material", "google"],
        "thumbnail_css": (
            "background:#FAFAFA; border-top:3px solid #1A73E8; "
            "font-family:'Google Sans','Roboto',sans-serif; color:#202124;"
        ),
        "html_content": _TEMPLATE_STITCH,
    },
]
