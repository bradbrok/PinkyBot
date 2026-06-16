"""Per-call audit sink for QBO tool calls (spec §6.6).

Exactly one row per tool call (on success AND on error). NO payloads — only
metadata: agent, tool, realm (hashed, since the realm id is broadly sensitive),
endpoint family, date range, row count, request id, success flag, error string.

Table ``qbo_audit`` lives in the agents DB (path from env ``PINKY_AGENTS_DB``).
The writer opens its own short-lived connection per write so it can be called
from the stdio server process without sharing the daemon's connection.

Never writes financial data or secrets.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
import time

_ENV_DB = "PINKY_AGENTS_DB"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS qbo_audit (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    ts              REAL    NOT NULL,
    agent           TEXT    NOT NULL,
    tool            TEXT    NOT NULL,
    realm_hash      TEXT    NOT NULL DEFAULT '',
    endpoint_family TEXT    NOT NULL DEFAULT '',
    date_range      TEXT    NOT NULL DEFAULT '',
    row_count       INTEGER NOT NULL DEFAULT 0,
    request_id      TEXT    NOT NULL DEFAULT '',
    success         INTEGER NOT NULL DEFAULT 0,
    error           TEXT    NOT NULL DEFAULT ''
);
"""


def _log(msg: str) -> None:
    print(f"[pinky-qbo.audit] {msg}", file=sys.stderr)


def _db_path(db_path: str | None = None) -> str | None:
    return (db_path or os.environ.get(_ENV_DB, "")).strip() or None


def hash_realm(realm_id: str | None) -> str:
    """Return a stable, non-reversible short hash of a realm id (or '')."""
    if not realm_id:
        return ""
    digest = hashlib.sha256(realm_id.encode("utf-8")).hexdigest()
    return digest[:16]


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def ensure_table(db_path: str | None = None) -> bool:
    """Create the qbo_audit table if needed. Returns True on success."""
    path = _db_path(db_path)
    if not path:
        return False
    try:
        conn = _connect(path)
        try:
            conn.executescript(_SCHEMA)
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error as e:
        _log(f"ensure_table failed: {e}")
        return False


def write_audit(
    *,
    agent: str,
    tool: str,
    realm_id: str | None = None,
    endpoint_family: str = "",
    date_range: str = "",
    row_count: int = 0,
    request_id: str = "",
    success: bool = True,
    error: str = "",
    db_path: str | None = None,
) -> bool:
    """Write exactly one audit row. Never raises — auditing must not break a call.

    The realm id is hashed before storage. No payloads are recorded.

    Returns True if the row was written, False otherwise.
    """
    path = _db_path(db_path)
    if not path:
        _log(f"{_ENV_DB} not set — audit row dropped (tool={tool}, agent={agent})")
        return False
    try:
        conn = _connect(path)
        try:
            conn.executescript(_SCHEMA)
            conn.execute(
                "INSERT INTO qbo_audit "
                "(ts, agent, tool, realm_hash, endpoint_family, date_range, "
                " row_count, request_id, success, error) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    time.time(),
                    agent,
                    tool,
                    hash_realm(realm_id),
                    endpoint_family,
                    date_range,
                    int(row_count),
                    request_id,
                    1 if success else 0,
                    error[:2000] if error else "",
                ),
            )
            conn.commit()
        finally:
            conn.close()
        return True
    except sqlite3.Error as e:
        _log(f"write_audit failed (tool={tool}): {e}")
        return False
