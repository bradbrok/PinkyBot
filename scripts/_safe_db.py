"""Guard against opening live daemon/MCP-held SQLite stores in place.

The PinkyBot daemon keeps ~40 WAL-mode stores under ``data/`` open for its
whole lifetime. Agent ``memory.db`` stores are held either by the shared-MCP
memory pool running *inside the daemon process itself* (current topology) or
by a legacy per-agent stdio ``pinky_memory`` process. Opening one of those
files in place from an external process (a bare ``sqlite3`` CLI, a
backfill/migration script) recreates the ``-wal``/``-shm`` sidecars,
orphaning the holder's open fd: the holder then commits into an unlinked WAL
that no path-linked reader sees, and the on-disk main file silently freezes.
Recovered from exactly this on 2026-08-19 (root cause of #1126).

Call ``refuse_if_live_store(path, write=...)`` before any direct
``sqlite3.connect()`` on a file under ``data/``. It fails loud (SystemExit)
when a live holder may have the store open — including when holder detection
itself fails, because an unknown holder is not an absent holder.

Safe alternatives to an in-place open of a live store:

- daemon-catalog store (``data/*.db``): ``scripts/store_snapshot.py`` asks the
  daemon for a verified point-in-time snapshot without opening the file.
- agent ``memory.db``: read through the pinky-memory MCP tools; there is no
  external snapshot path while a holder runs.
- write/migration: stop the holder process first, then re-run.

A raw byte copy of db+wal is NOT a safe snapshot: it cannot orphan the
holder's WAL (it never opens the database), but it is not
transaction-consistent — under rollback-journal cache spill it captures
uncommitted pages that look committed and pass ``integrity_check``.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA_DIR = (REPO / "data").resolve()


class HolderDetectionError(RuntimeError):
    """Process detection failed: liveness of a potential holder is unknown."""


def _proc_running(pattern: str) -> bool:
    """True if any process command line matches ``pattern`` (pgrep -f).

    Only pgrep exit status 1 proves "no match". Execution failure, timeout,
    or any other exit status leaves holder liveness unknown and raises
    HolderDetectionError so callers fail closed instead of authorizing the
    open.
    """
    try:
        out = subprocess.run(
            ["pgrep", "-f", pattern], capture_output=True, text=True, timeout=5
        )
    except Exception as exc:
        raise HolderDetectionError(f"pgrep -f {pattern!r}: {exc}") from exc
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    raise HolderDetectionError(
        f"pgrep -f {pattern!r} exited {out.returncode}: "
        f"{out.stderr.strip() or 'no stderr'}"
    )


def _holder(path) -> str | None:
    """Return a human label for a live holder of ``path``, or None.

    Raises HolderDetectionError when a check that could clear the path fails.
    Covers both memory-store topologies: the shared-MCP pool inside the
    daemon process (current) and a legacy stdio ``pinky_memory`` process.
    The daemon pattern is deliberately broad ("pinky_daemon", any mode): a
    false positive only refuses an open, never authorizes one.
    """
    p = Path(path).resolve()
    # Main daemon store: a *.db directly in the daemon's data/ dir.
    if p.suffix == ".db" and p.parent == DATA_DIR:
        if _proc_running("pinky_daemon"):
            return "the PinkyBot daemon"
    # Per-agent memory store: the daemon's shared-MCP memory pool opens these
    # in-process, so the daemon itself is a holder even with no separate
    # pinky_memory process running.
    if p.name == "memory.db" and "agents" in p.parts and DATA_DIR in p.parents:
        if _proc_running("pinky_daemon"):
            return "the PinkyBot daemon (shared-MCP memory pool)"
        if _proc_running("pinky_memory"):
            return "a pinky-memory MCP"
    return None


_ALTERNATIVES = (
    "  - daemon-catalog store: scripts/store_snapshot.py (daemon-owned, verified)\n"
    "  - agent memory.db:      read via the pinky-memory MCP tools\n"
    "  - write/migration:      stop the holder process first, then re-run."
)


def refuse_if_live_store(path, *, write: bool) -> None:
    """Abort (SystemExit) before an in-place open of a live daemon/MCP store.

    Refuses both write and read in-place opens while the holder runs: a read
    open in the default journal mode still recreates the sidecars and orphans
    the holder's WAL. Also refuses when holder detection fails — the guard
    must not authorize an open it cannot prove safe.
    """
    kind = "write" if write else "read"
    try:
        holder = _holder(path)
    except HolderDetectionError as exc:
        raise SystemExit(
            f"REFUSED (fail closed): cannot determine whether a live holder has "
            f"{path} open ({exc}); an unknown holder is not an absent holder.\n"
            f"{_ALTERNATIVES}"
        ) from exc
    if holder is not None:
        raise SystemExit(
            f"REFUSED: in-place {kind} open of {path} while {holder} holds it open "
            f"would orphan its WAL and silently freeze the on-disk file (#1126).\n"
            f"{_ALTERNATIVES}"
        )
