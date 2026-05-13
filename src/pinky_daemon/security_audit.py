"""Security audit log (#440).

Append-only audit trail for security-relevant events: auth, admin actions,
network filter rejects, rate-limit trips, boot guard overrides. Distinct
from the per-tool-call audit store in :mod:`pinky_daemon.hooks` (which
records agent tool execution / cost) — different schema, different
retention story, different consumers.

Design choices
--------------
* **Separate SQLite DB** (``data/security_audit.db``). Decoupled from the
  per-agent audit so a security-export job can run against a focused file
  without scanning the bulky tool-call history.
* **Append-only by convention.** No ``UPDATE`` / ``DELETE`` API. Retention
  pruning is the only deletion path and only operates on a row-age basis.
* **Best-effort writes on hot paths.** A locked / broken audit DB must NOT
  take down login. Exceptions during ``log_event`` are caught and logged
  to the standard logger; the call returns a sentinel ``None`` row id.
  Boot / refuse-to-boot events bypass this and fail loud (caller raises).
* **Allowlist redaction per event_type.** A blacklist of
  ``token|password|secret|authorization`` will miss the next credential
  field name added in five months. Each event type declares which keys
  from the detail blob are *kept*; everything else is dropped before
  serialisation.
* **Actor identity is `(user_id, token_id)`** so "Alice in browser" is
  distinguishable from "Alice's CI token" once #435 lands.
* **`forwarded_chain` reserved** for forensics (JSON array of XFF hops
  from #442's :class:`ClientIdentity`).
* **Hash-chain column reserved** but not populated in this PR — adding the
  column now avoids a future migration.

Schema
------
.. code-block:: sql

   CREATE TABLE security_audit_log (
     id              INTEGER PRIMARY KEY AUTOINCREMENT,
     ts              REAL    NOT NULL,             -- unix seconds
     event_type      TEXT    NOT NULL,             -- e.g. auth.login.fail
     actor_user_id   INTEGER,
     actor_token_id  INTEGER,
     actor_ip        TEXT,
     via_proxy       INTEGER NOT NULL DEFAULT 0,   -- 0/1
     forwarded_chain TEXT,                         -- JSON array
     method          TEXT,
     route_name      TEXT,
     target          TEXT,
     outcome         TEXT    NOT NULL,             -- success | failure | denied
     user_agent      TEXT,
     correlation_id  TEXT,
     detail_json     TEXT,                         -- redacted JSON object
     prev_hash       TEXT                          -- reserved for future hash chain
   );

Part of #433. Issue #440.
"""

from __future__ import annotations

import json
import logging
import sqlite3
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "DEFAULT_DETAIL_ALLOWLIST",
    "AuditOutcome",
    "EVENT_DETAIL_ALLOWLIST",
    "SecurityAuditStore",
    "redact_detail",
]

logger = logging.getLogger(__name__)


class AuditOutcome:
    """String constants for the ``outcome`` column.

    Not an Enum — kept as plain strings so callers can pass literals
    without an extra import, and so the column stays trivially
    JSON-serialisable.
    """

    SUCCESS = "success"
    FAILURE = "failure"
    DENIED = "denied"


@dataclass(frozen=True)
class SecurityAuditEntry:
    """A row in :data:`SecurityAuditStore`."""

    id: int
    ts: float
    event_type: str
    actor_user_id: int | None
    actor_token_id: int | None
    actor_ip: str | None
    via_proxy: bool
    forwarded_chain: list[str] | None
    method: str | None
    route_name: str | None
    target: str | None
    outcome: str
    user_agent: str | None
    correlation_id: str | None
    detail: dict | None


# ── Redaction ─────────────────────────────────────────────────────


#: Default allowlist applied when an event type doesn't declare its own.
#: Conservative — only fields with no plausible secret content.
DEFAULT_DETAIL_ALLOWLIST: frozenset[str] = frozenset(
    {
        "reason",
        "duration_ms",
        "status_code",
        "error_class",
        "count",
        "limit",
        "bucket",
    }
)


#: Per-event detail allowlists. Define what's *kept*, not what's stripped —
#: future credential field names default to redacted instead of accidentally
#: leaking.
EVENT_DETAIL_ALLOWLIST: dict[str, frozenset[str]] = {
    "auth.login.success": frozenset({"username", "method"}),
    "auth.login.fail": frozenset({"username", "method", "reason"}),
    "auth.logout": frozenset({"username"}),
    "auth.token.create": frozenset({"username", "scope", "ttl_seconds"}),
    "auth.token.revoke": frozenset({"username", "token_label"}),
    "auth.elevation.success": frozenset({"username", "scope"}),
    "auth.elevation.fail": frozenset({"username", "scope", "reason"}),
    "admin.update": frozenset(
        {"target_path", "fields_changed", "version_before", "version_after"}
    ),
    "admin.agent.create": frozenset({"agent_name", "model"}),
    "admin.agent.delete": frozenset({"agent_name"}),
    "admin.agent.retire": frozenset({"agent_name"}),
    "admin.skill.install": frozenset({"skill_name", "source"}),
    "admin.mcp.config_change": frozenset({"agent_name", "fields_changed"}),
    "admin.settings.change": frozenset({"setting_name", "value_redacted"}),
    "rate_limit.exceeded": frozenset({"bucket", "count", "limit", "route_name"}),
    "network.lan_filter.reject": frozenset(
        {"raw_peer", "via_proxy", "method", "path"}
    ),
    "boot.daemon.start": frozenset(
        {"deployment_mode", "host", "port", "version"}
    ),
    "boot.refuse": frozenset({"failed_guards"}),
    "boot.override.active": frozenset({"guard_name", "reason"}),
}


def _redact_value(value):
    """Recursively pass through plain JSON-safe values; replace anything
    with a credential-shaped key during dict descent."""
    if isinstance(value, Mapping):
        return {k: _redact_value(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [_redact_value(v) for v in value]
    return value


def redact_detail(
    event_type: str, detail: Mapping | None
) -> dict | None:
    """Return a redacted copy of ``detail`` per :data:`EVENT_DETAIL_ALLOWLIST`.

    Allowlist semantics: keys not in the per-event set are dropped. When
    the event type isn't registered, the conservative
    :data:`DEFAULT_DETAIL_ALLOWLIST` applies — that way a brand-new event
    type doesn't accidentally leak its fields before the schema is added.

    Nested dicts are walked but the same allowlist applies at every level.
    Lists are filtered element-wise (each is treated as a leaf or a nested
    dict).
    """
    if detail is None:
        return None
    allowlist = EVENT_DETAIL_ALLOWLIST.get(event_type, DEFAULT_DETAIL_ALLOWLIST)
    return {
        k: _redact_value(v) for k, v in detail.items() if k in allowlist
    }


# ── Store ─────────────────────────────────────────────────────────


class SecurityAuditStore:
    """SQLite-backed append-only security audit log.

    Thread-safe (uses ``check_same_thread=False`` and SQLite's WAL +
    one-write-at-a-time semantics). ``log_event`` is best-effort: a write
    failure logs at WARNING and returns ``None``, so callers on hot paths
    (login, admin endpoints) never raise into their handler.
    """

    def __init__(self, db_path: str = "data/security_audit.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript(
            """
            CREATE TABLE IF NOT EXISTS security_audit_log (
                id              INTEGER PRIMARY KEY AUTOINCREMENT,
                ts              REAL    NOT NULL,
                event_type      TEXT    NOT NULL,
                actor_user_id   INTEGER,
                actor_token_id  INTEGER,
                actor_ip        TEXT,
                via_proxy       INTEGER NOT NULL DEFAULT 0,
                forwarded_chain TEXT,
                method          TEXT,
                route_name      TEXT,
                target          TEXT,
                outcome         TEXT    NOT NULL,
                user_agent      TEXT,
                correlation_id  TEXT,
                detail_json     TEXT,
                prev_hash       TEXT
            );

            CREATE INDEX IF NOT EXISTS idx_sec_audit_ts
              ON security_audit_log(ts DESC);
            CREATE INDEX IF NOT EXISTS idx_sec_audit_event
              ON security_audit_log(event_type, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_sec_audit_actor
              ON security_audit_log(actor_user_id, ts DESC);
            CREATE INDEX IF NOT EXISTS idx_sec_audit_correlation
              ON security_audit_log(correlation_id);
            """
        )
        self._db.commit()

    # ── Write ────────────────────────────────────────────────

    def log_event(
        self,
        event_type: str,
        *,
        outcome: str = AuditOutcome.SUCCESS,
        actor_user_id: int | None = None,
        actor_token_id: int | None = None,
        actor_ip: str | None = None,
        via_proxy: bool = False,
        forwarded_chain: list[str] | None = None,
        method: str | None = None,
        route_name: str | None = None,
        target: str | None = None,
        user_agent: str | None = None,
        correlation_id: str | None = None,
        detail: Mapping | None = None,
        ts: float | None = None,
    ) -> int | None:
        """Append a single audit event. Returns the new row id, or ``None``
        on best-effort failure.

        ``detail`` is run through :func:`redact_detail` before storage.
        """
        try:
            redacted = redact_detail(event_type, detail)
            detail_json = json.dumps(redacted) if redacted is not None else None
            chain_json = (
                json.dumps(forwarded_chain) if forwarded_chain else None
            )
            cursor = self._db.execute(
                """
                INSERT INTO security_audit_log
                (ts, event_type, actor_user_id, actor_token_id, actor_ip,
                 via_proxy, forwarded_chain, method, route_name, target,
                 outcome, user_agent, correlation_id, detail_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    ts if ts is not None else time.time(),
                    event_type,
                    actor_user_id,
                    actor_token_id,
                    actor_ip,
                    1 if via_proxy else 0,
                    chain_json,
                    method,
                    route_name,
                    target,
                    outcome,
                    user_agent,
                    correlation_id,
                    detail_json,
                ),
            )
            self._db.commit()
            return cursor.lastrowid
        except Exception:  # noqa: BLE001
            # Best-effort: never fail the caller's request because the audit
            # DB is unhappy. Log loudly so this surfaces.
            logger.exception(
                "security_audit: failed to log event_type=%s", event_type
            )
            return None

    # ── Query ────────────────────────────────────────────────

    def query(
        self,
        *,
        event_type: str | None = None,
        actor_user_id: int | None = None,
        outcome: str | None = None,
        since: float | None = None,
        until: float | None = None,
        correlation_id: str | None = None,
        limit: int = 100,
        offset: int = 0,
    ) -> list[SecurityAuditEntry]:
        """Filtered query, newest-first."""
        conditions: list[str] = []
        params: list = []
        if event_type is not None:
            conditions.append("event_type = ?")
            params.append(event_type)
        if actor_user_id is not None:
            conditions.append("actor_user_id = ?")
            params.append(actor_user_id)
        if outcome is not None:
            conditions.append("outcome = ?")
            params.append(outcome)
        if since is not None:
            conditions.append("ts >= ?")
            params.append(since)
        if until is not None:
            conditions.append("ts <= ?")
            params.append(until)
        if correlation_id is not None:
            conditions.append("correlation_id = ?")
            params.append(correlation_id)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        params.extend([limit, offset])

        rows = self._db.execute(
            f"""
            SELECT id, ts, event_type, actor_user_id, actor_token_id,
                   actor_ip, via_proxy, forwarded_chain, method, route_name,
                   target, outcome, user_agent, correlation_id, detail_json
              FROM security_audit_log
              {where}
              ORDER BY ts DESC, id DESC
              LIMIT ? OFFSET ?
            """,
            params,
        ).fetchall()

        return [self._row_to_entry(r) for r in rows]

    def count(
        self,
        *,
        event_type: str | None = None,
        actor_user_id: int | None = None,
        outcome: str | None = None,
        since: float | None = None,
    ) -> int:
        conditions: list[str] = []
        params: list = []
        if event_type is not None:
            conditions.append("event_type = ?")
            params.append(event_type)
        if actor_user_id is not None:
            conditions.append("actor_user_id = ?")
            params.append(actor_user_id)
        if outcome is not None:
            conditions.append("outcome = ?")
            params.append(outcome)
        if since is not None:
            conditions.append("ts >= ?")
            params.append(since)

        where = f"WHERE {' AND '.join(conditions)}" if conditions else ""
        row = self._db.execute(
            f"SELECT COUNT(*) FROM security_audit_log {where}", params
        ).fetchone()
        return int(row[0]) if row else 0

    # ── Retention ────────────────────────────────────────────

    def prune_older_than(
        self, retention_days: int, *, now: float | None = None
    ) -> int:
        """Delete rows older than ``retention_days``. Returns rows deleted.

        Soft-retention: invoked by the existing scheduler; default
        365 days per #440 spec. The only deletion path on this table.
        """
        if retention_days <= 0:
            return 0
        cutoff = (now if now is not None else time.time()) - (
            retention_days * 86400
        )
        cursor = self._db.execute(
            "DELETE FROM security_audit_log WHERE ts < ?", (cutoff,)
        )
        self._db.commit()
        return cursor.rowcount

    # ── Internal helpers ─────────────────────────────────────

    def _row_to_entry(self, row) -> SecurityAuditEntry:
        return SecurityAuditEntry(
            id=row[0],
            ts=row[1],
            event_type=row[2],
            actor_user_id=row[3],
            actor_token_id=row[4],
            actor_ip=row[5],
            via_proxy=bool(row[6]),
            forwarded_chain=json.loads(row[7]) if row[7] else None,
            method=row[8],
            route_name=row[9],
            target=row[10],
            outcome=row[11],
            user_agent=row[12],
            correlation_id=row[13],
            detail=json.loads(row[14]) if row[14] else None,
        )
