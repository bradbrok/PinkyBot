"""Hub Store — SQLite-backed registry of Pinky daemon instances and public presentations.

This is the data layer for the pinkybot.ai hub service. It tracks registered
agent instances (user daemons) and aggregates their public presentations.

Schema:
    instances(id, label, url, api_key, owner_email, owner_name,
              is_active, registered_at, last_seen_at)
    public_presentations(id, instance_id, remote_id, title, description,
                         created_by, share_token, tags, version, template,
                         thumbnail_url, synced_at)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class Instance:
    id: int
    label: str
    url: str
    api_key: str  # stored plaintext for MVP; encrypt in production
    owner_email: str
    owner_name: str
    is_active: bool
    registered_at: float
    last_seen_at: float

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "label": self.label,
            "url": self.url,
            "owner_email": self.owner_email,
            "owner_name": self.owner_name,
            "is_active": self.is_active,
            "registered_at": self.registered_at,
            "last_seen_at": self.last_seen_at,
            # api_key intentionally omitted from serialized output
        }


@dataclass
class PublicPresentation:
    id: int
    instance_id: int
    remote_id: int
    title: str
    description: str
    created_by: str
    share_token: str
    tags: list[str]
    version: int
    template: str
    thumbnail_url: str
    synced_at: float
    # denormalized — populated when fetched alongside instances
    instance_label: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "instance_id": self.instance_id,
            "remote_id": self.remote_id,
            "title": self.title,
            "description": self.description,
            "created_by": self.created_by,
            "share_token": self.share_token,
            "tags": self.tags,
            "version": self.version,
            "template": self.template,
            "thumbnail_url": self.thumbnail_url,
            "synced_at": self.synced_at,
            "instance_label": self.instance_label,
        }


class HubStore:
    """SQLite-backed hub storage for instances and aggregated presentations."""

    _I_COLS = (
        "id, label, url, api_key, owner_email, owner_name, is_active, registered_at, last_seen_at"
    )
    _P_COLS = (
        "id, instance_id, remote_id, title, description, created_by,"
        " share_token, tags, version, template, thumbnail_url, synced_at"
    )

    def __init__(self, db_path: str = "data/hub.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.execute("PRAGMA foreign_keys=ON")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS instances (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                label         TEXT    NOT NULL,
                url           TEXT    NOT NULL,
                api_key       TEXT    NOT NULL,
                owner_email   TEXT    NOT NULL DEFAULT '',
                owner_name    TEXT    NOT NULL DEFAULT '',
                is_active     INTEGER NOT NULL DEFAULT 1,
                registered_at REAL    NOT NULL,
                last_seen_at  REAL    NOT NULL
            );

            CREATE TABLE IF NOT EXISTS public_presentations (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                instance_id   INTEGER NOT NULL REFERENCES instances(id) ON DELETE CASCADE,
                remote_id     INTEGER NOT NULL,
                title         TEXT    NOT NULL,
                description   TEXT    NOT NULL DEFAULT '',
                created_by    TEXT    NOT NULL DEFAULT '',
                share_token   TEXT    NOT NULL,
                tags          TEXT    NOT NULL DEFAULT '[]',
                version       INTEGER NOT NULL DEFAULT 1,
                template      TEXT    NOT NULL DEFAULT '',
                thumbnail_url TEXT    NOT NULL DEFAULT '',
                synced_at     REAL    NOT NULL,
                UNIQUE(instance_id, remote_id)
            );

            CREATE INDEX IF NOT EXISTS idx_i_url        ON instances(url);
            CREATE INDEX IF NOT EXISTS idx_i_active     ON instances(is_active);
            CREATE INDEX IF NOT EXISTS idx_pp_instance  ON public_presentations(instance_id);
            CREATE INDEX IF NOT EXISTS idx_pp_synced    ON public_presentations(synced_at);
        """)
        self._db.commit()
        self._migrate()

    def _migrate(self) -> None:
        """Add columns that were introduced after the initial schema."""
        existing = {
            row[1]
            for row in self._db.execute(
                "PRAGMA table_info(public_presentations)"
            ).fetchall()
        }
        if "template" not in existing:
            self._db.execute(
                "ALTER TABLE public_presentations ADD COLUMN template TEXT NOT NULL DEFAULT ''"
            )
        if "thumbnail_url" not in existing:
            self._db.execute(
                "ALTER TABLE public_presentations ADD COLUMN thumbnail_url TEXT NOT NULL DEFAULT ''"
            )
        self._db.commit()

    # ── Row helpers ───────────────────────────────────────────

    def _row_to_instance(self, row: tuple) -> Instance:
        return Instance(
            id=row[0],
            label=row[1],
            url=row[2],
            api_key=row[3],
            owner_email=row[4],
            owner_name=row[5],
            is_active=bool(row[6]),
            registered_at=row[7],
            last_seen_at=row[8],
        )

    def _row_to_presentation(self, row: tuple, instance_label: str = "") -> PublicPresentation:
        return PublicPresentation(
            id=row[0],
            instance_id=row[1],
            remote_id=row[2],
            title=row[3],
            description=row[4],
            created_by=row[5],
            share_token=row[6],
            tags=json.loads(row[7] or "[]"),
            version=row[8],
            template=row[9] or "",
            thumbnail_url=row[10] or "",
            synced_at=row[11],
            instance_label=instance_label,
        )

    # ── Instance methods ──────────────────────────────────────

    def register_instance(
        self,
        label: str,
        url: str,
        api_key: str,
        owner_email: str = "",
        owner_name: str = "",
    ) -> Instance:
        now = time.time()
        cursor = self._db.execute(
            """INSERT INTO instances
               (label, url, api_key, owner_email, owner_name, is_active, registered_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, 1, ?, ?)""",
            (label, url, api_key, owner_email, owner_name, now, now),
        )
        self._db.commit()
        return self.get_instance_by_id(cursor.lastrowid)

    def get_instance_by_id(self, instance_id: int) -> Instance | None:
        row = self._db.execute(
            f"SELECT {self._I_COLS} FROM instances WHERE id=?", (instance_id,)
        ).fetchone()
        return self._row_to_instance(row) if row else None

    def get_instance_by_url(self, url: str) -> Instance | None:
        row = self._db.execute(
            f"SELECT {self._I_COLS} FROM instances WHERE url=?", (url,)
        ).fetchone()
        return self._row_to_instance(row) if row else None

    def list_instances(self, active_only: bool = True) -> list[Instance]:
        if active_only:
            rows = self._db.execute(
                f"SELECT {self._I_COLS} FROM instances WHERE is_active=1 ORDER BY registered_at DESC"
            ).fetchall()
        else:
            rows = self._db.execute(
                f"SELECT {self._I_COLS} FROM instances ORDER BY registered_at DESC"
            ).fetchall()
        return [self._row_to_instance(r) for r in rows]

    def update_last_seen(self, instance_id: int) -> None:
        self._db.execute(
            "UPDATE instances SET last_seen_at=? WHERE id=?", (time.time(), instance_id)
        )
        self._db.commit()

    def deactivate_instance(self, instance_id: int) -> None:
        self._db.execute(
            "UPDATE instances SET is_active=0 WHERE id=?", (instance_id,)
        )
        self._db.commit()

    def get_instance(self, instance_id: int) -> dict | None:
        """Return an instance dict with its synced presentations included."""
        instance = self.get_instance_by_id(instance_id)
        if not instance:
            return None
        p_cols = ", ".join(f"pp.{c.strip()}" for c in self._P_COLS.split(","))
        rows = self._db.execute(
            f"SELECT {p_cols}, i.label"
            " FROM public_presentations pp"
            " JOIN instances i ON i.id = pp.instance_id"
            " WHERE pp.instance_id=?"
            " ORDER BY pp.synced_at DESC",
            (instance_id,),
        ).fetchall()
        presentations = [
            self._row_to_presentation(r, instance_label=r[12]).to_dict() for r in rows
        ]
        return {**instance.to_dict(), "presentations": presentations}

    def get_instance_stats(self) -> dict:
        """Return aggregate stats across all active instances."""
        total_instances = self.count_instances(active_only=True)
        total_presentations = self.count_presentations()
        total_agents = self._db.execute(
            """SELECT COUNT(DISTINCT created_by)
               FROM public_presentations pp
               JOIN instances i ON i.id = pp.instance_id
               WHERE i.is_active=1 AND pp.created_by != ''"""
        ).fetchone()[0]
        return {
            "total_instances": total_instances,
            "total_presentations": total_presentations,
            "total_agents": total_agents,
        }

    # ── Presentation methods ──────────────────────────────────

    def upsert_presentation(
        self,
        instance_id: int,
        remote_id: int,
        title: str,
        description: str,
        created_by: str,
        share_token: str,
        tags: list[str],
        version: int,
        template: str = "",
        thumbnail_url: str = "",
    ) -> PublicPresentation:
        now = time.time()
        tags_json = json.dumps(tags)
        self._db.execute(
            """INSERT INTO public_presentations
               (instance_id, remote_id, title, description, created_by,
                share_token, tags, version, template, thumbnail_url, synced_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(instance_id, remote_id) DO UPDATE SET
                   title=excluded.title,
                   description=excluded.description,
                   created_by=excluded.created_by,
                   share_token=excluded.share_token,
                   tags=excluded.tags,
                   version=excluded.version,
                   template=excluded.template,
                   thumbnail_url=excluded.thumbnail_url,
                   synced_at=excluded.synced_at""",
            (instance_id, remote_id, title, description, created_by,
             share_token, tags_json, version, template, thumbnail_url, now),
        )
        self._db.commit()
        row = self._db.execute(
            f"SELECT {self._P_COLS} FROM public_presentations"
            " WHERE instance_id=? AND remote_id=?",
            (instance_id, remote_id),
        ).fetchone()
        return self._row_to_presentation(row)

    def list_public_presentations(
        self, limit: int = 50, offset: int = 0
    ) -> list[PublicPresentation]:
        p_cols = ", ".join(f"pp.{c.strip()}" for c in self._P_COLS.split(","))
        rows = self._db.execute(
            f"""SELECT {p_cols}, i.label
                FROM public_presentations pp
                JOIN instances i ON i.id = pp.instance_id
                WHERE i.is_active=1
                ORDER BY pp.synced_at DESC
                LIMIT ? OFFSET ?""",
            (limit, offset),
        ).fetchall()
        return [self._row_to_presentation(r, instance_label=r[12]) for r in rows]

    def get_presentation_by_id(self, presentation_id: int) -> PublicPresentation | None:
        p_cols = ", ".join(f"pp.{c.strip()}" for c in self._P_COLS.split(","))
        row = self._db.execute(
            f"""SELECT {p_cols}, i.label
                FROM public_presentations pp
                JOIN instances i ON i.id = pp.instance_id
                WHERE pp.id=? AND i.is_active=1""",
            (presentation_id,),
        ).fetchone()
        return self._row_to_presentation(row, instance_label=row[12]) if row else None

    def get_presentation_by_token(self, share_token: str) -> PublicPresentation | None:
        p_cols = ", ".join(f"pp.{c.strip()}" for c in self._P_COLS.split(","))
        row = self._db.execute(
            f"""SELECT {p_cols}, i.label
                FROM public_presentations pp
                JOIN instances i ON i.id = pp.instance_id
                WHERE pp.share_token=? AND i.is_active=1""",
            (share_token,),
        ).fetchone()
        return self._row_to_presentation(row, instance_label=row[12]) if row else None

    def delete_presentations_for_instance(self, instance_id: int) -> None:
        self._db.execute(
            "DELETE FROM public_presentations WHERE instance_id=?", (instance_id,)
        )
        self._db.commit()

    def count_instances(self, active_only: bool = True) -> int:
        if active_only:
            return self._db.execute(
                "SELECT COUNT(*) FROM instances WHERE is_active=1"
            ).fetchone()[0]
        return self._db.execute("SELECT COUNT(*) FROM instances").fetchone()[0]

    def count_presentations(self) -> int:
        return self._db.execute(
            """SELECT COUNT(*) FROM public_presentations pp
               JOIN instances i ON i.id = pp.instance_id
               WHERE i.is_active=1"""
        ).fetchone()[0]
