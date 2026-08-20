"""Guard against opening live daemon/MCP-held SQLite stores in place.

The PinkyBot daemon keeps ~40 WAL-mode stores under ``data/`` open for its
whole lifetime, and each agent's pinky-memory MCP keeps that agent's
``memory.db`` open. Opening one of those files in place from an external
process (a bare ``sqlite3`` CLI, a backfill/migration script) recreates the
``-wal``/``-shm`` sidecars, orphaning the holder's open fd: the holder then
commits into an unlinked WAL that no path-linked reader sees, and the on-disk
main file silently freezes. Recovered from exactly this on 2026-08-19 (root
cause of #1126); see notes/wal-orphan-tooling-audit-641.md.

A raw *byte* copy (``shutil.copy2`` / cp / rsync) is safe: it never opens the
file as a database, so it never recreates the sidecars.

Use this instead of a bare ``sqlite3.connect()`` on anything under ``data/``:

- ``refuse_if_live_store(path, write=...)`` -- FAIL LOUD before an in-place open
  of a store whose holder process is running. For a *registered* store prefer
  the daemon-owned verified snapshot (``scripts/store_snapshot.py``).
- ``snapshot_ro(path)`` -- raw-copy db (+ -wal/-shm) to a temp file and return a
  read-only connection to the copy; safe against a live holder.
"""

from __future__ import annotations

import shutil
import sqlite3
import subprocess
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = (REPO / "data").resolve()


def _proc_running(pattern: str) -> bool:
    """True if any process command line matches ``pattern`` (pgrep -f)."""
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5
        )
        return out.returncode == 0 and out.stdout.strip() != ""
    except Exception:
        # Detection failure only: the snapshot_ro copy path is always safe
        # regardless, and refuse_if_live_store treats "unknown" conservatively.
        return False


def _holder(path) -> str | None:
    """Return a human label for the live holder of ``path``, or None.

    Covers the two external-open vectors: a main-daemon store (``data/*.db``)
    and an agent's pinky-memory ``memory.db``.
    """
    p = Path(path).resolve()
    # Main daemon store: a *.db directly in the daemon's data/ dir.
    if p.suffix == ".db" and p.parent == DATA_DIR:
        if _proc_running("pinky_daemon --mode api"):
            return "the PinkyBot daemon"
    # Per-agent memory store held by that agent's pinky-memory MCP.
    if p.name == "memory.db" and "agents" in p.parts and DATA_DIR in p.parents:
        if _proc_running("pinky_memory"):
            return "a pinky-memory MCP"
    return None


def refuse_if_live_store(path, *, write: bool) -> None:
    """Abort (SystemExit) before an in-place open of a live daemon/MCP store.

    Refuses both write and read in-place opens while the holder runs: a read
    open in the default journal mode still recreates the sidecars and orphans
    the holder's WAL. Stop the holder first, or use the safe alternatives.
    """
    holder = _holder(path)
    if holder is not None:
        kind = "write" if write else "read"
        raise SystemExit(
            f"REFUSED: in-place {kind} open of {path} while {holder} holds it open "
            f"would orphan its WAL and silently freeze the on-disk file (#1126).\n"
            f"  - registered store: use scripts/store_snapshot.py (daemon-owned, verified)\n"
            f"  - ad-hoc read:      use _safe_db.snapshot_ro(path)\n"
            f"  - write/migration:  stop the holder process first, then re-run."
        )


def snapshot_ro(path) -> sqlite3.Connection:
    """Raw-copy ``path`` (+ -wal/-shm) to a temp file; return a RO connection.

    Safe against a live holder -- reads a point-in-time byte copy, never the
    live file. The caller owns closing the connection; the temp copy is left in
    the system temp dir for the caller to discard.
    """
    src = Path(path)
    tmpdir = Path(tempfile.mkdtemp(prefix="safe_db_"))
    dst = tmpdir / src.name
    shutil.copy2(src, dst)
    for ext in ("-wal", "-shm"):
        sidecar = Path(str(src) + ext)
        if sidecar.exists():
            shutil.copy2(sidecar, str(dst) + ext)
    return sqlite3.connect(f"file:{dst}?mode=ro", uri=True)
