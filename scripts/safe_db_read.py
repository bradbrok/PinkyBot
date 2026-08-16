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
1. BEST (default here): copy the DB (+ any -wal/-shm) to a temp path, then read the
   COPY. Zero touch on the live file. Consistent snapshot. Use for anything.
2. OK for rough reads: ``mode=ro&immutable=1`` — immutable=1 tells SQLite the file
   won't change, so it never creates/opens -wal/-shm and never checkpoints/unlinks.
   Caveat: reads can be TORN on a live, actively-written DB (you may see a
   partially-updated page view). Fine for quick "latest row / schema" peeks.
   NB: ``mode=ro`` ALONE is NOT safe — it still re-creates -shm (#932).
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


def immutable_connect(db_path: str) -> sqlite3.Connection:
    """Tier 2: read-only + immutable=1 URI connection. Never touches -wal/-shm.
    May read torn pages on a live DB — use only for rough/quick reads."""
    uri = f"file:{Path(db_path).resolve()}?mode=ro&immutable=1"
    return sqlite3.connect(uri, uri=True)


@contextlib.contextmanager
def snapshot_connect(db_path: str) -> Iterator[sqlite3.Connection]:
    """Tier 1: copy the DB (+ sidecars) to a temp dir and connect to the COPY.
    Zero touch on the live file. Yields a connection to the consistent copy."""
    src = Path(db_path).resolve()
    tmpdir = Path(tempfile.mkdtemp(prefix="safe_db_read_"))
    try:
        dst = tmpdir / src.name
        shutil.copy2(src, dst)
        # Copy sidecars if present so the snapshot replays any pending WAL itself.
        for suffix in ("-wal", "-shm"):
            side = Path(str(src) + suffix)
            if side.exists():
                shutil.copy2(side, tmpdir / side.name)
        conn = sqlite3.connect(str(dst))
        try:
            yield conn
        finally:
            conn.close()
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
