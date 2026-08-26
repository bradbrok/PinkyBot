"""Mesh persistence — ferry envelope log + federation peer registry.

Two tables in SQLite (its own DB file by default, sibling to the others
under ``data/``):

- ``mesh_messages`` — every cross-fleet envelope we send or receive,
  one row each. Direction-tagged. Indexed on (local_agent, received_at)
  for the per-agent inbox view (#420 UI) and on correlation_id for
  request/response pairing.
- ``federation_peers`` — known remote agents, keyed by (fleet, agent).
  Tracks first/last seen + ``seed_source`` (``"config"`` for
  pre-configured peers vs ``"observed"`` for those auto-registered
  on first inbound).

Default-deny still lives in the ``mesh_outbound_allowlist`` (per-agent,
in agent_registry); this store is the *audit log + directory*, not the
permission boundary.

PinkyBot issue: #419.
"""

from __future__ import annotations

import json
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pinky_daemon.store_catalog import StoreCatalog, open_store_connection

# -- Records -------------------------------------------------------------------


@dataclass
class MeshMessage:
    """One row in ``mesh_messages``."""

    id: int
    correlation_id: str
    direction: Literal["inbound", "outbound"]
    local_agent: str
    remote_fleet: str
    remote_agent: str
    kind: str
    body: dict  # JSON-decoded
    envelope_ts: int  # millis from envelope.ts
    received_at: float  # our wall clock (seconds since epoch)
    reply_to: str | None = None
    error: str | None = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "correlation_id": self.correlation_id,
            "direction": self.direction,
            "local_agent": self.local_agent,
            "remote_fleet": self.remote_fleet,
            "remote_agent": self.remote_agent,
            "kind": self.kind,
            "body": self.body,
            "envelope_ts": self.envelope_ts,
            "received_at": self.received_at,
            "reply_to": self.reply_to,
            "error": self.error,
        }


@dataclass
class FederationPeer:
    """One row in ``federation_peers``."""

    fleet: str
    agent: str
    display_name: str
    first_seen: float
    last_seen: float
    seed_source: Literal["config", "observed"]

    def to_dict(self) -> dict:
        return {
            "fleet": self.fleet,
            "agent": self.agent,
            "display_name": self.display_name,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "seed_source": self.seed_source,
            "address": f"{self.agent}@{self.fleet}",
        }


# -- Store ---------------------------------------------------------------------


class MeshStore:
    """SQLite-backed mesh log + federation peer registry.

    Thread-safe via a per-instance lock around writes; reads use SQLite's
    own concurrency story.
    """

    def __init__(
        self,
        db_path: str = "data/mesh.db",
        *,
        catalog: StoreCatalog | None = None,
    ) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = open_store_connection(
            catalog,
            "mesh",
            db_path,
            owner=type(self).__name__,
            check_same_thread=False,
        )
        journal_mode = str(
            self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        ).lower()
        self._db.execute("PRAGMA foreign_keys=ON")
        if catalog is not None:
            catalog.register(
                "mesh",
                db_path,
                journal_mode=journal_mode,
                owner=type(self).__name__,
            )
        self._lock = threading.RLock()
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS mesh_messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                correlation_id TEXT NOT NULL DEFAULT '',
                direction TEXT NOT NULL CHECK(direction IN ('inbound','outbound')),
                local_agent TEXT NOT NULL,
                remote_fleet TEXT NOT NULL,
                remote_agent TEXT NOT NULL,
                kind TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '{}',
                envelope_ts INTEGER NOT NULL DEFAULT 0,
                received_at REAL NOT NULL,
                reply_to TEXT,
                error TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_mesh_messages_local
                ON mesh_messages(local_agent, received_at DESC);
            CREATE INDEX IF NOT EXISTS idx_mesh_messages_corr
                ON mesh_messages(correlation_id);
            CREATE INDEX IF NOT EXISTS idx_mesh_messages_direction
                ON mesh_messages(direction, received_at DESC);

            CREATE TABLE IF NOT EXISTS federation_peers (
                fleet TEXT NOT NULL,
                agent TEXT NOT NULL,
                display_name TEXT NOT NULL DEFAULT '',
                first_seen REAL NOT NULL,
                last_seen REAL NOT NULL,
                seed_source TEXT NOT NULL DEFAULT 'observed',
                PRIMARY KEY (fleet, agent)
            );

            CREATE INDEX IF NOT EXISTS idx_federation_peers_seen
                ON federation_peers(last_seen DESC);
        """)
        self._db.commit()

    # -- mesh_messages --------------------------------------------------------

    def log_message(
        self,
        *,
        direction: Literal["inbound", "outbound"],
        local_agent: str,
        remote_fleet: str,
        remote_agent: str,
        correlation_id: str = "",
        kind: str = "",
        body: dict | None = None,
        envelope_ts: int = 0,
        reply_to: str | None = None,
        error: str | None = None,
    ) -> int:
        """Append a row to ``mesh_messages`` and return its id.

        Side effect: also touches ``federation_peers`` — bumps last_seen
        if known, registers as ``seed_source='observed'`` if not. For
        outbound rows, only updates peer state when ``error is None``
        (a failed publish doesn't prove the peer's reachable).
        """
        body_json = json.dumps(body or {}, separators=(",", ":"), ensure_ascii=False)
        now = time.time()
        with self._lock:
            cur = self._db.execute(
                """INSERT INTO mesh_messages
                   (correlation_id, direction, local_agent,
                    remote_fleet, remote_agent, kind, body,
                    envelope_ts, received_at, reply_to, error)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (correlation_id, direction, local_agent,
                 remote_fleet, remote_agent, kind, body_json,
                 envelope_ts, now, reply_to, error),
            )
            row_id = cur.lastrowid or 0
            # Touch peer for inbound-success and outbound-success only.
            should_touch = direction == "inbound" or (direction == "outbound" and error is None)
            if should_touch:
                self._touch_peer_locked(remote_fleet, remote_agent, now)
            self._db.commit()
            return row_id

    def get_inbox(
        self,
        local_agent: str,
        *,
        limit: int = 50,
        offset: int = 0,
        kind: str = "",
        sender: str = "",
        direction: str = "",
    ) -> list[MeshMessage]:
        """Return mesh messages for ``local_agent``, newest first.

        Filters: ``kind`` exact match, ``sender`` matches against
        ``"agent@fleet"``, ``direction`` ∈ ``{"", "inbound", "outbound"}``.
        """
        clauses = ["local_agent = ?"]
        params: list[Any] = [local_agent]
        if kind:
            clauses.append("kind = ?")
            params.append(kind)
        if direction in ("inbound", "outbound"):
            clauses.append("direction = ?")
            params.append(direction)
        if sender:
            # sender is "agent@fleet"; split on rightmost @ for matching
            if "@" in sender:
                agent, _, fleet = sender.rpartition("@")
                if agent and fleet:
                    clauses.append("remote_agent = ? AND remote_fleet = ?")
                    params.extend([agent, fleet])
        where = " AND ".join(clauses)
        params.extend([int(limit), int(offset)])
        rows = self._db.execute(
            f"""SELECT id, correlation_id, direction, local_agent,
                       remote_fleet, remote_agent, kind, body,
                       envelope_ts, received_at, reply_to, error
                FROM mesh_messages
                WHERE {where}
                ORDER BY received_at DESC
                LIMIT ? OFFSET ?""",
            params,
        ).fetchall()
        return [self._row_to_message(r) for r in rows]

    def get_message_by_correlation_id(self, correlation_id: str) -> MeshMessage | None:
        """Fetch the most recent message with this correlation_id (any agent).

        Useful for request/response pairing. Returns None if not found.
        """
        if not correlation_id:
            return None
        row = self._db.execute(
            """SELECT id, correlation_id, direction, local_agent,
                      remote_fleet, remote_agent, kind, body,
                      envelope_ts, received_at, reply_to, error
               FROM mesh_messages
               WHERE correlation_id = ?
               ORDER BY received_at DESC
               LIMIT 1""",
            (correlation_id,),
        ).fetchone()
        return self._row_to_message(row) if row else None

    def count_messages(self, local_agent: str = "") -> int:
        """Count messages, optionally filtered to one agent."""
        if local_agent:
            row = self._db.execute(
                "SELECT COUNT(*) FROM mesh_messages WHERE local_agent = ?",
                (local_agent,),
            ).fetchone()
        else:
            row = self._db.execute("SELECT COUNT(*) FROM mesh_messages").fetchone()
        return int(row[0]) if row else 0

    def _row_to_message(self, row: tuple) -> MeshMessage:
        try:
            body = json.loads(row[7]) if row[7] else {}
            if not isinstance(body, dict):
                body = {"_raw": body}
        except (TypeError, ValueError):
            body = {"_raw": row[7]}
        return MeshMessage(
            id=row[0],
            correlation_id=row[1],
            direction=row[2],
            local_agent=row[3],
            remote_fleet=row[4],
            remote_agent=row[5],
            kind=row[6],
            body=body,
            envelope_ts=row[8],
            received_at=row[9],
            reply_to=row[10],
            error=row[11],
        )

    # -- federation_peers -----------------------------------------------------

    def _touch_peer_locked(self, fleet: str, agent: str, now: float) -> None:
        """Insert-or-update peer's last_seen. Called under self._lock."""
        existing = self._db.execute(
            "SELECT first_seen FROM federation_peers WHERE fleet = ? AND agent = ?",
            (fleet, agent),
        ).fetchone()
        if existing:
            self._db.execute(
                "UPDATE federation_peers SET last_seen = ? WHERE fleet = ? AND agent = ?",
                (now, fleet, agent),
            )
        else:
            self._db.execute(
                """INSERT INTO federation_peers
                   (fleet, agent, display_name, first_seen, last_seen, seed_source)
                   VALUES (?, ?, '', ?, ?, 'observed')""",
                (fleet, agent, now, now),
            )

    def upsert_peer(
        self,
        *,
        fleet: str,
        agent: str,
        display_name: str = "",
        seed_source: Literal["config", "observed"] = "config",
    ) -> None:
        """Insert or update a peer (admin / config-seeded path).

        ``seed_source='config'`` does NOT downgrade an already-observed
        peer to "observed" — config-flagged peers stay flagged. Conversely,
        an already-config peer that becomes observed keeps the 'config'
        flag (config wins, since it's a stronger statement of intent).
        """
        now = time.time()
        with self._lock:
            existing = self._db.execute(
                """SELECT first_seen, seed_source FROM federation_peers
                   WHERE fleet = ? AND agent = ?""",
                (fleet, agent),
            ).fetchone()
            if existing:
                # Keep the stronger seed_source: config beats observed.
                final_source = (
                    "config"
                    if seed_source == "config" or existing[1] == "config"
                    else "observed"
                )
                self._db.execute(
                    """UPDATE federation_peers
                       SET display_name = ?, last_seen = ?, seed_source = ?
                       WHERE fleet = ? AND agent = ?""",
                    (display_name, now, final_source, fleet, agent),
                )
            else:
                self._db.execute(
                    """INSERT INTO federation_peers
                       (fleet, agent, display_name, first_seen, last_seen, seed_source)
                       VALUES (?, ?, ?, ?, ?, ?)""",
                    (fleet, agent, display_name, now, now, seed_source),
                )
            self._db.commit()

    def list_peers(
        self,
        *,
        fleet: str = "",
        seed_source: str = "",
        limit: int = 200,
    ) -> list[FederationPeer]:
        """List peers, newest-seen first. Filter by fleet or seed_source."""
        clauses: list[str] = []
        params: list[Any] = []
        if fleet:
            clauses.append("fleet = ?")
            params.append(fleet)
        if seed_source in ("config", "observed"):
            clauses.append("seed_source = ?")
            params.append(seed_source)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        params.append(int(limit))
        rows = self._db.execute(
            f"""SELECT fleet, agent, display_name, first_seen, last_seen, seed_source
                FROM federation_peers
                {where}
                ORDER BY last_seen DESC
                LIMIT ?""",
            params,
        ).fetchall()
        return [
            FederationPeer(
                fleet=r[0], agent=r[1], display_name=r[2],
                first_seen=r[3], last_seen=r[4], seed_source=r[5],
            )
            for r in rows
        ]

    def get_peer(self, fleet: str, agent: str) -> FederationPeer | None:
        """Look up one peer by composite key."""
        row = self._db.execute(
            """SELECT fleet, agent, display_name, first_seen, last_seen, seed_source
               FROM federation_peers
               WHERE fleet = ? AND agent = ?""",
            (fleet, agent),
        ).fetchone()
        if not row:
            return None
        return FederationPeer(
            fleet=row[0], agent=row[1], display_name=row[2],
            first_seen=row[3], last_seen=row[4], seed_source=row[5],
        )

    def remove_peer(self, fleet: str, agent: str) -> bool:
        """Hard-delete a peer. Returns True if removed."""
        with self._lock:
            cur = self._db.execute(
                "DELETE FROM federation_peers WHERE fleet = ? AND agent = ?",
                (fleet, agent),
            )
            self._db.commit()
            return (cur.rowcount or 0) > 0

    # -- Lifecycle ------------------------------------------------------------

    def close(self) -> None:
        self._db.close()
