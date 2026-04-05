"""User Profile Store — structured, learned knowledge about each user.

Profiles are built from:
- Dream consolidation (auto-learned from conversations)
- Manual edits (owner/admin corrections via UI)
- Onboarding (initial setup)

Each entry is a (chat_id, category, key) → value triple with metadata
about confidence, source, and timestamps. Agents can be granted visibility
to specific users' profiles via the visibility table.
"""

from __future__ import annotations

import json
import sqlite3
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path


@dataclass
class ProfileEntry:
    """A single learned trait about a user."""

    id: int = 0
    chat_id: str = ""
    category: str = ""  # identity, communication, preferences, work, personal, patterns
    key: str = ""  # specific trait name
    value: str = ""  # the learned value
    confidence: float = 0.5  # 0.0–1.0, increases with reinforcement
    source: str = "dream"  # dream, manual, onboarding
    learned_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ProfileVisibility:
    """Whether an agent can see a specific user's profile."""

    agent_name: str = ""
    chat_id: str = ""
    visible: bool = True


# Standard categories and their descriptions
PROFILE_CATEGORIES = {
    "identity": "Name, pronouns, role, timezone, language",
    "communication": "Preferred style, formality, response length, language",
    "preferences": "Likes, dislikes, tools, workflows, conventions",
    "work": "Projects, tech stack, company, team, goals",
    "personal": "Interests, family, hobbies, life context",
    "patterns": "When active, interaction habits, recurring requests",
}


class UserProfileStore:
    """SQLite-backed store for structured user profiles."""

    def __init__(self, db_path: str = "data/user_profiles.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                chat_id TEXT NOT NULL,
                category TEXT NOT NULL,
                key TEXT NOT NULL,
                value TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0.5,
                source TEXT NOT NULL DEFAULT 'dream',
                learned_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                UNIQUE(chat_id, category, key)
            );
            CREATE INDEX IF NOT EXISTS idx_profiles_chat
                ON user_profiles(chat_id);
            CREATE INDEX IF NOT EXISTS idx_profiles_category
                ON user_profiles(chat_id, category);

            CREATE TABLE IF NOT EXISTS profile_visibility (
                agent_name TEXT NOT NULL,
                chat_id TEXT NOT NULL,
                visible INTEGER NOT NULL DEFAULT 1,
                PRIMARY KEY (agent_name, chat_id)
            );
            CREATE INDEX IF NOT EXISTS idx_visibility_agent
                ON profile_visibility(agent_name);
        """)
        self._db.commit()

    # ── Profile Entries ──────────────────────────────────

    def upsert(self, entry: ProfileEntry) -> ProfileEntry:
        """Insert or update a profile entry. Returns the saved entry."""
        now = time.time()
        if not entry.learned_at:
            entry.learned_at = now
        entry.updated_at = now

        self._db.execute(
            """
            INSERT INTO user_profiles
                (chat_id, category, key, value, confidence, source, learned_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, category, key) DO UPDATE SET
                value = excluded.value,
                confidence = MAX(user_profiles.confidence, excluded.confidence),
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            (
                entry.chat_id,
                entry.category,
                entry.key,
                entry.value,
                entry.confidence,
                entry.source,
                entry.learned_at,
                entry.updated_at,
            ),
        )
        self._db.commit()

        row = self._db.execute(
            "SELECT id FROM user_profiles WHERE chat_id=? AND category=? AND key=?",
            (entry.chat_id, entry.category, entry.key),
        ).fetchone()
        if row:
            entry.id = row[0]
        return entry

    def bulk_upsert(self, entries: list[ProfileEntry]) -> int:
        """Upsert multiple entries in a single transaction. Returns count."""
        now = time.time()
        rows = []
        for e in entries:
            if not e.learned_at:
                e.learned_at = now
            e.updated_at = now
            rows.append((
                e.chat_id, e.category, e.key, e.value,
                e.confidence, e.source, e.learned_at, e.updated_at,
            ))
        self._db.executemany(
            """
            INSERT INTO user_profiles
                (chat_id, category, key, value, confidence, source, learned_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(chat_id, category, key) DO UPDATE SET
                value = excluded.value,
                confidence = MAX(user_profiles.confidence, excluded.confidence),
                source = excluded.source,
                updated_at = excluded.updated_at
            """,
            rows,
        )
        self._db.commit()
        return len(rows)

    def get(self, entry_id: int) -> ProfileEntry | None:
        """Get a single entry by ID."""
        row = self._db.execute(
            "SELECT * FROM user_profiles WHERE id=?", (entry_id,)
        ).fetchone()
        return self._row_to_entry(row) if row else None

    def get_user_profile(
        self,
        chat_id: str,
        category: str = "",
        min_confidence: float = 0.0,
    ) -> list[ProfileEntry]:
        """Get all profile entries for a user, optionally filtered by category."""
        if category:
            rows = self._db.execute(
                """SELECT * FROM user_profiles
                   WHERE chat_id=? AND category=? AND confidence>=?
                   ORDER BY category, key""",
                (chat_id, category, min_confidence),
            ).fetchall()
        else:
            rows = self._db.execute(
                """SELECT * FROM user_profiles
                   WHERE chat_id=? AND confidence>=?
                   ORDER BY category, key""",
                (chat_id, min_confidence),
            ).fetchall()
        return [self._row_to_entry(r) for r in rows]

    def get_all_users(self) -> list[str]:
        """Return all distinct chat_ids with profile entries."""
        rows = self._db.execute(
            "SELECT DISTINCT chat_id FROM user_profiles ORDER BY chat_id"
        ).fetchall()
        return [r[0] for r in rows]

    def delete_entry(self, entry_id: int) -> bool:
        """Delete a specific profile entry."""
        cur = self._db.execute("DELETE FROM user_profiles WHERE id=?", (entry_id,))
        self._db.commit()
        return cur.rowcount > 0

    def delete_user_profile(self, chat_id: str) -> int:
        """Delete all profile entries for a user. Returns count deleted."""
        cur = self._db.execute("DELETE FROM user_profiles WHERE chat_id=?", (chat_id,))
        self._db.commit()
        return cur.rowcount

    def update_entry(
        self,
        entry_id: int,
        value: str | None = None,
        confidence: float | None = None,
        source: str | None = None,
    ) -> ProfileEntry | None:
        """Update specific fields of a profile entry."""
        parts = []
        params: list = []
        if value is not None:
            parts.append("value=?")
            params.append(value)
        if confidence is not None:
            parts.append("confidence=?")
            params.append(confidence)
        if source is not None:
            parts.append("source=?")
            params.append(source)
        if not parts:
            return self.get(entry_id)
        parts.append("updated_at=?")
        params.append(time.time())
        params.append(entry_id)
        self._db.execute(
            f"UPDATE user_profiles SET {', '.join(parts)} WHERE id=?",
            params,
        )
        self._db.commit()
        return self.get(entry_id)

    # ── Visibility ───────────────────────────────────────

    def set_visibility(
        self, agent_name: str, chat_id: str, visible: bool = True
    ) -> None:
        """Set whether an agent can see a user's profile."""
        self._db.execute(
            """
            INSERT INTO profile_visibility (agent_name, chat_id, visible)
            VALUES (?, ?, ?)
            ON CONFLICT(agent_name, chat_id) DO UPDATE SET visible=excluded.visible
            """,
            (agent_name, chat_id, 1 if visible else 0),
        )
        self._db.commit()

    def get_visibility(self, agent_name: str, chat_id: str) -> bool:
        """Check if an agent can see a user's profile. Default: True (visible)."""
        row = self._db.execute(
            "SELECT visible FROM profile_visibility WHERE agent_name=? AND chat_id=?",
            (agent_name, chat_id),
        ).fetchone()
        return bool(row[0]) if row else True  # Default visible

    def list_visibility(self, agent_name: str) -> list[dict]:
        """List all visibility settings for an agent."""
        rows = self._db.execute(
            "SELECT chat_id, visible FROM profile_visibility WHERE agent_name=?",
            (agent_name,),
        ).fetchall()
        return [{"chat_id": r[0], "visible": bool(r[1])} for r in rows]

    def get_visible_profile(
        self,
        agent_name: str,
        chat_id: str,
        min_confidence: float = 0.0,
    ) -> list[ProfileEntry]:
        """Get a user's profile entries only if the agent has visibility."""
        if not self.get_visibility(agent_name, chat_id):
            return []
        return self.get_user_profile(chat_id, min_confidence=min_confidence)

    # ── Formatting ───────────────────────────────────────

    def format_profile_for_prompt(
        self,
        agent_name: str,
        chat_id: str,
        display_name: str = "",
        min_confidence: float = 0.3,
    ) -> str:
        """Format a user's visible profile as markdown for system prompt injection."""
        entries = self.get_visible_profile(agent_name, chat_id, min_confidence)
        if not entries:
            return ""

        label = display_name or chat_id
        lines = [f"### Known about {label}"]

        by_category: dict[str, list[ProfileEntry]] = {}
        for e in entries:
            by_category.setdefault(e.category, []).append(e)

        for cat in PROFILE_CATEGORIES:
            if cat not in by_category:
                continue
            lines.append(f"**{cat.title()}:**")
            for e in by_category[cat]:
                conf = "high" if e.confidence >= 0.8 else "med" if e.confidence >= 0.5 else "low"
                lines.append(f"- {e.key}: {e.value} ({conf} confidence)")

        return "\n".join(lines)

    # ── Stats ────────────────────────────────────────────

    def stats(self) -> dict:
        """Return aggregate stats about stored profiles."""
        row = self._db.execute(
            "SELECT COUNT(DISTINCT chat_id), COUNT(*) FROM user_profiles"
        ).fetchone()
        return {
            "total_users": row[0] if row else 0,
            "total_entries": row[1] if row else 0,
        }

    # ── Internal ─────────────────────────────────────────

    def _row_to_entry(self, row) -> ProfileEntry:
        return ProfileEntry(
            id=row[0],
            chat_id=row[1],
            category=row[2],
            key=row[3],
            value=row[4],
            confidence=row[5],
            source=row[6],
            learned_at=row[7],
            updated_at=row[8],
        )
