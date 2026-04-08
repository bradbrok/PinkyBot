"""Trigger Store — SQLite-backed store for agent triggers.

Supports three trigger types:
  - webhook: external service POSTs to a secret URL, wakes the agent
  - url: daemon polls a URL on an interval, fires on condition match
  - file: daemon watches a file/glob on disk, fires when it changes

All triggers share the same table. Type-specific fields are nullable
and only populated when relevant.
"""

from __future__ import annotations

import secrets
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class Trigger:
    """A single trigger record."""

    id: int = 0
    agent_name: str = ""
    name: str = ""
    trigger_type: str = ""  # 'webhook' | 'url' | 'file'
    token: str = ""  # webhook only; never exposed in list responses
    url: str = ""  # url type only
    method: str = "GET"  # url type only
    condition: str = ""  # condition type string
    condition_value: str = ""  # JSON blob with condition params
    file_path: str = ""  # file type only
    interval_seconds: int = 300
    prompt_template: str = ""
    enabled: bool = True
    last_fired_at: float = 0.0
    last_checked_at: float = 0.0
    last_value: str = ""  # last response body/hash/status for change detection
    fire_count: int = 0
    created_at: float = 0.0

    def to_dict(self, include_token: bool = False) -> dict:
        d = {
            "id": self.id,
            "agent_name": self.agent_name,
            "name": self.name,
            "trigger_type": self.trigger_type,
            "url": self.url,
            "method": self.method,
            "condition": self.condition,
            "condition_value": self.condition_value,
            "file_path": self.file_path,
            "interval_seconds": self.interval_seconds,
            "prompt_template": self.prompt_template,
            "enabled": self.enabled,
            "last_fired_at": self.last_fired_at,
            "last_checked_at": self.last_checked_at,
            "fire_count": self.fire_count,
            "created_at": self.created_at,
        }
        if include_token and self.token:
            d["token"] = self.token
        return d


class TriggerStore:
    """SQLite-backed store for triggers."""

    def __init__(self, db_path: str = "data/triggers.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._db.row_factory = sqlite3.Row
        self._init_table()
        self._migrate()

    def _init_table(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS triggers (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                agent_name        TEXT    NOT NULL,
                name              TEXT    NOT NULL DEFAULT '',
                trigger_type      TEXT    NOT NULL,
                token             TEXT    UNIQUE,
                url               TEXT    NOT NULL DEFAULT '',
                method            TEXT    NOT NULL DEFAULT 'GET',
                condition         TEXT    NOT NULL DEFAULT '',
                condition_value   TEXT    NOT NULL DEFAULT '',
                file_path         TEXT    NOT NULL DEFAULT '',
                interval_seconds  INTEGER NOT NULL DEFAULT 300,
                prompt_template   TEXT    NOT NULL DEFAULT '',
                enabled           INTEGER NOT NULL DEFAULT 1,
                last_fired_at     REAL    NOT NULL DEFAULT 0,
                last_checked_at   REAL    NOT NULL DEFAULT 0,
                last_value        TEXT    NOT NULL DEFAULT '',
                fire_count        INTEGER NOT NULL DEFAULT 0,
                created_at        REAL    NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_triggers_agent
                ON triggers(agent_name);

            CREATE INDEX IF NOT EXISTS idx_triggers_token
                ON triggers(token) WHERE token IS NOT NULL;

            CREATE INDEX IF NOT EXISTS idx_triggers_type_enabled
                ON triggers(trigger_type, enabled);
        """)
        self._db.commit()

    def _migrate(self) -> None:
        """Add any missing columns following the _ensure_columns pattern."""
        existing = {
            row[1] for row in self._db.execute("PRAGMA table_info(triggers)").fetchall()
        }
        migrations: list[tuple[str, str]] = [
            # Future additions go here
        ]
        for col, typedef in migrations:
            if col not in existing:
                self._db.execute(f"ALTER TABLE triggers ADD COLUMN {col} {typedef}")
                _log(f"trigger_store: migrated — added {col} to triggers")
        if migrations:
            self._db.commit()

    # ── Row helper ─────────────────────────────────────────────

    def _row_to_trigger(self, row: sqlite3.Row) -> Trigger:
        return Trigger(
            id=row["id"],
            agent_name=row["agent_name"],
            name=row["name"],
            trigger_type=row["trigger_type"],
            token=row["token"] or "",
            url=row["url"],
            method=row["method"],
            condition=row["condition"],
            condition_value=row["condition_value"],
            file_path=row["file_path"],
            interval_seconds=row["interval_seconds"],
            prompt_template=row["prompt_template"],
            enabled=bool(row["enabled"]),
            last_fired_at=row["last_fired_at"],
            last_checked_at=row["last_checked_at"],
            last_value=row["last_value"],
            fire_count=row["fire_count"],
            created_at=row["created_at"],
        )

    # ── CRUD ───────────────────────────────────────────────────

    def create(
        self,
        agent_name: str,
        name: str,
        trigger_type: str,
        *,
        url: str = "",
        method: str = "GET",
        condition: str = "",
        condition_value: str = "",
        file_path: str = "",
        interval_seconds: int = 300,
        prompt_template: str = "",
        enabled: bool = True,
    ) -> Trigger:
        """Create a new trigger. For webhook type, a token is auto-generated."""
        token: str | None = None
        if trigger_type == "webhook":
            token = secrets.token_urlsafe(32)

        now = time.time()
        cur = self._db.execute(
            """
            INSERT INTO triggers (
                agent_name, name, trigger_type, token, url, method,
                condition, condition_value, file_path, interval_seconds,
                prompt_template, enabled, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                agent_name, name, trigger_type, token, url, method,
                condition, condition_value, file_path, interval_seconds,
                prompt_template, int(enabled), now,
            ),
        )
        self._db.commit()
        row = self._db.execute(
            "SELECT * FROM triggers WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return self._row_to_trigger(row)

    def get(self, trigger_id: int) -> Trigger | None:
        row = self._db.execute(
            "SELECT * FROM triggers WHERE id = ?", (trigger_id,)
        ).fetchone()
        return self._row_to_trigger(row) if row else None

    def get_by_token(self, token: str) -> Trigger | None:
        row = self._db.execute(
            "SELECT * FROM triggers WHERE token = ? AND enabled = 1", (token,)
        ).fetchone()
        return self._row_to_trigger(row) if row else None

    def list(
        self,
        agent_name: str | None = None,
        enabled_only: bool = False,
    ) -> list[Trigger]:
        query = "SELECT * FROM triggers"
        params: list = []
        conditions: list[str] = []
        if agent_name:
            conditions.append("agent_name = ?")
            params.append(agent_name)
        if enabled_only:
            conditions.append("enabled = 1")
        if conditions:
            query += " WHERE " + " AND ".join(conditions)
        query += " ORDER BY created_at DESC"
        rows = self._db.execute(query, params).fetchall()
        return [self._row_to_trigger(r) for r in rows]

    def update(self, trigger_id: int, **kwargs) -> Trigger | None:
        """Update arbitrary fields on a trigger. Returns updated trigger or None."""
        allowed = {
            "name", "url", "method", "condition", "condition_value",
            "file_path", "interval_seconds", "prompt_template", "enabled",
        }
        updates = {k: v for k, v in kwargs.items() if k in allowed}
        if not updates:
            return self.get(trigger_id)

        # Coerce enabled to int for SQLite
        if "enabled" in updates:
            updates["enabled"] = int(updates["enabled"])

        set_clause = ", ".join(f"{k} = ?" for k in updates)
        values = list(updates.values()) + [trigger_id]
        self._db.execute(
            f"UPDATE triggers SET {set_clause} WHERE id = ?", values
        )
        self._db.commit()
        return self.get(trigger_id)

    def delete(self, trigger_id: int) -> bool:
        cur = self._db.execute("DELETE FROM triggers WHERE id = ?", (trigger_id,))
        self._db.commit()
        return cur.rowcount > 0

    def rotate_token(self, trigger_id: int) -> str | None:
        """Generate a new secret token for a webhook trigger. Returns new token or None."""
        trigger = self.get(trigger_id)
        if not trigger or trigger.trigger_type != "webhook":
            return None
        new_token = secrets.token_urlsafe(32)
        self._db.execute(
            "UPDATE triggers SET token = ? WHERE id = ?", (new_token, trigger_id)
        )
        self._db.commit()
        return new_token

    # ── State updates ──────────────────────────────────────────

    def record_fire(self, trigger_id: int) -> None:
        """Increment fire_count and set last_fired_at to now."""
        now = time.time()
        self._db.execute(
            "UPDATE triggers SET fire_count = fire_count + 1, last_fired_at = ? WHERE id = ?",
            (now, trigger_id),
        )
        self._db.commit()

    def record_check(self, trigger_id: int, last_value: str) -> None:
        """Update last_checked_at and last_value for change-detection triggers."""
        now = time.time()
        self._db.execute(
            "UPDATE triggers SET last_checked_at = ?, last_value = ? WHERE id = ?",
            (now, last_value, trigger_id),
        )
        self._db.commit()

    # ── URL watcher helpers ─────────────────────────────────────

    def list_due_url_watchers(self, now: float) -> list[Trigger]:
        """Return enabled url triggers whose next check time has arrived."""
        rows = self._db.execute(
            """
            SELECT * FROM triggers
            WHERE trigger_type = 'url'
              AND enabled = 1
              AND last_checked_at + interval_seconds <= ?
            ORDER BY last_checked_at ASC
            """,
            (now,),
        ).fetchall()
        return [self._row_to_trigger(r) for r in rows]
