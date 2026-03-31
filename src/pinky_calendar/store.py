"""Per-user calendar token storage — encrypted at rest in conversations_agents.db."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import sqlite3
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Optional


def _get_fernet():
    """Build a Fernet cipher from MESH_SECRET."""
    from cryptography.fernet import Fernet
    secret = os.environ.get("MESH_SECRET", "pinky-default-secret-change-me")
    # Derive a 32-byte key from the secret
    key = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(key))


@dataclass
class CalendarToken:
    user_id: str
    provider: str          # "google" | "caldav" | "microsoft"
    access_token: str
    refresh_token: str
    expires_at: float      # Unix timestamp
    extra: dict            # Provider-specific metadata (scopes, caldav URL, etc.)

    def is_expired(self, buffer_seconds: int = 300) -> bool:
        """True if token expires within buffer_seconds."""
        return time.time() >= (self.expires_at - buffer_seconds)


class CalendarTokenStore:
    """Encrypted per-user calendar token storage backed by SQLite."""

    def __init__(self, db_path: str):
        self.db_path = db_path
        self._ensure_table()

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path)

    def _ensure_table(self) -> None:
        with self._conn() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS user_calendar_tokens (
                    user_id     TEXT NOT NULL,
                    provider    TEXT NOT NULL,
                    payload     TEXT NOT NULL,  -- encrypted JSON blob
                    updated_at  REAL NOT NULL,
                    PRIMARY KEY (user_id, provider)
                )
            """)

    def _encrypt(self, data: str) -> str:
        return _get_fernet().encrypt(data.encode()).decode()

    def _decrypt(self, data: str) -> str:
        return _get_fernet().decrypt(data.encode()).decode()

    def save(self, token: CalendarToken) -> None:
        """Encrypt and persist a token."""
        payload = self._encrypt(json.dumps(asdict(token)))
        with self._conn() as conn:
            conn.execute("""
                INSERT INTO user_calendar_tokens (user_id, provider, payload, updated_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, provider) DO UPDATE SET
                    payload=excluded.payload, updated_at=excluded.updated_at
            """, (token.user_id, token.provider, payload, time.time()))

    def load(self, user_id: str, provider: str) -> Optional[CalendarToken]:
        """Load and decrypt a token. Returns None if not found."""
        with self._conn() as conn:
            row = conn.execute(
                "SELECT payload FROM user_calendar_tokens WHERE user_id=? AND provider=?",
                (user_id, provider),
            ).fetchone()
        if not row:
            return None
        try:
            data = json.loads(self._decrypt(row[0]))
            return CalendarToken(**data)
        except Exception:
            return None

    def delete(self, user_id: str, provider: str) -> bool:
        """Remove a token. Returns True if it existed."""
        with self._conn() as conn:
            cur = conn.execute(
                "DELETE FROM user_calendar_tokens WHERE user_id=? AND provider=?",
                (user_id, provider),
            )
        return cur.rowcount > 0

    def list_providers(self, user_id: str) -> list[str]:
        """Return all connected providers for a user."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT provider FROM user_calendar_tokens WHERE user_id=?",
                (user_id,),
            ).fetchall()
        return [r[0] for r in rows]

    def all_users(self) -> list[tuple[str, str]]:
        """Return all (user_id, provider) pairs — useful for batch refresh."""
        with self._conn() as conn:
            rows = conn.execute(
                "SELECT user_id, provider FROM user_calendar_tokens"
            ).fetchall()
        return list(rows)


def default_store() -> CalendarTokenStore:
    """Return a store pointed at the standard Pinky agents DB."""
    db_path = os.environ.get(
        "PINKY_AGENTS_DB",
        str(Path(__file__).parents[3] / "data" / "conversations_agents.db"),
    )
    return CalendarTokenStore(db_path)
