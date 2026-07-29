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

import asyncio
import json
import os
import re
import sqlite3
import sys
import threading
import time
from collections.abc import Callable
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np

from pinky_daemon.auth import (
    build_internal_auth_headers,
    resolve_request_signing_secret,
)
from pinky_daemon.dream_prompt import DREAM_SYSTEM_PROMPT
from pinky_daemon.sdk_runner import SDKRunner, SDKRunnerConfig
from pinky_daemon.tmux_dream_runner import TmuxDreamConfig, TmuxDreamRunner


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _kg_proactive_enabled() -> bool:
    """Master switch for KG proactive surfacing (``PINKY_KG_PROACTIVE``).

    Default OFF. Gates BOTH the nightly compute and the morning delivery, so
    flipping it off fully suppresses surfacing — including any digest that was
    already computed while it was on.
    """
    return os.environ.get("PINKY_KG_PROACTIVE", "0").strip() == "1"


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

# Bare alias only: dated Haiku 4.5 snapshots return 404 from Anthropic.
_KG_EXTRACTION_MODEL_DEFAULT = "claude-haiku-4-5"


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
        owner_provider: Callable[[], dict] | None = None,
        setting_provider: Callable[[str], str] | None = None,
        signing_key_provider: Callable[[str], str | None] | None = None,
    ) -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db_path = db_path
        self._thread_local = threading.local()
        self._history_provider = history_provider
        # Optional () -> owner-profile dict, used to derive high-value entity
        # names for KG proactive-surfacing materiality. May be None.
        self._owner_provider = owner_provider
        # Optional (key) -> system_settings value resolver. The daemon process
        # env does NOT carry ANTHROPIC_API_KEY (it lives in system_settings), so
        # KG extraction's LLM caller must resolve the key through this. May be None.
        self._setting_provider = setting_provider
        # Optional agent_name -> per-agent signing key resolver for daemon-internal
        # API calls. Isolated agents cannot authenticate with the fleet-wide secret.
        self._signing_key_provider = signing_key_provider
        self._init_tables()

    @property
    def _db(self) -> sqlite3.Connection:
        """Return the calling thread's connection, creating it on first use."""
        connection = getattr(self._thread_local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(self._db_path)
            connection.execute("PRAGMA journal_mode=WAL")
            self._thread_local.connection = connection
        return connection

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS dream_state (
                agent_name TEXT PRIMARY KEY,
                last_dream_at REAL,
                last_summary TEXT,
                sessions_processed INT DEFAULT 0,
                memories_stored INT DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS kg_surfaced (
                agent_name TEXT NOT NULL,
                fingerprint TEXT NOT NULL,
                surfaced_at REAL,
                PRIMARY KEY (agent_name, fingerprint)
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
        # Migration: add KG proactive-surfacing columns (#682 PR2)
        if "kg_insights" not in cols:
            self._db.execute(
                "ALTER TABLE dream_state ADD COLUMN kg_insights TEXT"
            )
            self._db.commit()
        if "kg_insights_notified_at" not in cols:
            self._db.execute(
                "ALTER TABLE dream_state ADD COLUMN kg_insights_notified_at REAL"
            )
            self._db.commit()

    def _resolve_dream_transport(self) -> str:
        """Which transport runs the dream: ``sdk`` (default) or ``tmux`` (#707).

        Resolution: ``PINKY_DREAM_TRANSPORT`` in system_settings (via the
        injected setting_provider — daemon env does NOT carry settings, same
        trap as the API key / #685), then process env, then ``sdk``.
        """
        import os

        val = ""
        if self._setting_provider:
            try:
                val = (self._setting_provider("PINKY_DREAM_TRANSPORT") or "").strip()
            except Exception:
                val = ""
        if not val:
            val = os.environ.get("PINKY_DREAM_TRANSPORT", "").strip()
        val = val.lower()
        if val not in ("sdk", "tmux"):
            if val:
                _log(f"dream-runner: unknown PINKY_DREAM_TRANSPORT={val!r} — using sdk")
            return "sdk"
        return val

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
            # Preserve the existing watermark; saving the default 0.0 would
            # make the next dream re-fetch and re-consolidate all history.
            self._save_state(agent_name, summary, last_message_ts=last_message_ts)
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

        transport = self._resolve_dream_transport()
        if transport == "tmux":
            # #707: interactive tmux session on Max-account billing. The
            # system prompt travels inside the prompt file, not as an option.
            runner = TmuxDreamRunner(
                TmuxDreamConfig(
                    working_dir=work_dir,
                    model=model,
                    system_prompt=system_prompt,
                    # SDK allowlist + Read/Write for the file-passing protocol
                    allowed_tools=["Read", "Write", *_DREAM_ALLOWED_TOOLS],
                ),
                agent_name=agent_name,
            )
            _log(f"dream-runner: transport=tmux for '{agent_name}' "
                 f"(session pinky-dream-{agent_name})")
        else:
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
        run_failed = not summary or result.exit_code != 0
        if not summary:
            summary = f"Dream run failed or produced no output (exit={result.exit_code})"
            if result.error:
                summary += f": {result.error}"

        _log(f"dream-runner: '{agent_name}' dream complete in {elapsed:.1f}s — {summary[:120]}")

        # Only advance the watermark on success: a failed run must leave this
        # cycle's history unprocessed so the next dream re-fetches it.
        self._save_state(
            agent_name,
            summary,
            last_message_ts=last_message_ts if run_failed else new_watermark,
        )

        # The post-dream pipeline below is synchronous (blocking urllib calls
        # with retries, numpy over all embeddings). It runs on the daemon's
        # single shared event loop, so each step is offloaded to a worker
        # thread; otherwise the whole daemon (API, pollers, broker, scheduler)
        # freezes for the duration of the pipeline.

        # Post-dream: build memory graph links for new reflections
        await asyncio.to_thread(
            self._build_memory_links, agent_name, agent_config, since=dream_start
        )

        # Post-dream: extract and store user profiles + relationships from dream output
        profile_count = await asyncio.to_thread(self._extract_user_profiles, summary)
        if profile_count:
            _log(f"dream-runner: '{agent_name}' extracted {profile_count} user profile entries")
        rel_count = await asyncio.to_thread(self._extract_user_relationships, summary)
        if rel_count:
            _log(f"dream-runner: '{agent_name}' extracted {rel_count} user relationships")

        # Post-dream: extract and create proposed skills. Must run off-loop:
        # it POSTs to the daemon's own API, which can only be served while the
        # event loop is free.
        skills_created = await asyncio.to_thread(
            self._extract_proposed_skills, summary, agent_name
        )
        if skills_created:
            _log(f"dream-runner: '{agent_name}' created {skills_created} skill draft(s)")

        # Post-dream: extract KG triples from dream output (inline extraction)
        kg_dream_count = await asyncio.to_thread(
            self._extract_kg_from_dream_output, summary, agent_config
        )
        if kg_dream_count:
            _log(f"dream-runner: '{agent_name}' extracted {kg_dream_count} KG triples from dream")

        # Post-dream: extract KG triples from new reflections (per-reflection LLM pass)
        kg_count = await asyncio.to_thread(self._extract_kg_triples, agent_name, agent_config)
        if kg_count:
            _log(f"dream-runner: '{agent_name}' extracted {kg_count} KG triples")

        # Post-dream: surface MATERIAL, not-yet-seen KG contradictions/changes
        # into the agent's morning wake context. Flag-gated (PINKY_KG_PROACTIVE,
        # OFF by default), read-only over the KG, deduped + top-5 capped. It
        # never auto-messages the owner — the agent reviews the digest on wake
        # and decides. ``last_dream_at`` here is the PRIOR dream time (captured
        # before _save_state above), so changes are scoped to this cycle.
        kg_digest = await asyncio.to_thread(
            self._surface_kg_insights, agent_name, agent_config, since_ts=last_dream_at
        )
        if kg_digest:
            _log(
                f"dream-runner: '{agent_name}' KG insights digest ready "
                f"({len(kg_digest)} chars)"
            )

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

    # ── KG proactive surfacing (#682 PR2) ───────────────────────

    def get_kg_insights(self, agent_name: str) -> str | None:
        """Return the pending KG-insights digest once per dream cycle.

        Mirrors :meth:`get_morning_summary`: delivers the digest only when a
        dream ran within the morning window and it has not yet been delivered
        for this cycle (``kg_insights_notified_at`` is NULL or predates
        ``last_dream_at``). Returns None otherwise.

        Also gated by the ``PINKY_KG_PROACTIVE`` master switch: if surfacing is
        disabled at wake time, no digest is delivered even if one was computed
        while it was enabled.
        """
        if not _kg_proactive_enabled():
            return None

        row = self._db.execute(
            "SELECT last_dream_at, kg_insights, kg_insights_notified_at"
            " FROM dream_state WHERE agent_name=?",
            (agent_name,),
        ).fetchone()
        if not row or not row[0] or not row[1]:
            return None

        last_dream_at, digest, notified_at = row
        if time.time() - last_dream_at > _MORNING_WINDOW_S:
            return None
        if notified_at is not None and notified_at >= last_dream_at:
            return None

        self._db.execute(
            "UPDATE dream_state SET kg_insights_notified_at=? WHERE agent_name=?",
            (time.time(), agent_name),
        )
        self._db.commit()
        return digest

    def _high_value_entities(self) -> set[str]:
        """Case-folded owner name aliases used to boost insight materiality.

        Derived from the owner profile (if an ``owner_provider`` was wired);
        empty set otherwise. With an empty set, materiality rests entirely on
        the high-value predicate domains, which is conservative and correct.
        """
        if self._owner_provider is None:
            return set()
        try:
            owner = self._owner_provider() or {}
        except Exception:  # noqa: BLE001 — never let surfacing break the dream
            return set()
        names: set[str] = set()
        full = str(owner.get("name", "") or "").strip()
        if full:
            names.add(full)
            names.update(full.split())
        return {n.strip().casefold() for n in names if n.strip()}

    def _load_surfaced_fingerprints(self, agent_name: str) -> set[str]:
        rows = self._db.execute(
            "SELECT fingerprint FROM kg_surfaced WHERE agent_name=?",
            (agent_name,),
        ).fetchall()
        return {r[0] for r in rows}

    def _record_surfaced_fingerprints(
        self, agent_name: str, fingerprints: list[str]
    ) -> None:
        if not fingerprints:
            return
        now = time.time()
        self._db.executemany(
            "INSERT OR IGNORE INTO kg_surfaced(agent_name, fingerprint, surfaced_at)"
            " VALUES (?,?,?)",
            [(agent_name, fp, now) for fp in fingerprints],
        )
        self._db.commit()

    def _surface_kg_insights(
        self, agent_name: str, agent_config, *, since_ts: float | None = None
    ) -> str:
        """Compute a digest of material, unseen KG contradictions/changes.

        Flag-gated by ``PINKY_KG_PROACTIVE`` (default OFF). Reads the agent's
        KG (never writes), applies the materiality policy in
        :mod:`pinky_memory.kg_reason`, suppresses repeats by fingerprint, caps
        the volume, and stores the digest for once-per-cycle delivery via
        :meth:`get_kg_insights`. Returns the digest text ("" if nothing).

        Any failure is swallowed (logged) so surfacing can never break a dream.
        """
        if not _kg_proactive_enabled():
            return ""

        try:
            from pinky_memory import kg_reason
            from pinky_memory.store import ReflectionStore
        except ImportError:
            _log("dream-runner: kg_reason/store unavailable — skipping surfacing")
            return ""

        work_dir = getattr(agent_config, "working_dir", "") or "."
        db_path = str(Path(work_dir).resolve() / "data" / "memory.db")
        if not Path(db_path).exists():
            return ""

        try:
            store = ReflectionStore(db_path=db_path)
            contradictions = store.kg_find_contradictions()
            if since_ts is None:
                since_ts = time.time() - 48 * 3600
            changes = store.kg_what_changed(since=str(since_ts))
        except Exception as e:  # noqa: BLE001 — surfacing must never crash dream
            _log(f"dream-runner: KG surfacing query failed for '{agent_name}': {e}")
            return ""

        high_value = self._high_value_entities()
        seen = self._load_surfaced_fingerprints(agent_name)
        selected = kg_reason.select_insights(
            contradictions, changes, seen, high_value, cap=5
        )
        digest = kg_reason.format_digest(selected)

        if not digest:
            # Nothing new/material this cycle — clear any stale pending digest.
            self._db.execute(
                "UPDATE dream_state SET kg_insights=NULL WHERE agent_name=?",
                (agent_name,),
            )
            self._db.commit()
            return ""

        self._record_surfaced_fingerprints(agent_name, selected["fingerprints"])
        self._db.execute(
            "UPDATE dream_state SET kg_insights=?, kg_insights_notified_at=NULL"
            " WHERE agent_name=?",
            (digest, agent_name),
        )
        self._db.commit()
        _log(
            f"dream-runner: '{agent_name}' surfaced "
            f"{len(selected['contradictions'])} contradiction(s) + "
            f"{len(selected['changes'])} change(s)"
        )
        return digest

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

    # ── KG extraction from dream output ──────────────────────

    def _extract_kg_from_dream_output(
        self,
        dream_output: str,
        agent_config,
    ) -> int:
        """Parse <knowledge_graph> JSON from dream output and insert triples.

        This handles triples the dream agent extracted inline during
        conversation processing. Separate from per-reflection extraction.

        Returns the number of triples added.
        """
        match = re.search(
            r"<knowledge_graph>\s*(\[.*?\])\s*</knowledge_graph>",
            dream_output,
            re.DOTALL,
        )
        if not match:
            return 0

        try:
            triples_data = json.loads(match.group(1))
        except (json.JSONDecodeError, ValueError) as e:
            _log(f"dream-runner: failed to parse knowledge_graph JSON: {e}")
            return 0

        if not isinstance(triples_data, list) or not triples_data:
            return 0

        try:
            from pinky_memory.kg_extractor import (
                _SUPERSEDE_CONFIDENCE,
                ExtractedTriple,
                ExtractionResult,
                KGExtractor,
                get_predicate_type,
                normalize_entity_name,
                normalize_predicate,
                validate_triple,
            )
            from pinky_memory.store import ReflectionStore
        except ImportError:
            _log("dream-runner: kg_extractor not available")
            return 0

        work_dir = getattr(agent_config, "working_dir", "") or "."
        db_path = str(Path(work_dir).resolve() / "data" / "memory.db")
        if not Path(db_path).exists():
            return 0

        store = ReflectionStore(db_path=db_path)
        extractor = KGExtractor(store=store)
        count = 0

        for item in triples_data:
            if not isinstance(item, dict):
                continue

            try:
                t = ExtractedTriple(
                    subject=normalize_entity_name(str(item.get("subject", ""))),
                    predicate=normalize_predicate(str(item.get("predicate", ""))),
                    object=normalize_entity_name(str(item.get("object", ""))),
                    subject_type=str(item.get("subject_type", "unknown")).lower(),
                    object_type=str(item.get("object_type", "unknown")).lower(),
                    confidence=float(item.get("confidence", 0.8)),
                    valid_from=str(item.get("valid_from", "")),
                    temporal_granularity=str(
                        item.get("temporal_granularity", "none")
                    ).lower(),
                    evidence_span=str(item.get("evidence_span", ""))[:200],
                    is_negation=bool(item.get("is_negation", False)),
                )
            except (ValueError, TypeError) as e:
                _log(f"dream-runner: skipping malformed KG triple: {e}")
                continue

            issues = validate_triple(t)
            if issues:
                continue

            # Handle negations
            if t.is_negation:
                store.kg_invalidate(t.subject, t.predicate, t.object,
                                    valid_to=t.valid_from or "")
                count += 1
                continue

            # Conflict resolution for functional predicates
            pred_type = get_predicate_type(t.predicate)
            if pred_type == "functional" and t.confidence >= _SUPERSEDE_CONFIDENCE:
                result = ExtractionResult(reflection_id="dream")
                extractor._resolve_functional_conflict(t, result)

            try:
                added = store.kg_add(
                    subject=t.subject,
                    predicate=t.predicate,
                    obj=t.object,
                    valid_from=t.valid_from,
                    subject_type=t.subject_type,
                    object_type=t.object_type,
                    confidence=t.confidence,
                    extraction_method="auto_dream",
                    status="active",
                    temporal_granularity=t.temporal_granularity,
                    evidence_span=t.evidence_span,
                )
                # Write-time guard (#153) may reject a pure-ID ephemeral; don't
                # count a rejected triple as consolidated.
                if added.get("rejected"):
                    _log(
                        f"dream-runner: KG insert skipped (ephemeral): "
                        f"({t.subject}) --[{t.predicate}]--> ({t.object})"
                    )
                else:
                    count += 1
            except Exception as e:
                _log(
                    f"dream-runner: KG insert failed for "
                    f"({t.subject}, {t.predicate}, {t.object}): {e}"
                )

        return count

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

                request_path = "/skills/from-md"
                api_url = f"http://127.0.0.1:8888{request_path}"
                payload = json.dumps({
                    "content": skill_md,
                    "agent_name": agent_name,
                }).encode()
                headers = {"Content-Type": "application/json"}
                headers.update(build_internal_auth_headers(
                    resolve_request_signing_secret(
                        agent_name, self._signing_key_provider
                    ),
                    agent_name=agent_name,
                    method="POST",
                    path=request_path,
                ))
                req = urllib.request.Request(
                    api_url,
                    data=payload,
                    headers=headers,
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

    # ── KG triple extraction ─────────────────────────────

    def _extract_kg_triples(
        self,
        agent_name: str,
        agent_config,
    ) -> int:
        """Extract KG triples from unprocessed reflections via LLM.

        Uses the KGExtractor pipeline: LLM extraction → deterministic validation
        → predicate-aware conflict resolution → insert.

        Returns total number of triples added.
        """
        try:
            from pinky_memory.kg_extractor import KGExtractor
            from pinky_memory.store import ReflectionStore
        except ImportError:
            _log("dream-runner: pinky_memory.kg_extractor not available, skipping KG extraction")
            return 0

        work_dir = getattr(agent_config, "working_dir", "") or "."
        db_path = str(Path(work_dir).resolve() / "data" / "memory.db")
        if not Path(db_path).exists():
            _log(f"dream-runner: no memory DB at {db_path}, skipping KG extraction")
            return 0

        store = ReflectionStore(db_path=db_path)

        # Get unprocessed reflections
        unprocessed = store.kg_get_unprocessed_reflections(
            extractor_version=KGExtractor.EXTRACTOR_VERSION,
            limit=50,
        )
        if not unprocessed:
            _log(f"dream-runner: no unprocessed reflections for KG extraction ({agent_name})")
            return 0

        _log(
            f"dream-runner: extracting KG triples from {len(unprocessed)} "
            f"reflections for '{agent_name}'"
        )

        # Build LLM caller using a lightweight SDK run
        llm_caller = self._build_kg_llm_caller(agent_config)
        if llm_caller is None:
            _log("dream-runner: could not build LLM caller for KG extraction")
            return 0

        extractor = KGExtractor(store=store, llm_caller=llm_caller)
        total_added = 0

        for ref in unprocessed:
            try:
                result = extractor.extract_from_reflection(
                    reflection_id=ref["id"],
                    content=ref["content"],
                    context=ref.get("context", ""),
                    project=ref.get("project", ""),
                )

                # Log the extraction (idempotent per reflection + version)
                store.kg_log_extraction(
                    reflection_id=ref["id"],
                    extractor_version=KGExtractor.EXTRACTOR_VERSION,
                    triples_extracted=result.total_added,
                    triples_superseded=result.total_superseded,
                    errors="; ".join(result.errors) if result.errors else "",
                )

                total_added += result.total_added

                if result.total_added or result.total_superseded:
                    _log(
                        f"dream-runner: KG extraction for {ref['id'][:8]}: "
                        f"+{result.total_added} triples, "
                        f"~{result.total_superseded} superseded"
                    )
            except Exception as e:
                _log(f"dream-runner: KG extraction failed for {ref['id'][:8]}: {e}")
                store.kg_log_extraction(
                    reflection_id=ref["id"],
                    extractor_version=KGExtractor.EXTRACTOR_VERSION,
                    errors=str(e),
                )

        return total_added

    def _build_kg_llm_caller(self, agent_config) -> "Callable[[str], str] | None":
        """Build a lightweight LLM caller for KG extraction.

        Uses urllib to call the Anthropic API directly — avoids spawning
        a full SDK session for each reflection's extraction prompt.
        """
        import os
        import socket
        import time
        import urllib.error
        import urllib.request

        # Resolve the API key the same way the rest of the daemon does
        # (cf. voice_routes.py): per-agent provider_key override → system_settings
        # → process env. The daemon process env does NOT carry ANTHROPIC_API_KEY
        # (it lives in system_settings), so an os.environ-only read silently
        # returned None here and skipped KG extraction on every dream run.
        api_key = (
            getattr(agent_config, "provider_key", "")
            or (self._setting_provider("ANTHROPIC_API_KEY") if self._setting_provider else "")
            or os.environ.get("ANTHROPIC_API_KEY", "")
        )
        if not api_key:
            return None

        # Resolve the extraction model from durable settings first, then the
        # process environment, with a cheap bare-alias default. Keep provider
        # failures non-fatal so an unavailable settings DB cannot stop dreams.
        model = ""
        if self._setting_provider:
            try:
                model = (self._setting_provider("KG_EXTRACTION_MODEL") or "").strip()
            except Exception:
                model = ""
        if not model:
            model = os.environ.get("KG_EXTRACTION_MODEL", "").strip()
        if not model:
            model = _KG_EXTRACTION_MODEL_DEFAULT

        # 30s was too short once #686 raised max_tokens 2048->8192: the heaviest
        # reflections now generate longer responses that blew past it, losing
        # ~34% of a rotation to "read operation timed out" (#172). 90s
        # comfortably covers a heavy single reflection's output; transient
        # failures are retried below with a short backoff.
        read_timeout = 90
        max_attempts = 3  # 1 initial try + 2 retries

        def call_llm(prompt: str) -> str:
            # 2048 was too low: reflections with many triples overflowed it, the
            # JSON array was truncated mid-output, and the whole reflection's
            # triples were dropped on parse. 8192 leaves ample room for a single
            # reflection's triples; parse_llm_response also salvages partial
            # arrays as defense-in-depth if a response still overflows.
            body = json.dumps({
                "model": model,
                "max_tokens": 8192,
                "messages": [{"role": "user", "content": prompt}],
            })
            req = urllib.request.Request(
                "https://api.anthropic.com/v1/messages",
                data=body.encode(),
                headers={
                    "Content-Type": "application/json",
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                },
            )
            for attempt in range(max_attempts):
                try:
                    with urllib.request.urlopen(req, timeout=read_timeout) as resp:
                        data = json.loads(resp.read().decode())
                        # Extract text from content blocks
                        for block in data.get("content", []):
                            if block.get("type") == "text":
                                return block["text"]
                        return ""
                except urllib.error.HTTPError as e:
                    # Retry transient server-side / rate-limit statuses; fail
                    # fast on 4xx client errors (bad key, malformed request).
                    if e.code in (429, 500, 502, 503, 529) and attempt < max_attempts - 1:
                        time.sleep(2 ** attempt)
                        continue
                    _log(f"dream-runner: KG LLM call failed (HTTP {e.code}): {e}")
                    raise
                except (TimeoutError, socket.timeout, urllib.error.URLError) as e:
                    # Network-level transient: read timeout, connection reset.
                    # Exactly the failures #172 saw — retry with backoff.
                    if attempt < max_attempts - 1:
                        time.sleep(2 ** attempt)
                        continue
                    _log(
                        f"dream-runner: KG LLM call failed after "
                        f"{max_attempts} attempts: {e}"
                    )
                    raise
                except Exception as e:
                    _log(f"dream-runner: KG LLM call failed: {e}")
                    raise
            return ""  # unreachable: the loop always returns or raises

        return call_llm

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

    def close(self) -> None:
        """Close the calling thread's connection, if it has opened one.

        Connections opened by other threads belong to their thread-local state
        and are released when those threads exit.
        """
        connection = getattr(self._thread_local, "connection", None)
        if connection is not None:
            connection.close()
            del self._thread_local.connection
