#!/usr/bin/env python3
"""Backfill Codex CLI agents from legacy provider_url runtime selection.

Dry-run by default. Pass --apply to update matching rows.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any

DEFAULT_DB_PATH = Path("data/pinky_agents.db")

MATCH_QUERY = """
    SELECT name, runtime, transport, provider_url, provider_model, provider_ref
    FROM agents
    WHERE provider_url = 'codex_cli'
      AND runtime = 'claude_sdk'
    ORDER BY name
"""

UPDATE_QUERY = """
    UPDATE agents
    SET runtime = 'codex_cli',
        transport = 'sdk',
        updated_at = ?
    WHERE provider_url = 'codex_cli'
      AND runtime = 'claude_sdk'
"""


def _connect(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        raise FileNotFoundError(f"agents database not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    conn.row_factory = sqlite3.Row
    return conn


def _require_agents_columns(conn: sqlite3.Connection) -> None:
    try:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(agents)").fetchall()}
    except sqlite3.Error as exc:
        raise RuntimeError(f"failed to inspect agents table: {exc}") from exc

    required = {"name", "runtime", "transport", "provider_url", "updated_at"}
    missing = sorted(required - columns)
    if missing:
        raise RuntimeError(f"agents table is missing required column(s): {', '.join(missing)}")


def find_codex_runtime_mismatches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """Return agents that should run codex_cli but are still marked claude_sdk."""
    _require_agents_columns(conn)
    return [dict(row) for row in conn.execute(MATCH_QUERY).fetchall()]


def apply_codex_runtime_backfill(conn: sqlite3.Connection, *, now: float | None = None) -> int:
    """Update mismatched Codex CLI rows and force their transport to sdk."""
    _require_agents_columns(conn)
    cursor = conn.execute(UPDATE_QUERY, (time.time() if now is None else now,))
    conn.commit()
    return int(cursor.rowcount)


def _print_report(rows: list[dict[str, Any]], *, applied: bool, updated: int) -> None:
    mode = "APPLY" if applied else "DRY RUN"
    print(f"{mode}: {len(rows)} Codex runtime mismatch(es) found")
    for row in rows:
        transport_note = ""
        if row.get("transport") != "sdk":
            transport_note = f" transport {row.get('transport')!r}->'sdk'"
        print(
            f"- {row['name']}: runtime 'claude_sdk'->'codex_cli'"
            f"{transport_note}; provider_model={row.get('provider_model') or '<empty>'}"
        )
    if applied:
        print(f"Updated {updated} row(s).")
    elif rows:
        print("No rows changed. Re-run with --apply to write the backfill.")
    else:
        print("No rows changed.")


def parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Backfill agents with provider_url='codex_cli' and runtime='claude_sdk' "
            "to runtime='codex_cli'. Dry-run by default."
        )
    )
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"path to agents SQLite database (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="write the backfill; without this flag the script only reports matches",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        with _connect(args.db) as conn:
            rows = find_codex_runtime_mismatches(conn)
            updated = apply_codex_runtime_backfill(conn) if args.apply and rows else 0
    except (FileNotFoundError, RuntimeError, sqlite3.Error) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    _print_report(rows, applied=args.apply, updated=updated)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
