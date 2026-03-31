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
import urllib.request
import json
from datetime import date, datetime
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

# Restricted tool set for dream agent — memory only, no messaging or history search.
# History is pre-fetched and injected directly into the prompt.
_DREAM_ALLOWED_TOOLS = [
    "mcp__pinky-memory__recall",
    "mcp__pinky-memory__reflect",
]

# API base for fetching conversation history
_API_BASE = "http://localhost:8888"

# Max characters of conversation history to inject into the prompt.
# Keeps the dream prompt under a reasonable context budget.
_MAX_HISTORY_CHARS = 200_000


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
        # Migration: add last_message_ts watermark for tracking processed history
        if "last_message_ts" not in cols:
            self._db.execute(
                "ALTER TABLE dream_state ADD COLUMN last_message_ts REAL DEFAULT 0"
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

        # Fetch unprocessed conversation history
        last_message_ts = self._get_last_message_ts(agent_name)
        history_lines, new_watermark = self._fetch_unprocessed_history(
            agent_name, after_ts=last_message_ts
        )
        _log(f"dream-runner: fetched {len(history_lines)} messages since ts={last_message_ts}")

        if not history_lines:
            summary = "No new conversation history to process."
            self._save_state(agent_name, summary)
            return summary

        # Build system prompt
        today_str = date.today().isoformat()
        last_dream_at = self._get_last_dream_at(agent_name)
        last_dream_str = (
            datetime.fromtimestamp(last_dream_at).isoformat()
            if last_dream_at
            else "never (first dream run)"
        )
        system_prompt = DREAM_SYSTEM_PROMPT.format(
            agent_name=agent_name,
            today=today_str,
            last_dream_at=last_dream_str,
        )

        # Resolve working directory (same as normal streaming session)
        work_dir = "."
        if getattr(agent_config, "working_dir", ""):
            work_dir = str(Path(agent_config.working_dir).resolve())

        # Use dream_model if set, otherwise fall back to agent's main model
        dream_model = getattr(agent_config, "dream_model", "") or ""
        model = dream_model or getattr(agent_config, "model", None) or None

        config = SDKRunnerConfig(
            working_dir=work_dir,
            model=model,
            allowed_tools=_DREAM_ALLOWED_TOOLS,
            permission_mode="bypassPermissions",
            system_prompt=system_prompt,
            max_turns=50,
        )

        runner = SDKRunner(config, agent_name=agent_name)

        # Build the user prompt with conversation history injected
        history_block = "\n".join(history_lines)
        # Truncate if too large
        if len(history_block) > _MAX_HISTORY_CHARS:
            history_block = history_block[-_MAX_HISTORY_CHARS:]
            history_block = "...(truncated older messages)\n" + history_block

        prompt = (
            f"Process the following conversation history for agent {agent_name}. "
            f"There are {len(history_lines)} messages to consolidate.\n\n"
            f"<conversation_history>\n{history_block}\n</conversation_history>\n\n"
            "Work through all phases in your system prompt and end with the report."
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

        self._save_state(agent_name, summary, last_message_ts=new_watermark)
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

    def _get_last_dream_at(self, agent_name: str) -> float | None:
        """Return the timestamp of the last dream run, or None if never dreamed."""
        row = self._db.execute(
            "SELECT last_dream_at FROM dream_state WHERE agent_name=?",
            (agent_name,),
        ).fetchone()
        return row[0] if row and row[0] else None

    def _get_last_message_ts(self, agent_name: str) -> float:
        """Return the last processed message timestamp, or 0 if never dreamed."""
        row = self._db.execute(
            "SELECT last_message_ts FROM dream_state WHERE agent_name=?",
            (agent_name,),
        ).fetchone()
        return (row[0] or 0.0) if row else 0.0

    def _fetch_unprocessed_history(
        self, agent_name: str, after_ts: float = 0.0
    ) -> tuple[list[str], float]:
        """Fetch all conversation messages since the last watermark.

        Returns (formatted_lines, new_watermark_ts).
        """
        try:
            url = f"{_API_BASE}/agents/{agent_name}/chat-history?limit=1000"
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read())
        except Exception as e:
            _log(f"dream-runner: failed to fetch history for '{agent_name}': {e}")
            return [], after_ts

        messages = data.get("messages", [])
        if not messages:
            return [], after_ts

        # Filter to messages after the watermark
        new_msgs = [m for m in messages if (m.get("timestamp") or 0) > after_ts]
        if not new_msgs:
            return [], after_ts

        # Sort chronologically (API returns newest first)
        new_msgs.sort(key=lambda m: m.get("timestamp", 0))

        # Format as readable conversation lines
        lines = []
        new_watermark = after_ts
        for m in new_msgs:
            ts = m.get("timestamp", 0)
            role = m.get("role", "?")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            time_str = datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M") if ts else "?"
            lines.append(f"[{time_str}] [{role}] {content}")
            if ts > new_watermark:
                new_watermark = ts

        return lines, new_watermark

    def _save_state(self, agent_name: str, summary: str, last_message_ts: float = 0.0) -> None:
        """Persist dream watermark and free-form summary."""
        now = time.time()
        self._db.execute(
            """INSERT INTO dream_state
                   (agent_name, last_dream_at, last_summary, last_message_ts)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(agent_name) DO UPDATE SET
                   last_dream_at=excluded.last_dream_at,
                   last_summary=excluded.last_summary,
                   last_message_ts=excluded.last_message_ts""",
            (agent_name, now, summary, last_message_ts),
        )
        self._db.commit()
