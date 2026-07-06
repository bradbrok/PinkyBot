"""Per-call audit sink for QBO MCP requests.

Inserts one row per call into a local SQLite `qbo_audit` table.
Payloads are NEVER stored — only metadata for security audit purposes.

DB path: env PINKY_QBO_AUDIT_DB or defaults to data/qbo_audit.db.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

_DEFAULT_DB = "data/qbo_audit.db"

_CREATE_TABLE = """
CREATE TABLE IF NOT EXISTS qbo_audit (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            TEXT    NOT NULL,
    agent         TEXT    NOT NULL,
    tool          TEXT    NOT NULL,
    realm_hash    TEXT    NOT NULL,
    endpoint_family TEXT  NOT NULL,
    date_range    TEXT    NOT NULL DEFAULT '',
    row_count     INTEGER NOT NULL DEFAULT -1,
    request_id    TEXT    NOT NULL DEFAULT '',
    status        TEXT    NOT NULL
)
"""


def _get_db() -> sqlite3.Connection:
    db_path = os.environ.get("PINKY_QBO_AUDIT_DB", _DEFAULT_DB)
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.execute(_CREATE_TABLE)
    conn.commit()
    return conn


# Module-level lazy connection
_conn: sqlite3.Connection | None = None


def _connection() -> sqlite3.Connection:
    global _conn
    if _conn is None:
        _conn = _get_db()
    return _conn


def record(
    *,
    agent: str,
    tool: str,
    realm_hash: str,
    endpoint_family: str,
    date_range: str = "",
    row_count: int = -1,
    request_id: str = "",
    status: str,
) -> None:
    """Insert one audit row. Never raises — logs to stderr on failure."""
    try:
        conn = _connection()
        conn.execute(
            """
            INSERT INTO qbo_audit
                (ts, agent, tool, realm_hash, endpoint_family, date_range, row_count, request_id, status)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                datetime.now(tz=timezone.utc).isoformat(),
                agent,
                tool,
                realm_hash,
                endpoint_family,
                date_range,
                row_count,
                request_id,
                status,
            ),
        )
        conn.commit()
    except Exception as exc:
        print(f"[pinky-qbo] audit.record failed: {exc}", file=sys.stderr)


def hash_realm(realm_id: str) -> str:
    """Return a stable non-reversible hash of the realm ID for audit logs."""
    return hashlib.sha256(realm_id.encode()).hexdigest()[:16]
