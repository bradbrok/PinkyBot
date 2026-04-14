"""App Store — SQLite-backed mini web application management.

Agents can build, deploy, and manage self-contained HTML apps (single-page tools,
dashboards, games, etc.). Each app has a share token for public access at /a/{share_token}.

Schema:
    apps(id, slug, name, description, app_type, status, created_by, tags,
         html_content, share_token, created_at, updated_at)
"""

from __future__ import annotations

import re
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


VALID_STATUSES = ("draft", "deployed", "stopped", "error")
VALID_APP_TYPES = ("tool", "dashboard", "game", "page", "other")


def _slugify(text: str) -> str:
    """Convert name to a URL-safe slug."""
    text = text.lower().strip()
    text = re.sub(r"[^\w\s-]", "", text)
    text = re.sub(r"[\s_-]+", "-", text)
    return text.strip("-")[:80] or "app"


@dataclass
class App:
    id: int
    slug: str
    name: str
    description: str
    app_type: str
    status: str
    created_by: str
    tags: list[str]
    html_content: str
    share_token: str
    created_at: float
    updated_at: float
    access_password: str = ""  # never exposed in to_dict()

    def to_dict(self, *, include_html: bool = False) -> dict:
        d = {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "app_type": self.app_type,
            "status": self.status,
            "created_by": self.created_by,
            "tags": self.tags,
            "share_token": self.share_token,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "protected": bool(self.access_password),
        }
        if include_html:
            d["html_content"] = self.html_content
        return d


class AppStore:
    """SQLite-backed app storage."""

    def __init__(self, db_path: str = "data/apps.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS apps (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                slug         TEXT    NOT NULL UNIQUE,
                name         TEXT    NOT NULL,
                description  TEXT    NOT NULL DEFAULT '',
                app_type     TEXT    NOT NULL DEFAULT 'other',
                status       TEXT    NOT NULL DEFAULT 'draft',
                created_by   TEXT    NOT NULL DEFAULT '',
                tags         TEXT    NOT NULL DEFAULT '[]',
                html_content TEXT    NOT NULL DEFAULT '',
                share_token  TEXT    NOT NULL UNIQUE,
                created_at   REAL   NOT NULL,
                updated_at   REAL   NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_apps_slug   ON apps(slug);
            CREATE INDEX IF NOT EXISTS idx_apps_share  ON apps(share_token);
            CREATE INDEX IF NOT EXISTS idx_apps_status ON apps(status);
            CREATE INDEX IF NOT EXISTS idx_apps_agent  ON apps(created_by);
        """)
        self._db.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add new columns to existing databases."""
        existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(apps)").fetchall()
        }
        migrations = [
            ("access_password", "TEXT NOT NULL DEFAULT ''"),
        ]
        for col, typedef in migrations:
            if col not in existing:
                self._db.execute(f"ALTER TABLE apps ADD COLUMN {col} {typedef}")
                _log(f"[app_store] migrated — added {col} to apps")
        self._db.commit()

    _COLS = (
        "id, slug, name, description, app_type, status, created_by, "
        "tags, html_content, share_token, created_at, updated_at, access_password"
    )

    def _row_to_app(self, row: tuple) -> App:
        import json

        return App(
            id=row[0],
            slug=row[1],
            name=row[2],
            description=row[3],
            app_type=row[4],
            status=row[5],
            created_by=row[6],
            tags=json.loads(row[7] or "[]"),
            html_content=row[8],
            share_token=row[9],
            created_at=row[10],
            updated_at=row[11],
            access_password=row[12] if len(row) > 12 else "",
        )

    def _unique_slug(self, base_slug: str) -> str:
        slug = base_slug
        suffix = 1
        while self._db.execute(
            "SELECT 1 FROM apps WHERE slug=?", (slug,)
        ).fetchone():
            slug = f"{base_slug}-{suffix}"
            suffix += 1
        return slug

    # ── CRUD ─────────────────────────────────────────────────

    def create(
        self,
        name: str,
        *,
        description: str = "",
        app_type: str = "other",
        created_by: str = "",
        tags: list[str] | None = None,
        html_content: str = "",
    ) -> App:
        import json

        now = time.time()
        slug = self._unique_slug(_slugify(name))
        share_token = secrets.token_urlsafe(12)
        if app_type not in VALID_APP_TYPES:
            app_type = "other"
        tags_json = json.dumps(tags or [])

        self._db.execute(
            f"""INSERT INTO apps ({self._COLS})
                VALUES (NULL, ?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, '')""",
            (slug, name, description, app_type, created_by,
             tags_json, html_content, share_token, now, now),
        )
        self._db.commit()
        app_id = self._db.execute("SELECT last_insert_rowid()").fetchone()[0]
        _log(f"[app_store] created app #{app_id} '{name}' ({app_type})")
        return self.get(app_id)  # type: ignore[return-value]

    def get(self, app_id: int) -> App | None:
        row = self._db.execute(
            f"SELECT {self._COLS} FROM apps WHERE id=?", (app_id,)
        ).fetchone()
        return self._row_to_app(row) if row else None

    def get_by_share_token(self, token: str) -> App | None:
        row = self._db.execute(
            f"SELECT {self._COLS} FROM apps WHERE share_token=?", (token,)
        ).fetchone()
        return self._row_to_app(row) if row else None

    def list(
        self,
        *,
        status: str = "",
        created_by: str = "",
        tag: str = "",
        limit: int = 100,
        offset: int = 0,
    ) -> list[App]:
        conditions = []
        params: list = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if created_by:
            conditions.append("created_by=?")
            params.append(created_by)
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        rows = self._db.execute(
            f"SELECT {self._COLS} FROM apps {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ).fetchall()
        apps = [self._row_to_app(r) for r in rows]
        if tag:
            apps = [a for a in apps if tag in a.tags]
        return apps

    def update(
        self,
        app_id: int,
        *,
        name: str | None = None,
        description: str | None = None,
        app_type: str | None = None,
        tags: list[str] | None = None,
        status: str | None = None,
    ) -> App | None:
        import json

        app = self.get(app_id)
        if not app:
            return None
        now = time.time()
        updates = ["updated_at=?"]
        params: list = [now]
        if name is not None:
            updates.append("name=?")
            params.append(name)
        if description is not None:
            updates.append("description=?")
            params.append(description)
        if app_type is not None and app_type in VALID_APP_TYPES:
            updates.append("app_type=?")
            params.append(app_type)
        if tags is not None:
            updates.append("tags=?")
            params.append(json.dumps(tags))
        if status is not None and status in VALID_STATUSES:
            updates.append("status=?")
            params.append(status)
        params.append(app_id)
        self._db.execute(
            f"UPDATE apps SET {', '.join(updates)} WHERE id=?", params
        )
        self._db.commit()
        return self.get(app_id)

    def deploy(self, app_id: int, html_content: str) -> App | None:
        """Deploy or update app content and set status to deployed."""
        app = self.get(app_id)
        if not app:
            return None
        now = time.time()
        self._db.execute(
            "UPDATE apps SET html_content=?, status='deployed', updated_at=? WHERE id=?",
            (html_content, now, app_id),
        )
        self._db.commit()
        _log(f"[app_store] deployed app #{app_id}")
        return self.get(app_id)

    def regenerate_share_token(self, app_id: int) -> App | None:
        """Generate a new share token for the app."""
        app = self.get(app_id)
        if not app:
            return None
        new_token = secrets.token_urlsafe(12)
        now = time.time()
        self._db.execute(
            "UPDATE apps SET share_token=?, updated_at=? WHERE id=?",
            (new_token, now, app_id),
        )
        self._db.commit()
        _log(f"[app_store] regenerated share token for app #{app_id}")
        return self.get(app_id)

    def delete(self, app_id: int) -> bool:
        app = self.get(app_id)
        if not app:
            return False
        self._db.execute("DELETE FROM apps WHERE id=?", (app_id,))
        self._db.commit()
        _log(f"[app_store] deleted app #{app_id} '{app.name}'")
        return True

    def get_stats(self) -> dict:
        """Return aggregate stats about apps."""
        total = self._db.execute("SELECT COUNT(*) FROM apps").fetchone()[0]
        by_status = {}
        for row in self._db.execute(
            "SELECT status, COUNT(*) FROM apps GROUP BY status"
        ).fetchall():
            by_status[row[0]] = row[1]
        by_type = {}
        for row in self._db.execute(
            "SELECT app_type, COUNT(*) FROM apps GROUP BY app_type"
        ).fetchall():
            by_type[row[0]] = row[1]
        return {
            "total": total,
            "by_status": by_status,
            "by_type": by_type,
        }

    def check_health(self, app_id: int) -> dict:
        """Basic health/accessibility check for an app."""
        app = self.get(app_id)
        if not app:
            return {"ok": False, "error": "App not found"}
        has_content = bool(app.html_content.strip())
        has_files = self.get_static_dir(app_id).exists()
        return {
            "ok": app.status == "deployed" and (has_content or has_files),
            "app_id": app.id,
            "status": app.status,
            "has_content": has_content,
            "has_static_files": has_files,
            "share_token": app.share_token,
            "content_length": len(app.html_content),
        }

    # ── Password protection ──────────────────────────────────

    def set_password(self, app_id: int, password: str) -> bool:
        """Set or remove password protection. Empty string = remove."""
        import hashlib

        app = self.get(app_id)
        if not app:
            return False
        hashed = (
            hashlib.sha256(password.encode()).hexdigest() if password else ""
        )
        self._db.execute(
            "UPDATE apps SET access_password=?, updated_at=? WHERE id=?",
            (hashed, time.time(), app_id),
        )
        self._db.commit()
        return True

    def check_password(self, app_id: int, password: str) -> bool:
        """Verify a password against the stored hash."""
        import hashlib

        app = self.get(app_id)
        if not app or not app.access_password:
            return True  # no password = always pass
        return (
            hashlib.sha256(password.encode()).hexdigest()
            == app.access_password
        )

    # ── Static file management ───────────────────────────────

    def get_static_dir(self, app_id: int) -> Path:
        """Return the filesystem path for an app's static files."""
        base = Path(self._db_path).parent / "apps" / str(app_id)
        return base

    def ensure_static_dir(self, app_id: int) -> Path:
        """Create and return the static file directory for an app."""
        d = self.get_static_dir(app_id)
        d.mkdir(parents=True, exist_ok=True)
        return d
