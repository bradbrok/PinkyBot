from __future__ import annotations

import importlib.util
import sqlite3
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "backfill_codex_runtime.py"
SPEC = importlib.util.spec_from_file_location("backfill_codex_runtime", SCRIPT_PATH)
assert SPEC is not None
backfill_codex_runtime = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(backfill_codex_runtime)


def _make_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(str(path))
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE agents (
            name TEXT PRIMARY KEY,
            runtime TEXT NOT NULL,
            transport TEXT NOT NULL,
            provider_url TEXT NOT NULL,
            provider_model TEXT NOT NULL DEFAULT '',
            provider_ref TEXT NOT NULL DEFAULT '',
            updated_at REAL NOT NULL DEFAULT 0
        )
        """
    )
    return conn


def test_dry_run_reports_only_codex_provider_claude_runtime_rows(tmp_path):
    db_path = tmp_path / "agents.db"
    conn = _make_db(db_path)
    conn.executemany(
        """
        INSERT INTO agents (name, runtime, transport, provider_url, provider_model)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("bad-codex", "claude_sdk", "sdk", "codex_cli", "gpt-5.4"),
            ("already-codex", "codex_cli", "sdk", "codex_cli", "gpt-5.4"),
            ("opencode-future", "opencode", "sdk", "codex_cli", "deepseek"),
            ("plain-claude", "claude_sdk", "sdk", "", "claude-opus-4-6"),
        ],
    )
    conn.commit()

    rows = backfill_codex_runtime.find_codex_runtime_mismatches(conn)

    assert [row["name"] for row in rows] == ["bad-codex"]
    assert conn.execute("SELECT runtime FROM agents WHERE name='bad-codex'").fetchone()[0] == "claude_sdk"


def test_apply_updates_runtime_and_forces_sdk_transport(tmp_path):
    db_path = tmp_path / "agents.db"
    conn = _make_db(db_path)
    conn.executemany(
        """
        INSERT INTO agents (name, runtime, transport, provider_url, provider_model)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            ("bad-codex", "claude_sdk", "tmux", "codex_cli", "gpt-5.4"),
            ("already-codex", "codex_cli", "sdk", "codex_cli", "gpt-5.4"),
            ("opencode-future", "opencode", "sdk", "codex_cli", "deepseek"),
        ],
    )
    conn.commit()

    updated = backfill_codex_runtime.apply_codex_runtime_backfill(conn, now=123.0)

    assert updated == 1
    row = conn.execute(
        "SELECT runtime, transport, updated_at FROM agents WHERE name='bad-codex'"
    ).fetchone()
    assert dict(row) == {"runtime": "codex_cli", "transport": "sdk", "updated_at": 123.0}
    assert conn.execute(
        "SELECT runtime FROM agents WHERE name='opencode-future'"
    ).fetchone()[0] == "opencode"
