"""Dream Runner — nightly memory consolidation for agents.

Spawns a dedicated dream agent (SDK run) that processes each agent's
conversation history and distills it into durable memory nodes via
pinky-memory reflect. After the run, stores a summary and watermark
in the dream_state table so the morning wake context can include it.

Usage:
    runner = DreamRunner(db_path="data/dream_state.db")
    if runner.should_dream("oleg", agent_config):
        summary = await runner.run_dream("oleg", agent_config)
"""

from __future__ import annotations

import sqlite3
import sys
import time
from pathlib import Path

from pinky_daemon.dream_prompt import DREAM_SYSTEM_PROMPT
from pinky_daemon.sdk_runner import SDKRunner, SDKRunnerConfig


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# How long between dream runs (seconds). Default: 20 hours so nightly cron
# won't double-fire if the scheduler ticks slightly early.
_DREAM_COOLDOWN_S = 20 * 3600

# Morning delivery window: deliver the summary if the dream ran within this
# many seconds of "now" (12 hours).
_MORNING_WINDOW_S = 12 * 3600

# Restricted tool set for dream agent — memory + history only, no messaging.
_DREAM_ALLOWED_TOOLS = [
    "mcp__pinky-memory__recall",
    "mcp__pinky-memory__reflect",
    "mcp__pinky-self__search_history",
]


class DreamRunner:
    """Runs nightly dream consolidation sessions for agents.

    Manages the dream_state SQLite table (tracks last run, last summary,
    and aggregate stats). Each dream spawns a one-shot SDKRunner with the
    dream system prompt and restricted tool access.
    """

    def __init__(self, db_path: str = "data/dream_state.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS dream_state (
                agent_name TEXT PRIMARY KEY,
                last_dream_at REAL,
                last_summary TEXT,
                sessions_processed INT DEFAULT 0,
                memories_stored INT DEFAULT 0
            );
        """)
        self._db.commit()
        self._migrate_tables()

    def _migrate_tables(self) -> None:
        """Apply incremental schema migrations."""
        # Migration: add last_notified_at column (tracks when summary was last delivered)
        cols = {
            row[1]
            for row in self._db.execute("PRAGMA table_info(dream_state)").fetchall()
        }
        if "last_notified_at" not in cols:
            self._db.execute(
                "ALTER TABLE dream_state ADD COLUMN last_notified_at REAL"
            )
            self._db.commit()
        # Migration: rename sessions_processed -> last_sessions_processed
        if "last_sessions_processed" not in cols:
            self._db.execute(
                "ALTER TABLE dream_state ADD COLUMN last_sessions_processed INT DEFAULT 0"
            )
            self._db.execute(
                "UPDATE dream_state SET last_sessions_processed = sessions_processed"
            )
            self._db.commit()
        # Migration: rename memories_stored -> last_memories_stored
        if "last_memories_stored" not in cols:
            self._db.execute(
                "ALTER TABLE dream_state ADD COLUMN last_memories_stored INT DEFAULT 0"
            )
            self._db.execute(
                "UPDATE dream_state SET last_memories_stored = memories_stored"
            )
            self._db.commit()

    # ── Public API ────────────────────────────────────────────

    def should_dream(self, agent_name: str, agent_config) -> bool:
        """Return True if this agent is due for a dream run.

        Checks:
        - dream_enabled is True on the agent config
        - At least _DREAM_COOLDOWN_S has elapsed since last_dream_at
        """
        if not getattr(agent_config, "dream_enabled", False):
            return False

        row = self._db.execute(
            "SELECT last_dream_at FROM dream_state WHERE agent_name=?",
            (agent_name,),
        ).fetchone()

        if not row or not row[0]:
            return True  # Never dreamed before

        elapsed = time.time() - row[0]
        return elapsed >= _DREAM_COOLDOWN_S

    async def run_dream(self, agent_name: str, agent_config) -> str:
        """Spawn a dream SDK run for the agent and store the summary.

        Args:
            agent_name: The agent's unique name.
            agent_config: Agent dataclass / object with .working_dir etc.

        Returns:
            The summary line emitted by the dream agent, or an error string.
        """
        _log(f"dream-runner: starting dream for '{agent_name}'")

        # Build system prompt with agent name substituted
        system_prompt = DREAM_SYSTEM_PROMPT.format(agent_name=agent_name)

        # Resolve working directory (same as normal streaming session)
        work_dir = "."
        if getattr(agent_config, "working_dir", ""):
            work_dir = str(Path(agent_config.working_dir).resolve())

        config = SDKRunnerConfig(
            working_dir=work_dir,
            model=getattr(agent_config, "model", None) or None,
            allowed_tools=_DREAM_ALLOWED_TOOLS,
            permission_mode="bypassPermissions",
            system_prompt=system_prompt,
            max_turns=50,  # Dream runs are bounded but need enough turns
        )

        runner = SDKRunner(config, agent_name=agent_name)

        # Kick off the dream with a minimal prompt — the system prompt contains
        # all the instructions; the user message just triggers execution.
        prompt = (
            f"Begin the memory consolidation process for agent {agent_name}. "
            "Follow the process in your system prompt exactly. Work through all "
            "phases and end with the summary line."
        )

        start = time.time()
        result = await runner.run(prompt)
        elapsed = time.time() - start

        summary = result.output.strip() if result.output else ""
        if not summary:
            summary = f"Dream run failed or produced no output (exit={result.exit_code})"
            if result.error:
                summary += f": {result.error}"

        _log(f"dream-runner: '{agent_name}' dream complete in {elapsed:.1f}s — {summary[:120]}")

        self._save_state(agent_name, summary)
        return summary

    def get_morning_summary(self, agent_name: str) -> str | None:
        """Return the dream summary if a dream ran in the last 12 hours and has
        not yet been delivered for this dream cycle.

        Delivers at most once per dream run: returns the summary only when
        ``last_dream_at`` is within the morning window AND either
        ``last_notified_at`` is NULL or predates ``last_dream_at``.  After
        returning the summary, stamps ``last_notified_at`` so subsequent calls
        for the same dream cycle return None.

        Returns None if no recent dream exists or it was already delivered.
        """
        row = self._db.execute(
            "SELECT last_dream_at, last_summary, last_notified_at"
            " FROM dream_state WHERE agent_name=?",
            (agent_name,),
        ).fetchone()

        if not row or not row[0] or not row[1]:
            return None

        last_dream_at, last_summary, last_notified_at = row

        age = time.time() - last_dream_at
        if age > _MORNING_WINDOW_S:
            return None

        # Already notified for this dream cycle — skip
        if last_notified_at is not None and last_notified_at >= last_dream_at:
            return None

        # Mark as delivered
        self._db.execute(
            "UPDATE dream_state SET last_notified_at=? WHERE agent_name=?",
            (time.time(), agent_name),
        )
        self._db.commit()

        return last_summary

    def get_state(self, agent_name: str) -> dict:
        """Return the full dream_state row for an agent as a dict."""
        row = self._db.execute(
            """SELECT agent_name, last_dream_at, last_summary,
                      last_sessions_processed, last_memories_stored
               FROM dream_state WHERE agent_name=?""",
            (agent_name,),
        ).fetchone()

        if not row:
            return {
                "agent_name": agent_name,
                "last_dream_at": None,
                "last_summary": None,
                "last_sessions_processed": 0,
                "last_memories_stored": 0,
            }

        return {
            "agent_name": row[0],
            "last_dream_at": row[1],
            "last_summary": row[2],
            "last_sessions_processed": row[3] or 0,
            "last_memories_stored": row[4] or 0,
        }

    def list_states(self) -> list[dict]:
        """Return dream_state rows for all agents that have ever dreamed."""
        rows = self._db.execute(
            """SELECT agent_name, last_dream_at, last_summary,
                      last_sessions_processed, last_memories_stored
               FROM dream_state ORDER BY last_dream_at DESC""",
        ).fetchall()
        return [
            {
                "agent_name": r[0],
                "last_dream_at": r[1],
                "last_summary": r[2],
                "last_sessions_processed": r[3] or 0,
                "last_memories_stored": r[4] or 0,
            }
            for r in rows
        ]

    # ── Internal ─────────────────────────────────────────────

    def _save_state(self, agent_name: str, summary: str) -> None:
        """Persist dream watermark. Parses summary line for stats if possible."""
        now = time.time()
        sessions_processed = 0
        memories_stored = 0

        # Try to extract stats from "Dream complete. Sessions processed: N | Memories stored: M | ..."
        try:
            if "Sessions processed:" in summary:
                part = summary.split("Sessions processed:")[1].split("|")[0].strip()
                sessions_processed = int(part)
            if "Memories stored:" in summary:
                part = summary.split("Memories stored:")[1].split("|")[0].strip()
                memories_stored = int(part)
        except (ValueError, IndexError):
            pass

        self._db.execute(
            """INSERT INTO dream_state
                   (agent_name, last_dream_at, last_summary,
                    last_sessions_processed, last_memories_stored)
               VALUES (?, ?, ?, ?, ?)
               ON CONFLICT(agent_name) DO UPDATE SET
                   last_dream_at=excluded.last_dream_at,
                   last_summary=excluded.last_summary,
                   last_sessions_processed=excluded.last_sessions_processed,
                   last_memories_stored=excluded.last_memories_stored""",
            (agent_name, now, summary, sessions_processed, memories_stored),
        )
        self._db.commit()
