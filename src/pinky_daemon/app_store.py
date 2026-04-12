"""App Store — SQLite-backed HTML app storage with deploy/share support.

Agents create fully self-contained HTML apps (games, tools, visualizations),
stored here with a public share token. The public viewer at /a/{share_token}
requires no authentication (optionally password-protected).

Schema:
    apps(id, name, description, app_type, created_by, tags, status,
         share_token, html_content, access_password, created_at, updated_at)
"""

from __future__ import annotations

import json
import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class App:
    id: int
    name: str
    description: str
    app_type: str
    created_by: str
    tags: list[str]
    status: str  # "draft" | "deployed"
    share_token: str
    html_content: str
    access_password: str  # never exposed in to_dict()
    created_at: float
    updated_at: float

    def to_dict(self, *, include_html: bool = False) -> dict:
        d = {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "app_type": self.app_type,
            "created_by": self.created_by,
            "tags": self.tags,
            "status": self.status,
            "share_token": self.share_token,
            "protected": bool(self.access_password),
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        if include_html:
            d["html_content"] = self.html_content
        return d


class AppStore:
    """SQLite-backed app storage with deploy/share/password support."""

    def __init__(self, db_path: str = "data/apps.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._data_dir = Path(db_path).parent
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS apps (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                name            TEXT    NOT NULL,
                description     TEXT    NOT NULL DEFAULT '',
                app_type        TEXT    NOT NULL DEFAULT 'other',
                created_by      TEXT    NOT NULL DEFAULT '',
                tags            TEXT    NOT NULL DEFAULT '[]',
                status          TEXT    NOT NULL DEFAULT 'draft',
                share_token     TEXT    NOT NULL UNIQUE,
                html_content    TEXT    NOT NULL DEFAULT '',
                access_password TEXT    NOT NULL DEFAULT '',
                created_at      REAL    NOT NULL,
                updated_at      REAL    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_apps_status     ON apps(status);
            CREATE INDEX IF NOT EXISTS idx_apps_created_by ON apps(created_by);
            CREATE INDEX IF NOT EXISTS idx_apps_share      ON apps(share_token);
        """)
        self._db.commit()

    # ── Column helpers ────────────────────────────────────────

    _COLS = (
        "id, name, description, app_type, created_by, tags, status, "
        "share_token, html_content, access_password, created_at, updated_at"
    )

    def _row_to_app(self, row: tuple) -> App:
        return App(
            id=row[0],
            name=row[1],
            description=row[2],
            app_type=row[3],
            created_by=row[4],
            tags=json.loads(row[5] or "[]"),
            status=row[6],
            share_token=row[7],
            html_content=row[8],
            access_password=row[9] or "",
            created_at=row[10],
            updated_at=row[11],
        )

    # ── Write ─────────────────────────────────────────────────

    def create(
        self,
        *,
        name: str,
        description: str = "",
        app_type: str = "other",
        created_by: str = "",
        tags: list[str] | None = None,
        html_content: str = "",
    ) -> App:
        now = time.time()
        share_token = secrets.token_urlsafe(16)
        status = "deployed" if html_content.strip() else "draft"
        cursor = self._db.execute(
            """INSERT INTO apps
               (name, description, app_type, created_by, tags, status,
                share_token, html_content, access_password, created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, '', ?, ?)""",
            (
                name,
                description,
                app_type,
                created_by,
                json.dumps(tags or []),
                status,
                share_token,
                html_content,
                now,
                now,
            ),
        )
        self._db.commit()
        return self.get(cursor.lastrowid)  # type: ignore[return-value]

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
        existing = self.get(app_id)
        if not existing:
            return None
        new_name = name if name is not None else existing.name
        new_desc = description if description is not None else existing.description
        new_type = app_type if app_type is not None else existing.app_type
        new_tags = tags if tags is not None else existing.tags
        new_status = status if status is not None else existing.status
        self._db.execute(
            """UPDATE apps SET name=?, description=?, app_type=?, tags=?,
               status=?, updated_at=? WHERE id=?""",
            (new_name, new_desc, new_type, json.dumps(new_tags), new_status, time.time(), app_id),
        )
        self._db.commit()
        return self.get(app_id)

    def deploy(self, app_id: int, html_content: str) -> App | None:
        existing = self.get(app_id)
        if not existing:
            return None
        self._db.execute(
            "UPDATE apps SET html_content=?, status='deployed', updated_at=? WHERE id=?",
            (html_content, time.time(), app_id),
        )
        self._db.commit()
        return self.get(app_id)

    def delete(self, app_id: int) -> bool:
        cursor = self._db.execute("DELETE FROM apps WHERE id=?", (app_id,))
        self._db.commit()
        # Clean up static files
        static = self._static_path(app_id)
        if static.exists():
            import shutil
            shutil.rmtree(static, ignore_errors=True)
        return cursor.rowcount > 0

    def regenerate_share_token(self, app_id: int) -> App | None:
        existing = self.get(app_id)
        if not existing:
            return None
        new_token = secrets.token_urlsafe(16)
        self._db.execute(
            "UPDATE apps SET share_token=?, updated_at=? WHERE id=?",
            (new_token, time.time(), app_id),
        )
        self._db.commit()
        return self.get(app_id)

    # ── Password protection ───────────────────────────────────

    def set_password(self, app_id: int, password: str) -> bool:
        """Set or remove password protection. Pass empty string to remove."""
        cursor = self._db.execute(
            "UPDATE apps SET access_password=?, updated_at=? WHERE id=?",
            (password, time.time(), app_id),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def check_password(self, app_id: int, password: str) -> bool:
        """Return True if the supplied password matches the stored one."""
        import hmac as _hmac

        row = self._db.execute(
            "SELECT access_password FROM apps WHERE id=?", (app_id,)
        ).fetchone()
        if not row:
            return False
        stored = row[0] or ""
        if not stored:
            return True  # no password set — open access
        return _hmac.compare_digest(stored, password)

    # ── Read ──────────────────────────────────────────────────

    def get(self, app_id: int) -> App | None:
        row = self._db.execute(
            f"SELECT {self._COLS} FROM apps WHERE id=?", (app_id,)
        ).fetchone()
        return self._row_to_app(row) if row else None

    def get_by_share_token(self, share_token: str) -> App | None:
        row = self._db.execute(
            f"SELECT {self._COLS} FROM apps WHERE share_token=?", (share_token,)
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
        conditions: list[str] = []
        params: list = []
        if status:
            conditions.append("status=?")
            params.append(status)
        if created_by:
            conditions.append("created_by=?")
            params.append(created_by)
        if tag:
            conditions.append("tags LIKE ?")
            params.append(f'%"{tag}"%')
        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])
        rows = self._db.execute(
            f"SELECT {self._COLS} FROM apps {where} ORDER BY updated_at DESC LIMIT ? OFFSET ?",
            params,
        ).fetchall()
        return [self._row_to_app(r) for r in rows]

    def get_stats(self) -> dict:
        row = self._db.execute(
            "SELECT COUNT(*), SUM(status='deployed'), SUM(status='draft') FROM apps"
        ).fetchone()
        return {
            "total": row[0] or 0,
            "deployed": row[1] or 0,
            "draft": row[2] or 0,
        }

    def check_health(self, app_id: int) -> dict:
        found = self.get(app_id)
        if not found:
            return {"ok": False, "error": "App not found"}
        has_content = bool(found.html_content.strip()) or self.get_static_dir(app_id).is_dir()
        return {
            "ok": True,
            "status": found.status,
            "has_content": has_content,
            "protected": bool(found.access_password),
        }

    # ── Static file helpers ───────────────────────────────────

    def _static_path(self, app_id: int) -> Path:
        return self._data_dir / "apps" / str(app_id)

    def ensure_static_dir(self, app_id: int) -> Path:
        path = self._static_path(app_id)
        path.mkdir(parents=True, exist_ok=True)
        return path

    def get_static_dir(self, app_id: int) -> Path:
        return self._static_path(app_id)
