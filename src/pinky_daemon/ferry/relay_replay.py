"""Durable authenticated claims and a monotonic-only admission clock floor."""

from __future__ import annotations

import fcntl
import os
import threading
import time
import uuid
from contextlib import contextmanager
from pathlib import Path

from pinky_daemon.store_catalog import open_store_connection

PEER_QUOTA = 50_000
GLOBAL_QUOTA = 200_000
RETAIN_MS = 24 * 60 * 60 * 1000
CLOCK_BEHIND_MS = 5 * 60 * 1000
TERMINAL = frozenset({"DELIVERED", "REJECTED", "QUEUED", "DROPPED", "UNKNOWN"})
_OWNED = set()
_OWNERS_LOCK = threading.Lock()


class ReplayStoreError(RuntimeError):
    pass


class ReplayStore:
    def __init__(self, path, *, wall_ms=None, monotonic_ns=time.monotonic_ns, catalog=None):
        self.path = Path(path).absolute()
        self.wall_ms = wall_ms or (lambda: int(time.time() * 1000))
        self.monotonic_ns = monotonic_ns
        self._last_ns = monotonic_ns()
        self.owner = uuid.uuid4().hex
        self._mutex = threading.RLock()
        self._fd = None
        self._db = None
        self._closed = False
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        with _OWNERS_LOCK:
            if str(self.path) in _OWNED:
                raise ReplayStoreError("relay receiver already owned")
            _OWNED.add(str(self.path))
        try:
            self._fd = os.open(str(self.path) + ".receiver.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(self._fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                raise ReplayStoreError("relay receiver already owned") from None
            if self.path.is_symlink():
                raise ReplayStoreError("unsafe replay store")
            self._db = open_store_connection(catalog, "ferry_replay", str(self.path),
                                              owner="RelayReceiver", isolation_level=None,
                                              check_same_thread=False)
            self._db.execute("PRAGMA journal_mode=WAL")
            self._db.execute("PRAGMA synchronous=FULL")
            self._bootstrap()
            if catalog is not None:
                catalog.register("ferry_replay", str(self.path), journal_mode="wal",
                                 owner="RelayReceiver", criticality="authoritative", recovery="manual")
        except BaseException:
            self.close()
            raise

    def _bootstrap(self):
        with self.transaction() as db:
            tables = {r[0] for r in db.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            if not tables:
                db.execute("CREATE TABLE relay_floor (id INTEGER PRIMARY KEY CHECK(id=1), p INTEGER NOT NULL, h INTEGER NOT NULL, remainder_ns INTEGER NOT NULL, owner TEXT NOT NULL)")
                db.execute("CREATE TABLE relay_claims (sender_fp BLOB NOT NULL, inner_id TEXT NOT NULL, digest BLOB NOT NULL, relay_id TEXT NOT NULL, expires_at INTEGER NOT NULL, state TEXT NOT NULL, attempts INTEGER NOT NULL, next_retry_at INTEGER NOT NULL, PRIMARY KEY(sender_fp,inner_id))")
                db.execute("CREATE INDEX relay_claims_expiry ON relay_claims(expires_at)")
                now = self.wall_ms()
                if type(now) is not int or not -(2**63) <= now < 2**63:
                    raise ReplayStoreError("invalid replay setup clock")
                db.execute("INSERT INTO relay_floor VALUES(1,?,0,0,?)", (now, self.owner))
            elif tables != {"relay_floor", "relay_claims"}:
                raise ReplayStoreError("unexpected replay store schema")
            if db.execute("SELECT count(*) FROM relay_floor").fetchone()[0] != 1:
                raise ReplayStoreError("replay clock floor is missing")
            floor = db.execute("SELECT id,p,h,remainder_ns FROM relay_floor").fetchone()
            if (floor[0] != 1 or any(type(value) is not int for value in floor)
                    or not 0 <= floor[3] < 1_000_000 or floor[2] > floor[1]):
                raise ReplayStoreError("invalid replay clock floor")
            if db.execute("SELECT 1 FROM relay_claims WHERE state NOT IN "
                          "('CLAIMED','HANDING_OFF','RETRY','DELIVERED','REJECTED',"
                          "'QUEUED','DROPPED','UNKNOWN') OR length(sender_fp)!=16 "
                          "OR length(digest)!=32 OR attempts<0 LIMIT 1").fetchone():
                raise ReplayStoreError("invalid replay claim state")
            db.execute("UPDATE relay_floor SET owner=? WHERE id=1", (self.owner,))
            db.execute("UPDATE relay_claims SET state='UNKNOWN' WHERE state='HANDING_OFF'")
            db.execute("UPDATE relay_claims SET state='RETRY',next_retry_at=0 WHERE state='CLAIMED'")

    @contextmanager
    def transaction(self):
        with self._mutex:
            try:
                self._db.execute("BEGIN IMMEDIATE")
                yield self._db
                self._db.execute("COMMIT")
            except BaseException:
                if self._db is not None and self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise

    def _tick(self, db):
        p, h, remainder, owner = db.execute("SELECT p,h,remainder_ns,owner FROM relay_floor WHERE id=1").fetchone()
        if owner != self.owner:
            raise ReplayStoreError("replay receiver ownership changed")
        current = self.monotonic_ns()
        if current < self._last_ns:
            raise ReplayStoreError("monotonic clock moved backwards")
        elapsed = current - self._last_ns + remainder
        self._last_ns = current
        delta, remainder = divmod(elapsed, 1_000_000)
        p += delta
        db.execute("UPDATE relay_floor SET p=?,remainder_ns=? WHERE id=1", (p, remainder))
        return p, h

    def clock(self):
        with self.transaction() as db:
            p, h = self._tick(db)
        wall = self.wall_ms()
        state = "clock behind floor" if wall < p - CLOCK_BEHIND_MS else "clock ahead or long downtime" if wall > p + 3_600_000 else "ok"
        return {"wall": wall, "P": p, "H": h, "state": state}

    def claim(self, sender_fp, inner_id, digest, relay_id, expires_at, timestamp):
        with self.transaction() as db:
            p, _ = self._tick(db)
            now = self.wall_ms()
            if now < p - CLOCK_BEHIND_MS:
                return "clock"
            if timestamp > now + 600_000 or now > expires_at:
                return "expired"
            row = db.execute("SELECT digest,state,next_retry_at FROM relay_claims WHERE sender_fp=? AND inner_id=?", (sender_fp, inner_id)).fetchone()
            if row:
                if row[0] != digest:
                    return "equivocation"
                if row[1] in TERMINAL:
                    return "terminal"
                if row[1] == "HANDING_OFF":
                    db.execute("UPDATE relay_claims SET state='UNKNOWN' WHERE sender_fp=? AND inner_id=? AND state='HANDING_OFF'", (sender_fp, inner_id))
                    return "terminal"
                if row[1] == "RETRY" and row[2] <= now:
                    changed = db.execute("UPDATE relay_claims SET state='CLAIMED',relay_id=? WHERE sender_fp=? AND inner_id=? AND state='RETRY' AND next_retry_at<=?", (relay_id, sender_fp, inner_id, now)).rowcount
                    return "claimed" if changed == 1 else "busy"
                return "busy"
            total = db.execute("SELECT count(*) FROM relay_claims").fetchone()[0]
            peer = db.execute("SELECT count(*) FROM relay_claims WHERE sender_fp=?", (sender_fp,)).fetchone()[0]
            if total >= GLOBAL_QUOTA or peer >= PEER_QUOTA:
                return "quota"
            db.execute("INSERT INTO relay_claims VALUES(?,?,?,?,?,'CLAIMED',0,0)",
                       (sender_fp, inner_id, digest, relay_id, expires_at))
            return "claimed"

    def handoff(self, key):
        with self.transaction() as db:
            p, _ = self._tick(db)
            if self.wall_ms() < p - CLOCK_BEHIND_MS:
                db.execute("UPDATE relay_claims SET state='RETRY',next_retry_at=0 WHERE sender_fp=? AND inner_id=? AND state='CLAIMED'", key)
                return False
            return db.execute("UPDATE relay_claims SET state='HANDING_OFF' WHERE sender_fp=? AND inner_id=? AND state='CLAIMED'", key).rowcount == 1

    def finish(self, key, status):
        if status not in TERMINAL:
            raise ReplayStoreError("invalid replay terminal state")
        with self.transaction() as db:
            changed = db.execute("UPDATE relay_claims SET state=? WHERE sender_fp=? AND inner_id=? AND state='HANDING_OFF'", (status, *key)).rowcount
            if changed != 1:
                raise ReplayStoreError("replay terminal transition lost ownership")

    def retry(self, key):
        with self.transaction() as db:
            row = db.execute("SELECT attempts FROM relay_claims WHERE sender_fp=? AND inner_id=? AND state='HANDING_OFF'", key).fetchone()
            if row is None:
                raise ReplayStoreError("replay retry transition lost ownership")
            attempts = row[0] + 1
            state = "DROPPED" if attempts >= 5 else "RETRY"
            delay = min(60_000, 1000 * 2 ** (attempts - 1))
            db.execute("UPDATE relay_claims SET state=?,attempts=?,next_retry_at=? WHERE sender_fp=? AND inner_id=? AND state='HANDING_OFF'", (state, attempts, self.wall_ms() + delay, *key))
            return state

    def prune(self, limit=100):
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ReplayStoreError("invalid replay prune batch")
        with self.transaction() as db:
            p, h = self._tick(db)
            rows = db.execute("SELECT sender_fp,inner_id,expires_at FROM relay_claims WHERE expires_at<? ORDER BY expires_at LIMIT ?", (p - RETAIN_MS, limit)).fetchall()
            for sender_fp, ident, expires in rows:
                db.execute("DELETE FROM relay_claims WHERE sender_fp=? AND inner_id=? AND expires_at<?", (sender_fp, ident, p - RETAIN_MS))
                h = max(h, expires + RETAIN_MS)
            db.execute("UPDATE relay_floor SET h=? WHERE id=1", (h,))
            return len(rows)

    def snapshot(self):
        with self._mutex:
            counts = dict(self._db.execute("SELECT state,count(*) FROM relay_claims GROUP BY state"))
            size = sum(p.stat().st_size for p in (self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")) if p.exists())
        return {"states": counts, "bytes": size}

    def close(self):
        with self._mutex:
            if self._closed:
                return
            try:
                if self._db is not None:
                    self._db.close()
                    self._db = None
            finally:
                if self._fd is not None:
                    os.close(self._fd)
                    self._fd = None
                with _OWNERS_LOCK:
                    _OWNED.discard(str(self.path))
                self._closed = True


def floor_command(path, *, reset=False, confirm=False, wall_ms=None):
    now = (wall_ms or (lambda: int(time.time() * 1000)))()
    # Operator-only; it never acquires a receiver role or alters its owner row.
    if not Path(path).is_file():
        raise ReplayStoreError("replay store is not initialized")
    db = open_store_connection(None, "ferry_replay", str(path), owner="RelayFloorCommand",
                               isolation_level=None)
    try:
        db.execute("BEGIN IMMEDIATE" if reset else "BEGIN")
        p, h = db.execute("SELECT p,h FROM relay_floor WHERE id=1").fetchone()
        if reset:
            if not confirm or now <= p:
                raise ReplayStoreError("floor reset requires confirmation and a forward clock")
            db.execute("UPDATE relay_floor SET p=?,remainder_ns=0 WHERE id=1 AND p<?", (now, now))
            p = now
        db.execute("COMMIT")
        return {"wall": now, "P": p, "H": h}
    finally:
        if db.in_transaction:
            db.execute("ROLLBACK")
        db.close()
