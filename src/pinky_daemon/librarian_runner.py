"""KB Librarian Runner — periodic wiki curation from raw sources.

Spawns a one-shot SDK session that reads new raw KB sources and
generates/updates wiki pages. Modeled on DreamRunner but focused
on knowledge organization rather than memory consolidation.

Usage:
    runner = LibrarianRunner(kb_store, db_path="data/librarian_state.db")
    if runner.has_new_sources():
        stats = await runner.run("barsik", agent_config)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from datetime import date, datetime, timezone
from pathlib import Path

from pinky_daemon.kb_store import KBStore
from pinky_daemon.librarian_prompt import LIBRARIAN_SYSTEM_PROMPT
from pinky_daemon.sdk_runner import SDKRunner, SDKRunnerConfig


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# Cooldown between runs (20 hours — same as dreams, prevents double-fire)
_LIBRARIAN_COOLDOWN_S = 20 * 3600

# Max characters of source content to inject into the prompt
_MAX_SOURCE_CHARS = 150_000

# Tools the librarian SDK session can use (MCP tools from pinky-self + file read)
_LIBRARIAN_ALLOWED_TOOLS = [
    "Read", "Glob", "Grep",
    "mcp__pinky-self__kb_search",
    "mcp__pinky-self__kb_get_wiki",
    "mcp__pinky-self__kb_stats",
    "mcp__pinky-self__kb_save_wiki",
    "mcp__pinky-self__kb_delete_wiki",
]


class LibrarianRunner:
    """Runs periodic KB curation sessions.

    Manages the librarian_state SQLite table and spawns one-shot SDK
    sessions to process new raw sources into wiki pages.
    """

    def __init__(
        self,
        kb_store: KBStore,
        db_path: str | Path = "data/librarian_state.db",
    ) -> None:
        self._kb = kb_store
        self._db_path = Path(db_path)
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self._db_path))
        conn.execute("PRAGMA journal_mode=WAL")
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        conn = self._conn()
        try:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS librarian_state (
                    agent_name TEXT PRIMARY KEY,
                    last_run_at TEXT,
                    last_sources_processed INTEGER DEFAULT 0,
                    last_pages_created INTEGER DEFAULT 0,
                    last_pages_updated INTEGER DEFAULT 0,
                    last_summary TEXT DEFAULT ''
                );

                CREATE TABLE IF NOT EXISTS librarian_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    agent_name TEXT NOT NULL,
                    run_at TEXT NOT NULL,
                    sources_processed TEXT DEFAULT '[]',
                    pages_created TEXT DEFAULT '[]',
                    pages_updated TEXT DEFAULT '[]',
                    duration_s REAL DEFAULT 0,
                    summary TEXT DEFAULT ''
                );
            """)
            conn.commit()
        finally:
            conn.close()

    def _get_last_run_at(self, agent_name: str) -> str | None:
        """Get the ISO timestamp of the last successful run for an agent."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT last_run_at FROM librarian_state WHERE agent_name = ?",
                (agent_name,),
            ).fetchone()
            return row["last_run_at"] if row and row["last_run_at"] else None
        finally:
            conn.close()

    def has_new_sources(self, agent_name: str = "") -> bool:
        """Check if there are raw sources filed since the last run."""
        last_run = self._get_last_run_at(agent_name or "_default")
        sources = self._kb.list_raw(since=last_run, limit=1)
        return len(sources) > 0

    def should_run(self, agent_name: str = "") -> bool:
        """Check if the librarian should run (has new sources + past cooldown)."""
        name = agent_name or "_default"
        if not self.has_new_sources(name):
            return False

        last_run = self._get_last_run_at(name)
        if last_run:
            try:
                last_dt = datetime.fromisoformat(last_run)
                elapsed = (datetime.now(timezone.utc) - last_dt).total_seconds()
                if elapsed < _LIBRARIAN_COOLDOWN_S:
                    return False
            except (ValueError, TypeError):
                pass

        return True

    async def run(self, agent_name: str, agent_config) -> dict:
        """Run the librarian — process new sources and update wiki.

        Args:
            agent_name: Name of the agent that owns the KB.
            agent_config: Agent dataclass with .working_dir, .model, etc.

        Returns:
            Stats dict with sources_processed, pages_created, pages_updated, summary.
        """
        _log(f"librarian: starting KB curation for '{agent_name}'")
        start = time.time()

        last_run = self._get_last_run_at(agent_name)
        new_sources = self._kb.list_raw(since=last_run, limit=100)

        if not new_sources:
            _log("librarian: no new sources — skipping")
            return {"sources_processed": 0, "skipped": True}

        _log(f"librarian: found {len(new_sources)} new source(s) since {last_run or 'beginning'}")

        # Build source content block for the prompt
        source_blocks = []
        total_chars = 0
        for src in new_sources:
            content = self._kb.get_raw_content(src.id)
            if not content:
                continue
            block = (
                f"### Source: {src.title} (ID: {src.id})\n"
                f"Type: {src.source_type} | Filed: {src.filed_at} | "
                f"Tags: {', '.join(src.tags)}\n\n"
                f"{content}\n\n---\n"
            )
            if total_chars + len(block) > _MAX_SOURCE_CHARS:
                _log(f"librarian: truncating sources at {len(source_blocks)} "
                     f"(budget: {_MAX_SOURCE_CHARS})")
                break
            source_blocks.append(block)
            total_chars += len(block)

        # Build wiki manifest (slug + title + sources — not full content)
        wiki_pages = self._kb.list_wiki(limit=200)
        wiki_manifest = "\n".join(
            f"- **{p.slug}**: {p.title} (sources: {', '.join(p.sources)})"
            for p in wiki_pages
        ) or "(No existing wiki pages)"

        # Build system prompt
        today_str = date.today().isoformat()
        last_run_str = last_run or "never (first run)"
        system_prompt = LIBRARIAN_SYSTEM_PROMPT.format(
            agent_name=agent_name,
            today=today_str,
            last_run_at=last_run_str,
        )

        # Resolve working directory
        work_dir = "."
        if getattr(agent_config, "working_dir", ""):
            work_dir = str(Path(agent_config.working_dir).resolve())

        config = SDKRunnerConfig(
            working_dir=work_dir,
            model="sonnet",  # Cost-efficient for curation
            allowed_tools=_LIBRARIAN_ALLOWED_TOOLS,
            permission_mode="bypassPermissions",
            system_prompt=system_prompt,
            max_turns=50,
        )

        runner = SDKRunner(config, agent_name=f"{agent_name}-librarian")

        # Build the user prompt
        source_content = "\n".join(source_blocks)
        prompt = (
            f"Process {len(new_sources)} new raw source(s) filed since {last_run_str}.\n\n"
            f"## New Raw Sources\n\n{source_content}\n\n"
            f"## Existing Wiki Pages\n\n{wiki_manifest}\n\n"
            "Read the sources, check existing wiki pages as needed, "
            "and create/update wiki pages to organize this knowledge."
        )

        result = await runner.run(prompt)
        elapsed = time.time() - start

        summary = result.output.strip() if result.output else ""
        if not summary:
            summary = f"Librarian run failed (exit={result.exit_code})"
            if result.error:
                summary += f": {result.error}"

        _log(f"librarian: curation complete in {elapsed:.1f}s — {summary[:200]}")

        # Parse stats from summary (best-effort)
        source_ids = [s.id for s in new_sources[:len(source_blocks)]]
        stats = self._parse_stats(summary, source_ids)
        stats["duration_s"] = round(elapsed, 1)
        stats["summary"] = summary

        # Save state
        self._save_state(agent_name, stats)
        self._save_run_log(agent_name, stats)

        return stats

    def _parse_stats(self, summary: str, source_ids: list[str]) -> dict:
        """Best-effort parse of stats from the librarian's summary output."""
        return {
            "sources_processed": len(source_ids),
            "source_ids": source_ids,
            "pages_created": [],  # TODO: parse from summary
            "pages_updated": [],  # TODO: parse from summary
        }

    def _save_state(self, agent_name: str, stats: dict) -> None:
        """Update the persistent state after a run."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO librarian_state
                    (agent_name, last_run_at, last_sources_processed,
                     last_pages_created, last_pages_updated, last_summary)
                   VALUES (?, ?, ?, ?, ?, ?)
                   ON CONFLICT(agent_name) DO UPDATE SET
                       last_run_at = excluded.last_run_at,
                       last_sources_processed = excluded.last_sources_processed,
                       last_pages_created = excluded.last_pages_created,
                       last_pages_updated = excluded.last_pages_updated,
                       last_summary = excluded.last_summary
                """,
                (
                    agent_name,
                    now,
                    stats.get("sources_processed", 0),
                    len(stats.get("pages_created", [])),
                    len(stats.get("pages_updated", [])),
                    stats.get("summary", ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def _save_run_log(self, agent_name: str, stats: dict) -> None:
        """Log this run for auditability."""
        now = datetime.now(timezone.utc).isoformat()
        conn = self._conn()
        try:
            conn.execute(
                """INSERT INTO librarian_runs
                    (agent_name, run_at, sources_processed, pages_created,
                     pages_updated, duration_s, summary)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    agent_name,
                    now,
                    json.dumps(stats.get("source_ids", [])),
                    json.dumps(stats.get("pages_created", [])),
                    json.dumps(stats.get("pages_updated", [])),
                    stats.get("duration_s", 0),
                    stats.get("summary", ""),
                ),
            )
            conn.commit()
        finally:
            conn.close()

    def get_state(self, agent_name: str = "") -> dict:
        """Get the current librarian state for an agent."""
        conn = self._conn()
        try:
            row = conn.execute(
                "SELECT * FROM librarian_state WHERE agent_name = ?",
                (agent_name or "_default",),
            ).fetchone()
            if not row:
                return {}
            return {
                "agent_name": row["agent_name"],
                "last_run_at": row["last_run_at"],
                "last_sources_processed": row["last_sources_processed"],
                "last_pages_created": row["last_pages_created"],
                "last_pages_updated": row["last_pages_updated"],
                "last_summary": row["last_summary"],
            }
        finally:
            conn.close()

    def get_run_history(self, agent_name: str = "", limit: int = 10) -> list[dict]:
        """Get recent run history for debugging."""
        conn = self._conn()
        try:
            if agent_name:
                rows = conn.execute(
                    "SELECT * FROM librarian_runs WHERE agent_name = ? "
                    "ORDER BY run_at DESC LIMIT ?",
                    (agent_name, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM librarian_runs ORDER BY run_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [
                {
                    "run_at": r["run_at"],
                    "sources_processed": json.loads(r["sources_processed"]),
                    "pages_created": json.loads(r["pages_created"]),
                    "pages_updated": json.loads(r["pages_updated"]),
                    "duration_s": r["duration_s"],
                    "summary": r["summary"],
                }
                for r in rows
            ]
        finally:
            conn.close()
