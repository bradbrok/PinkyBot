#!/usr/bin/env python3
"""One-time backfill of missing reflection embeddings (issue #630).

Background: the pinky-memory embedder only ever read OPENAI_API_KEY from the
process env, while the key actually lives in system_settings. So it ran NoOp
and stored empty embeddings (``'[]'``) fleet-wide — semantic recall() silently
degraded to keyword-only (found 2026-06-05). After the embedder fix
(get_setting -> env) deploys, NEW reflects embed correctly; this script
re-embeds the historical rows that were written empty.

Safety:
  * DRY-RUN by default. Pass --apply to write.
  * Idempotent: only touches rows whose embedding is empty (``length<=2``,
    i.e. the ``'[]'`` default) AND have non-empty content. Never overwrites an
    existing real embedding, never touches content.
  * Per-batch transactions with a busy timeout so it cooperates with the live
    daemon's WAL connections.
  * The OpenAI key is read from system_settings (same source the fix uses);
    nothing is printed.

Usage:
    python scripts/backfill_embeddings.py                 # dry-run, all agents
    python scripts/backfill_embeddings.py --apply         # write
    python scripts/backfill_embeddings.py --apply --agent barsik
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sqlite3
import sys

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(REPO, "src"))

SETTINGS_DB = os.path.join(REPO, "data", "conversations_agents.db")
BATCH = 100


def resolve_openai_key() -> str:
    """Read OPENAI_API_KEY from system_settings, else env. No printing."""
    try:
        conn = sqlite3.connect(SETTINGS_DB)
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key='OPENAI_API_KEY'"
        ).fetchone()
        conn.close()
        if row and row[0]:
            return row[0]
    except Exception as exc:  # noqa: BLE001
        print(f"  (settings lookup failed: {type(exc).__name__}: {exc})")
    return os.environ.get("OPENAI_API_KEY", "")


def discover_dbs(agent: str = "") -> list[str]:
    pat = (
        os.path.join(REPO, "data", "agents", agent, "data", "memory.db")
        if agent
        else os.path.join(REPO, "data", "agents", "*", "data", "memory.db")
    )
    dbs = []
    for p in sorted(glob.glob(pat)):
        try:
            c = sqlite3.connect(p)
            has = c.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='reflections'"
            ).fetchone()
            c.close()
            if has:
                dbs.append(p)
        except Exception:  # noqa: BLE001
            continue
    return dbs


def backfill_db(path: str, embedder, apply: bool) -> tuple[int, int]:
    """Returns (rows_needing_backfill, rows_written)."""
    conn = sqlite3.connect(path, timeout=30.0)
    conn.execute("PRAGMA busy_timeout=30000")
    rows = conn.execute(
        "SELECT id, content FROM reflections "
        "WHERE length(embedding)<=2 AND length(trim(content))>0 "
        "ORDER BY created_at"
    ).fetchall()
    need = len(rows)
    if not apply or need == 0:
        conn.close()
        return need, 0

    written = 0
    for i in range(0, need, BATCH):
        chunk = rows[i : i + BATCH]
        vectors = embedder.embed_batch([c for _, c in chunk])
        cur = conn.cursor()
        for (rid, _), vec in zip(chunk, vectors):
            if not vec:  # embed failed for this item — leave it for a re-run
                continue
            cur.execute(
                "UPDATE reflections SET embedding=? WHERE id=?",
                (json.dumps(vec), rid),
            )
            written += 1
        conn.commit()
        print(f"    {path.split('/agents/')[-1]}: {min(i + BATCH, need)}/{need}")
    conn.close()
    return need, written


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="write (default: dry-run)")
    ap.add_argument("--agent", default="", help="limit to one agent")
    args = ap.parse_args()

    key = resolve_openai_key()
    if not key:
        print("FATAL: no OPENAI_API_KEY in system_settings or env — cannot embed.")
        return 1

    from pinky_memory.embeddings import EmbeddingClient

    embedder = EmbeddingClient(api_key=key) if args.apply else None
    dbs = discover_dbs(args.agent)
    print(f"{'APPLY' if args.apply else 'DRY-RUN'} — {len(dbs)} memory DB(s)\n")

    total_need = total_written = 0
    for db in dbs:
        need, written = backfill_db(db, embedder, args.apply)
        total_need += need
        total_written += written
        tag = db.split("/agents/")[-1]
        print(f"  {tag}: {need} need embedding" + (f", {written} written" if args.apply else ""))

    print(
        f"\nTOTAL: {total_need} rows need embedding"
        + (f", {total_written} written." if args.apply else " (dry-run; pass --apply to write).")
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
