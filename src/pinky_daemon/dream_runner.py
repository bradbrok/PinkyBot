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

import json
import re
import sqlite3
import sys
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

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
# ToolSearch is required because MCP_CONNECTION_NONBLOCKING=true defers MCP tools.
_DREAM_ALLOWED_TOOLS = [
    "ToolSearch",
    "mcp__pinky-memory__recall",
    "mcp__pinky-memory__reflect",
]

# Max characters of conversation history to inject into the prompt.
# Keeps the dream prompt under a reasonable context budget.
_MAX_HISTORY_CHARS = 200_000


class DreamRunner:
    """Runs nightly dream consolidation sessions for agents.

    Manages the dream_state SQLite table (tracks last run, last summary,
    and aggregate stats). Each dream spawns a one-shot SDKRunner with the
    dream system prompt and restricted tool access.
    """

    def __init__(
        self,
        db_path: str = "data/dream_state.db",
        *,
        history_provider: Callable[[str, float, int, str], list[dict]] | None = None,
    ) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._history_provider = history_provider
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

        dream_start = datetime.now(timezone.utc)
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

        # Post-dream: build memory graph links for new reflections
        self._build_memory_links(agent_name, agent_config, since=dream_start)

        # Post-dream: extract and store user profiles + relationships from dream output
        profile_count = self._extract_user_profiles(summary)
        if profile_count:
            _log(f"dream-runner: '{agent_name}' extracted {profile_count} user profile entries")
        rel_count = self._extract_user_relationships(summary)
        if rel_count:
            _log(f"dream-runner: '{agent_name}' extracted {rel_count} user relationships")

        # Post-dream: extract and create proposed skills
        skills_created = self._extract_proposed_skills(summary, agent_name)
        if skills_created:
            _log(f"dream-runner: '{agent_name}' created {skills_created} skill draft(s)")

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

    # ── User profile extraction ─────────────────────────────

    def _extract_user_profiles(self, dream_output: str) -> int:
        """Parse <user_profiles> JSON from dream output and store entries.

        Returns the number of profile entries stored.
        """
        match = re.search(
            r"<user_profiles>\s*(\[.*?\])\s*</user_profiles>",
            dream_output,
            re.DOTALL,
        )
        if not match:
            return 0

        try:
            profiles_data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError) as e:
            _log(f"dream-runner: failed to parse user_profiles JSON: {e}")
            return 0

        # Lazy import to avoid circular deps
        from pinky_daemon.user_profile_store import ProfileEntry, UserProfileStore

        store = UserProfileStore()
        count = 0

        valid_categories = {
            "identity", "communication", "preferences",
            "work", "personal", "patterns", "relationships",
        }

        for user_block in profiles_data:
            chat_id = user_block.get("chat_id", "")
            if not chat_id or chat_id == "unknown":
                continue

            entries = []
            for entry_data in user_block.get("entries", []):
                cat = entry_data.get("category", "")
                key = entry_data.get("key", "")
                value = entry_data.get("value", "")
                confidence = entry_data.get("confidence", 0.5)

                if not cat or not key or not value:
                    continue
                if cat not in valid_categories:
                    continue
                # Clamp confidence
                confidence = max(0.0, min(1.0, float(confidence)))

                entries.append(ProfileEntry(
                    chat_id=chat_id,
                    category=cat,
                    key=key,
                    value=value,
                    confidence=confidence,
                    source="dream",
                ))

            if entries:
                count += store.bulk_upsert(entries)

        return count

    def _extract_user_relationships(self, dream_output: str) -> int:
        """Parse <user_relationships> JSON from dream output and store.

        Returns the number of relationships stored.
        """
        match = re.search(
            r"<user_relationships>\s*(\[.*?\])\s*</user_relationships>",
            dream_output,
            re.DOTALL,
        )
        if not match:
            return 0

        try:
            rels_data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError) as e:
            _log(f"dream-runner: failed to parse user_relationships JSON: {e}")
            return 0

        from pinky_daemon.user_profile_store import Relationship, UserProfileStore

        store = UserProfileStore()
        rels = []
        for rd in rels_data:
            from_id = rd.get("from_chat_id", "")
            to_name = rd.get("to_display_name", "")
            relation = rd.get("relation", "")
            if not from_id or not to_name or not relation:
                continue
            confidence = max(0.0, min(1.0, float(rd.get("confidence", 0.7))))
            rels.append(Relationship(
                from_chat_id=from_id,
                to_chat_id=rd.get("to_chat_id", ""),
                to_display_name=to_name,
                relation=relation,
                context=rd.get("context", ""),
                confidence=confidence,
            ))

        if rels:
            return store.bulk_add_relationships(rels)
        return 0

    # ── Skill extraction ────────────────────────────────────

    def _extract_proposed_skills(self, dream_output: str, agent_name: str) -> int:
        """Parse <proposed_skills> JSON from dream output and create skill drafts.

        Skills are created via the /skills/from-md API endpoint. Each proposed
        skill becomes a SKILL.md and is assigned to the agent that dreamed it.

        Returns the number of skills successfully created.
        """
        match = re.search(
            r"<proposed_skills>\s*(\[.*?\])\s*</proposed_skills>",
            dream_output,
            re.DOTALL,
        )
        if not match:
            return 0

        try:
            skills_data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError) as e:
            _log(f"dream-runner: failed to parse proposed_skills JSON: {e}")
            return 0

        if not skills_data:
            return 0

        count = 0
        for skill in skills_data:
            name = skill.get("skill_name", "").strip()
            description = skill.get("description", "").strip()
            task_summary = skill.get("task_summary", "").strip()
            source = skill.get("source_pattern", "").strip()

            if not name or not description or not task_summary:
                _log(f"dream-runner: skipping incomplete skill proposal: {name}")
                continue

            # Sanitize name: lowercase, hyphens only, max 30 chars
            name = re.sub(r"[^a-z0-9-]", "-", name.lower())[:30].strip("-")
            if not name:
                continue

            # Build SKILL.md content
            skill_md = (
                f"---\n"
                f"name: {name}\n"
                f"description: {description}\n"
                f"---\n\n"
                f"# {name.replace('-', ' ').title()}\n\n"
                f"## When to Use\n\n{description}\n\n"
                f"## Approach\n\n{task_summary}\n\n"
                f"## Origin\n\n"
                f"Auto-generated during dream cycle from observed workflow patterns.\n"
                f"Source: {source}\n"
            )

            # Create via API
            try:
                import urllib.parse
                import urllib.request

                api_url = "http://127.0.0.1:8888/skills/from-md"
                payload = json.dumps({
                    "content": skill_md,
                    "agent_name": agent_name,
                }).encode()
                req = urllib.request.Request(
                    api_url,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    result = json.loads(resp.read().decode())

                if result.get("name"):
                    _log(
                        f"dream-runner: created skill '{result['name']}' "
                        f"for {agent_name}"
                    )
                    count += 1
                else:
                    _log(f"dream-runner: skill creation returned unexpected: {result}")
            except Exception as e:
                _log(f"dream-runner: failed to create skill '{name}': {e}")

        return count

    # ── Memory graph linking ───────────────────────────────

    def _build_memory_links(
        self,
        agent_name: str,
        agent_config,
        since: datetime,
    ) -> None:
        """Build cosine-similarity links between new and existing reflections.

        Runs after the dream SDK session. Compares reflections created during
        this dream run against all active reflections with embeddings, creating
        links for pairs above LINK_THRESHOLD. Also prunes orphan links.
        """
        try:
            from pinky_memory.store import (
                LINK_THRESHOLD,
                MAX_LINKS_PER_MEMORY,
                ReflectionStore,
            )
        except ImportError:
            _log("dream-runner: pinky_memory not installed, skipping link build")
            return

        work_dir = getattr(agent_config, "working_dir", "") or "."
        db_path = str(Path(work_dir).resolve() / "data" / "memory.db")
        if not Path(db_path).exists():
            _log(f"dream-runner: no memory DB at {db_path}, skipping link build")
            return

        store = ReflectionStore(db_path=db_path)

        # Prune links pointing to inactive/deleted reflections
        pruned = store.prune_orphan_links()
        if pruned:
            _log(f"dream-runner: pruned {pruned} orphan links for '{agent_name}'")

        # Fetch new reflections (created during this dream run)
        new_refs = store.get_active_with_embeddings(since=since)
        if not new_refs:
            _log(f"dream-runner: no new embedded reflections for '{agent_name}', skipping links")
            return

        # Fetch all active reflections with embeddings
        all_refs = store.get_active_with_embeddings()
        if len(all_refs) < 2:
            return

        # Build ID -> embedding index for all reflections
        all_ids = [r.id for r in all_refs]
        all_embeddings = np.array([r.embedding for r in all_refs], dtype=np.float32)

        # Normalize all embeddings for cosine similarity (dot product of unit vectors)
        norms = np.linalg.norm(all_embeddings, axis=1, keepdims=True)
        norms[norms == 0] = 1.0  # avoid division by zero
        all_normed = all_embeddings / norms

        new_ids = {r.id for r in new_refs}
        links_created = 0

        for i, ref_id in enumerate(all_ids):
            if ref_id not in new_ids:
                continue

            # Cosine similarity of this new reflection vs all others
            sims = all_normed @ all_normed[i]

            # Find top candidates above threshold (excluding self)
            candidates = []
            for j, sim in enumerate(sims):
                if all_ids[j] == ref_id:
                    continue
                if sim >= LINK_THRESHOLD:
                    candidates.append((float(sim), all_ids[j]))

            # Sort by similarity descending, cap at MAX_LINKS_PER_MEMORY
            candidates.sort(reverse=True)
            for sim, target_id in candidates[:MAX_LINKS_PER_MEMORY]:
                if store.create_link(ref_id, target_id, sim):
                    links_created += 1

        _log(f"dream-runner: built {links_created} links for '{agent_name}' "
             f"({len(new_refs)} new reflections)")

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
        if not self._history_provider:
            return [], after_ts

        try:
            messages = self._history_provider(agent_name, after_ts, 1000, "")
        except Exception as e:
            _log(f"dream-runner: failed to fetch history for '{agent_name}': {e}")
            return [], after_ts

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
