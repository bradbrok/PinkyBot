"""Serialized queue writes with dirty-page admission and observed checkpoints."""

from __future__ import annotations

import base64
import fcntl
import json
import math
import os
import sqlite3
import stat
import sys
import threading
import time
import uuid
from collections import deque
from contextlib import contextmanager
from pathlib import Path

from pinky_relay.config import canonical_fleet

MIB = 1024 * 1024
DISK_CAP = 2 * 1024 * MIB
OP_CAP = MARGIN = 16 * MIB
PAGE_SIZE = 4096
DEPTH = 20
TREES = 5
MAX_BLOB = 256 * 1024
MAX_PAGE = MIB
WAL_TRIGGER = 32 * MIB
WAL_STOP = 64 * MIB
TTL_MS = 7 * 24 * 60 * 60 * 1000
LEASE_MS = 60_000
SCHEMA = {
    "messages": "CREATE TABLE messages (id TEXT PRIMARY KEY NOT NULL, sender_fleet TEXT NOT NULL, to_fleet TEXT NOT NULL, blob BLOB NOT NULL, received_at INTEGER NOT NULL, lease_until INTEGER NOT NULL)",
    "messages_poll": "CREATE INDEX messages_poll ON messages(to_fleet, received_at, id)",
    "messages_pair": "CREATE INDEX messages_pair ON messages(sender_fleet, to_fleet)",
    "messages_ttl": "CREATE INDEX messages_ttl ON messages(received_at)",
    "sqlite_autoindex_messages_1": None,
}
INDEX_COLUMNS = {
    "messages_poll": ("to_fleet", "received_at", "id", None),
    "messages_pair": ("sender_fleet", "to_fleet", None),
    "messages_ttl": ("received_at", None),
    "sqlite_autoindex_messages_1": ("id", None),
}


class StoreRefusalError(RuntimeError):
    """A fixed, content-free operational refusal."""


class GrowthStopError(StoreRefusalError):
    pass


class QueueFullError(StoreRefusalError):
    pass


def require_runtime(version=None):
    if (version or sys.version_info) < (3, 12):
        raise StoreRefusalError("relay requires Python >= 3.12")
    if not hasattr(sqlite3.Connection, "setconfig"):
        raise StoreRefusalError("relay requires SQLite connection configuration support")


def row_pages(fleet_bytes: int) -> int:
    table = MAX_BLOB + 64 + 2 * fleet_bytes + 128
    indexes = (64 + 9 + 64, fleet_bytes + 9 + 64 + 9 + 64,
               2 * fleet_bytes + 9 + 64, 9 + 9 + 64)
    overflow = math.ceil(table / 4092) + sum(math.ceil(n / 4092) for n in indexes)
    overflow += 2 * 5 * (DEPTH + 1) * sum(math.ceil(n / 4092) for n in indexes if n > 1002)
    return 2 * (overflow + 5 * TREES * (DEPTH + 1))


def operation_bound(rows: int, pages: int) -> int:
    dirty = 2 + rows * pages
    return 32 + dirty * (PAGE_SIZE + 24) + 32768 * (math.ceil(dirty / 4096) + 1)


class QueueStore:
    def __init__(self, path, fleets, *, disk_cap=DISK_CAP, observer=None, connection=sqlite3.connect):
        require_runtime()
        self.path = Path(path)
        self.fleets = frozenset(canonical_fleet(name) for name in fleets)
        if not self.fleets:
            raise StoreRefusalError("relay requires configured fleets")
        self.cap = disk_cap
        self.observations = deque(maxlen=1024)
        self.observer = observer
        self._connection = connection
        self._mutex = threading.RLock()
        self._readers = set()
        self.stopped = False
        self.accepting = True
        self.closed = False
        self._db = None
        self._owner = None
        self.path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
        info = self.path.parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid()
                or stat.S_IMODE(info.st_mode) & 0o022):
            raise StoreRefusalError("unsafe relay store directory")
        fd = os.open(str(self.path) + ".writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise StoreRefusalError("relay writer already owned") from None
        self._owner = fd
        try:
            creating = not self.path.exists() or self.path.stat().st_size == 0
            if self.path.is_symlink():
                raise StoreRefusalError("unsafe relay store file")
            self._db = self._open(writer=True, creating=creating)
            if creating:
                self._create()
            self._assert_geometry(self._db, writer=True)
            self._assert_schema()
            longest = max(len(n.encode("ascii")) for n in self.fleets)
            for sender, target, ident, size in self._db.execute(
                "SELECT sender_fleet,to_fleet,id,length(blob) FROM messages"
            ):
                if (canonical_fleet(sender) != sender or canonical_fleet(target) != target
                        or not isinstance(ident, str) or not ident.isascii() or len(ident) > 64
                        or not isinstance(size, int) or size > MAX_BLOB):
                    raise StoreRefusalError("stored relay row violates geometry")
                longest = max(longest, len(sender), len(target))
            self.fleet_bytes = longest
            self.pages = row_pages(longest)
            self.batch = 0
            while operation_bound(self.batch + 1, self.pages) <= OP_CAP:
                self.batch += 1
            self.lease_batch = self.batch // 2
            if self.batch < 1 or self.lease_batch < 1:
                raise StoreRefusalError("relay row geometry exceeds operation budget")
            if self.reserved_size() > self.cap - MARGIN:
                raise StoreRefusalError("relay store lacks safe checkpoint headroom")
            self.barrier()
        except BaseException:
            self._close_connections()
            raise

    def _open(self, *, writer, creating=False):
        db = self._connection(str(self.path), isolation_level=None, timeout=0.02)
        try:
            db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
            if not db.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE):
                raise StoreRefusalError("NO_CKPT_ON_CLOSE read-back failed")
            if creating:
                db.execute("PRAGMA page_size=4096")
                db.execute("PRAGMA auto_vacuum=NONE")
            for setting in ("secure_delete=OFF", "temp_store=MEMORY", "wal_autocheckpoint=0",
                            "synchronous=FULL"):
                db.execute("PRAGMA " + setting)
            if writer:
                db.execute("PRAGMA cache_spill=OFF")
            return db
        except BaseException:
            db.close()
            raise

    def _create(self):
        if self.reserved_size() + OP_CAP > self.cap - MARGIN:
            raise StoreRefusalError("relay creation lacks admission headroom")
        if self._db.execute("PRAGMA journal_mode=WAL").fetchone()[0] != "wal":
            raise StoreRefusalError("relay requires WAL")
        before = self.sizes()
        self._db.execute("BEGIN IMMEDIATE")
        try:
            for statement in SCHEMA.values():
                if statement:
                    self._db.execute(statement)
            self._db.execute("COMMIT")
        except BaseException:
            if self._db.in_transaction:
                self._db.execute("ROLLBACK")
            raise
        finally:
            self._observe("creation", before, bound=OP_CAP)

    def _assert_geometry(self, db, *, writer):
        expected = {"page_size": PAGE_SIZE, "auto_vacuum": 0, "journal_mode": "wal",
                    "secure_delete": 0, "temp_store": 2, "wal_autocheckpoint": 0}
        if writer:
            expected["cache_spill"] = 0
        for pragma, value in expected.items():
            if db.execute("PRAGMA " + pragma).fetchone()[0] != value:
                raise StoreRefusalError("relay geometry mismatch: " + pragma)
        if not db.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE):
            raise StoreRefusalError("relay geometry mismatch: NO_CKPT_ON_CLOSE")
        with self.path.open("rb") as stream:
            stream.seek(20)
            if stream.read(1) != b"\0":
                raise StoreRefusalError("relay geometry mismatch: reserved bytes")

    def _assert_schema(self):
        actual = dict(self._db.execute("SELECT name,sql FROM sqlite_schema"))
        if actual != SCHEMA:
            raise StoreRefusalError("relay schema mismatch")
        for name, columns in INDEX_COLUMNS.items():
            found = tuple(row[2] for row in self._db.execute(f"PRAGMA index_xinfo('{name}')"))
            if found != columns:
                raise StoreRefusalError("relay index geometry mismatch")

    def sizes(self):
        def size(path):
            try:
                return path.stat().st_size
            except FileNotFoundError:
                return 0
        return {"db": size(self.path), "wal": size(Path(str(self.path) + "-wal")),
                "shm": size(Path(str(self.path) + "-shm"))}

    def reserved_size(self):
        sizes = self.sizes()
        return sizes["db"] + 2 * sizes["wal"] + sizes["shm"]

    def _observe(self, phase, before, **fields):
        after = self.sizes()
        event = {"phase": phase, "before": before, "after": after,
                 "peak": sum(after.values()), "sqlite_version": sqlite3.sqlite_version, **fields}
        self.observations.append(event)
        if self.observer:
            self.observer(event)
        if event["peak"] > self.cap:
            self.stopped = True
            raise GrowthStopError("relay physical ceiling exceeded")
        return event

    def _admit(self, rows):
        bound = operation_bound(rows, self.pages)
        if (not self.accepting or self.stopped or bound > OP_CAP
                or self.reserved_size() + bound > self.cap - MARGIN):
            self.stopped = True
            raise GrowthStopError("relay growth stopped")
        return bound

    def _write(self, rows, callback):
        with self._mutex:
            bound = self._admit(rows)
            before = self.sizes()
            phase = "rollback"
            try:
                self._db.execute("BEGIN IMMEDIATE")
                result = callback(self._db)
                self._db.execute("COMMIT")
                phase = "commit"
            except BaseException:
                if self._db.in_transaction:
                    self._db.execute("ROLLBACK")
                raise
            finally:
                event = self._observe(phase, before, bound=bound, charged_rows=rows)
                growth = event["after"]["wal"] + event["after"]["shm"] - before["wal"] - before["shm"]
                if growth > bound or event["after"]["db"] != before["db"]:
                    self.stopped = True
                    raise GrowthStopError("relay unobserved growth")
            if self.sizes()["wal"] > WAL_TRIGGER:
                self.barrier()
            return result

    def barrier(self):
        with self._mutex:
            was_stopped = self.stopped
            before = self.sizes()
            if self.reserved_size() > self.cap:
                self.stopped = True
                return False
            result = self._db.execute("PRAGMA wal_checkpoint(FULL)").fetchone()
            complete = result[0] == 0 and result[1] == result[2]
            self._observe("full", before, result=tuple(result), complete=complete)
            if self.sizes()["wal"] != before["wal"]:
                self.stopped = True
                raise GrowthStopError("FULL changed WAL length")
            if complete:
                before = self.sizes()
                truncate = self._db.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                event = self._observe("truncate", before, result=tuple(truncate))
                if event["after"]["db"] != before["db"]:
                    self.stopped = True
                    raise GrowthStopError("TRUNCATE copied unobserved pages")
                complete = truncate[0] == 0 and truncate[1] == truncate[2] and event["after"]["wal"] == 0
            self.stopped = (was_stopped and not complete
                            or not complete and self.sizes()["wal"] > WAL_STOP
                            or self.reserved_size() + operation_bound(self.batch, self.pages) > self.cap - MARGIN)
            return complete

    def send(self, sender, target, blob, *, now=None):
        if sender not in self.fleets or target not in self.fleets:
            raise ValueError("unknown relay fleet")
        if not isinstance(blob, bytes) or len(blob) > MAX_BLOB:
            raise ValueError("relay blob exceeds size limit")
        now = int(time.time() * 1000) if now is None else now
        ident = uuid.uuid4().hex

        def insert(db):
            count, size = db.execute("SELECT count(*),coalesce(sum(length(blob)),0) FROM messages WHERE to_fleet=?", (target,)).fetchone()
            pairs, pair_size = db.execute("SELECT count(*),coalesce(sum(length(blob)),0) FROM messages WHERE sender_fleet=? AND to_fleet=?", (sender, target)).fetchone()
            if count >= 2000 or size + len(blob) > 256 * MIB or pairs >= 1000 or pair_size + len(blob) > 128 * MIB:
                raise QueueFullError("relay queue quota exceeded")
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (ident, sender, target, blob, now, 0))
            return ident
        return self._write(1, insert)

    def poll(self, target, *, now=None):
        now = int(time.time() * 1000) if now is None else now
        with self._mutex:
            self._admit(2)
            cursor = self._db.execute("SELECT id,sender_fleet,to_fleet,blob,received_at FROM messages WHERE to_fleet=? AND lease_until<=? AND received_at>=? ORDER BY received_at,id LIMIT 20", (target, now, now - TTL_MS))
            page = []
            try:
                for ident, sender, fleet, blob, received in cursor:
                    row = {"id": ident, "sender_fleet": sender, "to_fleet": fleet,
                           "blob_b64": base64.b64encode(blob).decode("ascii"), "received_at": received}
                    if len(json.dumps({"rows": [*page, row]}, separators=(",", ":")).encode()) > MAX_PAGE:
                        break
                    page.append(row)
            finally:
                cursor.close()
            for start in range(0, len(page), self.lease_batch):
                batch = page[start:start + self.lease_batch]

                def lease(db, batch=batch):
                    for row in batch:
                        db.execute("UPDATE messages SET lease_until=? WHERE id=? AND to_fleet=? AND lease_until<=?", (now + LEASE_MS, row["id"], target, now))
                self._write(2 * len(batch), lease)
            return page

    def ack(self, target, identifiers):
        self._admit(1)
        deleted = 0
        identifiers = list(dict.fromkeys(identifiers))
        for start in range(0, len(identifiers), self.batch):
            batch = identifiers[start:start + self.batch]

            def remove(db, batch=batch):
                marks = ",".join("?" for _ in batch)
                return db.execute(f"DELETE FROM messages WHERE to_fleet=? AND id IN ({marks})", (target, *batch)).rowcount
            deleted += self._write(len(batch), remove)
        return deleted

    def sweep(self, *, now=None):
        now = int(time.time() * 1000) if now is None else now
        with self._mutex:
            ids = [row[0] for row in self._db.execute("SELECT id FROM messages WHERE received_at<? ORDER BY received_at,id LIMIT ?", (now - TTL_MS, self.batch))]
            if not ids:
                self.barrier()
                return 0

            def remove(db):
                marks = ",".join("?" for _ in ids)
                return db.execute(f"DELETE FROM messages WHERE id IN ({marks})", ids).rowcount
            count = self._write(len(ids), remove)
            self.barrier()
            return count

    @contextmanager
    def reader(self):
        db = self._open(writer=False)
        self._assert_geometry(db, writer=False)
        db.execute("PRAGMA query_only=ON")
        self._readers.add(db)
        try:
            db.execute("BEGIN")
            yield db
        finally:
            if db in self._readers:
                db.rollback()
                self._readers.remove(db)
                before = self.sizes()
                db.close()
                self._observe("reader-close", before)

    def _close_connections(self):
        for db in list(self._readers):
            before = self.sizes()
            db.close()
            self._readers.remove(db)
            self._observe("reader-close", before)
        if self._db is not None:
            before = self.sizes()
            self._db.close()
            self._db = None
            self._observe("writer-close", before)
        if self._owner is not None:
            os.close(self._owner)
            self._owner = None
        self.closed = True

    def close(self):
        with self._mutex:
            if self.closed:
                return
            self.accepting = False
            try:
                self.barrier()
            finally:
                self._close_connections()
