#!/usr/bin/env python3
"""Recurring ephemeral-node sweep for an agent's KG (#153 phase-2, step 1).

The mitigation half of the #153 plan: the dream/extraction write-path keeps
minting *pure-identifier* "entities" (bare PR/issue/task/release IDs, version
stamps, owner/repo#NNN) that are point-in-time events, not durable graph
nodes. Pass-1 (scripts/kg_hygiene.py) removed a hardcoded list of them once;
this script generalizes that pass to PATTERNS so it can be cron'd to catch
regrowth, and doubles as the instrument that measures the regrowth rate.

The durable fix is the write-path GUARD (a separate, reviewed pinky-memory
PR). This sweep is intentionally conservative and reversible — it is NOT the
guard, and it does NOT attempt the judgment-heavy work (descriptive-ephemeral
demotion, concept retyping, fuzzy dedup).

Safety design (per Dymok's reliability review):
  * Anchored, full-name patterns only — must match the ENTIRE entity name.
    Misses (e.g. "PR #124 period foundation") are intentional: descriptive
    nodes carry context and are out of scope. Miss-and-recatch > false-delete.
  * Auto-backup of the DB file before any --apply that actually deletes.
  * Every deleted entity AND every deleted edge is logged to a JSONL regrowth
    log, so false positives are auditable and the rate is measurable.
  * Dry-run by default (prints plan, rolls back). --apply commits.
  * Fails safe: on a locked/busy DB it backs off and exits non-zero without
    partial writes (single transaction).

Usage:
    python3 scripts/kg_ephemeral_sweep.py <memory.db> [--apply] [--log PATH]
"""
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone

# Detection logic lives in the package so the write-time guard
# (src/pinky_memory/ephemeral_guard.py) and this sweep share ONE source of
# truth and can never drift. Requires the pinky_memory package importable
# (editable install / installed wheel), which is the case wherever the daemon
# runs this cron.
from pinky_memory.ephemeral_guard import is_ephemeral

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # scripts/ for _safe_db
from _safe_db import refuse_if_live_store  # noqa: E402


def find_candidates(conn):
    rows = conn.execute("SELECT id, name, type FROM kg_entities").fetchall()
    return [(eid, name, etype) for eid, name, etype in rows if is_ephemeral(name)]


def edges_for(conn, name):
    return conn.execute(
        "SELECT id, subject, predicate, object, valid_to FROM kg_triples "
        "WHERE subject = ? OR object = ?",
        (name, name),
    ).fetchall()


def backup_db(db_path):
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%d_%H%M%S")
    base = os.path.basename(db_path).replace(".db", "")
    dest_dir = os.path.join("data", "backups", "kg", f"{ts}_sweep")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, f"{base}.db")
    shutil.copy2(db_path, dest)
    # WAL/SHM too, if present, so the copy is consistent on restore.
    for ext in ("-wal", "-shm"):
        if os.path.exists(db_path + ext):
            shutil.copy2(db_path + ext, dest + ext)
    return dest


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        print(__doc__)
        sys.exit(0 if len(sys.argv) >= 2 else 1)

    db = sys.argv[1]
    args = sys.argv[2:]
    apply = "--apply" in args
    log_path = "data/backups/kg/ephemeral_sweep_log.jsonl"
    if "--log" in args:
        log_path = args[args.index("--log") + 1]

    if not os.path.exists(db):
        print(f"ERROR: db not found: {db}")
        sys.exit(2)

    # Never open a live memory.db in place (#1126): stop the agent's MCP first.
    # (busy_timeout below handles lock contention, NOT WAL-sidecar orphaning.)
    refuse_if_live_store(db, write=True)
    conn = sqlite3.connect(db, timeout=10)
    conn.execute("PRAGMA busy_timeout = 8000")

    candidates = find_candidates(conn)
    deleted_edges = []
    for _eid, name, _etype in candidates:
        for tid, s, p, o, vt in edges_for(conn, name):
            deleted_edges.append(
                {"triple_id": tid, "subject": s, "predicate": p, "object": o,
                 "active": vt is None, "via": name}
            )

    n_ent = len(candidates)
    n_active_edges = sum(1 for e in deleted_edges if e["active"])
    n_total_edges = len(deleted_edges)

    print(f"db: {db}")
    print(f"ephemeral pure-ID candidates: {n_ent}")
    for _eid, name, etype in candidates:
        ec = sum(1 for e in deleted_edges if e["via"] == name)
        print(f"  - {name!r} (type={etype}, edges={ec})")
    print(f"edges to remove: {n_total_edges} ({n_active_edges} active)")

    backup_path = None
    applied = False
    if apply and n_ent:
        backup_path = backup_db(db)
        print(f"backup: {backup_path}")
        try:
            conn.execute("BEGIN IMMEDIATE")
            for _eid, name, _etype in candidates:
                conn.execute("DELETE FROM kg_triples WHERE subject = ? OR object = ?", (name, name))
                conn.execute("DELETE FROM kg_entities WHERE name = ?", (name,))
            conn.commit()
            applied = True
            print("APPLIED + committed.")
        except sqlite3.OperationalError as e:
            conn.rollback()
            print(f"ABORTED (db busy/locked): {e}")
            conn.close()
            sys.exit(3)
    elif apply:
        print("nothing to apply (0 candidates).")
    else:
        print("DRY RUN — no changes.")

    conn.close()

    # Regrowth log: one JSONL record per run. n_candidates per run = regrowth
    # since the previous sweep (the measurement Dymok asked for).
    record = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "db": db,
        "n_candidates": n_ent,
        "candidates": [name for _e, name, _t in candidates],
        "n_edges": n_total_edges,
        "n_active_edges": n_active_edges,
        "deleted_edges": deleted_edges if applied else [],
        "applied": applied,
        "backup": backup_path,
    }
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    with open(log_path, "a") as f:
        f.write(json.dumps(record) + "\n")
    print(f"logged -> {log_path}")


if __name__ == "__main__":
    main()
