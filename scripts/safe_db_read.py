#!/usr/bin/env python3
"""Safe read access to a LIVE daemon SQLite DB — never poison its WAL (#889).

WHY THIS EXISTS
---------------
Opening a live WAL-mode daemon DB with a plain/RW client (the classic
``sqlite3 data/conversations.db "SELECT ..."`` one-liner, or a bare
``sqlite3.connect(path)``) is dangerous: when that client closes, SQLite runs a
checkpoint-on-last-close and UNLINKS the ``-wal``/``-shm`` files. Because POSIX
advisory locks are per-process, the client can be the effective "last connection"
while the daemon's own connection is idle between transactions — so the WAL is
deleted out from under the daemon's open fd. No data is lost immediately, but the
daemon now holds a fd to a DELETED inode, and on the next restart the un-replayed
WAL is gone and the main DB can be left header-corrupt (the #889 family: 6+
user_profiles malformations + multi-instance outages).

SAFE TIERS
----------
1. BEST (default here): snapshot the DB into a temp file via SQLite's ONLINE
   BACKUP API, reading the source through the daemon's sanctioned live-DB reader
   (``BoundSQLiteFile``, #619: open-once ``O_RDONLY|O_NOFOLLOW`` + ``mode=ro`` on
   the pinned fd), then read the COPY. The backup runs inside a real read
   transaction, so the snapshot is transaction-consistent (only COMMITTED rows) —
   a raw byte copy is NOT: in rollback-journal mode a mid-transaction writer
   spills dirty UNCOMMITTED pages into the main file, so a byte copy captures
   phantoms (#889 follow-up). Zero write-touch on the live file. Use for anything.
2. OK for rough reads: ``mode=ro&immutable=1`` — immutable=1 tells SQLite the file
   won't change, so it never creates/opens -wal/-shm and never checkpoints/unlinks.
   Caveat: reads can be TORN on a live, actively-written DB (you may see a
   partially-updated page view). Fine for quick "latest row / schema" peeks.
   NB: ``mode=ro`` ALONE (a bare ad-hoc connect) re-creates -shm on a WAL source
   (#932) — that is why tier 1 goes through ``BoundSQLiteFile`` and not a raw
   ``mode=ro`` open of its own.
3. NEVER: raw ``sqlite3 <live-db>`` / plain ``sqlite3.connect(path)`` / ``mode=ro``
   without ``immutable=1``.

USAGE
-----
    # tier 1 (safe snapshot):
    python3 scripts/safe_db_read.py data/conversations.db \
        "SELECT id, datetime(timestamp,'unixepoch'), substr(content,1,80) \
         FROM messages ORDER BY id DESC LIMIT 5"

    # tier 2 (fast, may tear):
    python3 scripts/safe_db_read.py --immutable data/conversations.db "SELECT count(*) FROM messages"

    # importable:
    from scripts.safe_db_read import safe_connect, snapshot_query
    for row in snapshot_query("data/conversations.db", "SELECT ..."):
        ...
"""

from __future__ import annotations

import argparse
import contextlib
import shutil
import sqlite3
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

# Tier 1 opens the live source through the daemon's sanctioned live-DB reader
# (BoundSQLiteFile, #619): one open-once O_RDONLY|O_NOFOLLOW fd, journal mode read
# from the raw header bytes, mode=ro over that pinned descriptor — so there is ONE
# read discipline, not a parallel mode=ro path. scripts/ is not under src/, so make
# pinky_daemon importable for standalone CLI use too.
_REPO_SRC = Path(__file__).resolve().parent.parent / "src"
if _REPO_SRC.is_dir() and str(_REPO_SRC) not in sys.path:
    sys.path.insert(0, str(_REPO_SRC))
from pinky_daemon.store_catalog import BoundSQLiteFile  # noqa: E402


def immutable_connect(db_path: str) -> sqlite3.Connection:
    """Tier 2: read-only + immutable=1 URI connection. Never touches -wal/-shm.
    May read torn pages on a live DB — use only for rough/quick reads."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


@contextlib.contextmanager
def snapshot_connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Tier 1: snapshot the live DB into a fresh temp DB via SQLite's ONLINE
    BACKUP API, then connect to the COPY. Yields a connection to a
    transaction-consistent copy; zero *write* touch on the live file.

    Why the backup API and NOT ``shutil.copy2`` (the #889 follow-up caught in review):
    a raw byte copy of the main DB file is not transaction-consistent. The churn
    stores run in ROLLBACK-journal mode (DELETE/TRUNCATE), and there a writer
    flushes DIRTY, UNCOMMITTED pages straight into the *main* file mid-transaction
    whenever its page cache spills — the undo image lives in the ``-journal``
    sidecar, which the old code did not even copy. So ``shutil.copy2`` of the main
    file alone captured phantom, never-committed rows (probe: 2494/2500 uncommitted
    rows visible through the snapshot). The backup API instead reads the source
    through a proper SQLite read transaction, so it only ever sees the last
    COMMITTED state: in WAL mode it folds in committed WAL frames; in rollback mode
    it takes a SHARED lock, which blocks behind the writer's EXCLUSIVE lock until
    that transaction commits or rolls back, so spilled dirty pages are never read.

    Still #889-safe, and now on the SANCTIONED read path: the source is opened via
    ``BoundSQLiteFile`` (the daemon-owned live-DB reader, #619) — open-once
    ``O_RDONLY|O_NOFOLLOW``, journal mode read from the raw header bytes, then a
    ``mode=ro`` connection over that one pinned descriptor. A read-only connection
    never checkpoints, so it never unlinks the live ``-wal``/``-shm``; and reusing
    the one reader avoids a parallel raw ``mode=ro`` open (which alone re-creates
    ``-shm`` on a WAL source, #932). ``connect_read_only``'s ``timeout`` waits out
    the brief window a writer holds the rollback-mode EXCLUSIVE lock, so the
    snapshot WAITS for the commit/rollback rather than racing it (the phantom bug).
    """
    src = Path(db_path).resolve()
    tmpdir = Path(tempfile.mkdtemp(prefix="safe_db_read_"))
    try:
        # Open the source through the sanctioned reader: one open-once
        # O_RDONLY|O_NOFOLLOW fd, mode=ro over that pinned descriptor. timeout waits
        # out (does not race) a writer briefly holding the rollback-mode lock.
        with BoundSQLiteFile.open(src) as bound:
            src_conn = bound.connect_read_only(timeout=30)
            try:
                dst_path = tmpdir / src.name
                dst_conn = sqlite3.connect(str(dst_path))
                try:
                    src_conn.backup(dst_conn)
                    src_conn.close()
                    yield dst_conn
                finally:
                    dst_conn.close()
            finally:
                with contextlib.suppress(Exception):
                    src_conn.close()
    finally:
        shutil.rmtree(tmpdir, ignore_errors=True)


def safe_connect(db_path: str, *, immutable: bool = False):
    """Return a safe connection. Default = tier-1 snapshot (context manager);
    immutable=True = tier-2 immutable RO connection (plain, caller closes)."""
    if immutable:
        return immutable_connect(db_path)
    return snapshot_connect(db_path)


def snapshot_query(db_path: str, sql: str, params: tuple = ()) -> list:
    """Convenience: run a query against a tier-1 snapshot and return all rows."""
    with snapshot_connect(db_path) as conn:
        return conn.execute(sql, params).fetchall()


def _main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="Safe read of a live daemon SQLite DB (#889).")
    ap.add_argument("db_path")
    ap.add_argument("sql")
    ap.add_argument(
        "--immutable",
        action="store_true",
        help="tier 2: mode=ro&immutable=1 (fast, may read torn pages). Default = tier-1 copy-and-read.",
    )
    ap.add_argument("-separator", "--separator", default="|", dest="sep")
    args = ap.parse_args(argv)
    try:
        if args.immutable:
            conn = immutable_connect(args.db_path)
            try:
                rows = conn.execute(args.sql).fetchall()
            finally:
                conn.close()
        else:
            rows = snapshot_query(args.db_path, args.sql)
    except sqlite3.Error as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    for row in rows:
        print(args.sep.join("" if v is None else str(v) for v in row))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))
