"""Dirty-page admission and checkpoint observations bound real queue files."""

import json
import sqlite3
import sys
from contextlib import contextmanager

import pytest

from pinky_relay.store import (
    DISK_CAP,
    MARGIN,
    MAX_BLOB,
    MIB,
    OP_CAP,
    SCHEMA,
    TTL_MS,
    WAL_STOP,
    GrowthStopError,
    QueueFullError,
    QueueStore,
    StoreRefusalError,
    operation_bound,
    row_pages,
)

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="relay requires Python >= 3.12")
FLEETS = ("peer-one", "peer-two", "receiver")


@contextmanager
def queue(tmp_path, **kwargs):
    store = QueueStore(tmp_path / "queue.db", kwargs.pop("fleets", FLEETS),
                       disk_cap=kwargs.pop("disk_cap", 96 * MIB), **kwargs)
    try:
        yield store
    finally:
        store.close()


def _refused_open(path, fleets=FLEETS, **kwargs):
    try:
        store = QueueStore(path, fleets, disk_cap=96 * MIB, **kwargs)
    except (StoreRefusalError, ValueError, sqlite3.DatabaseError):
        return True
    store.close()
    return False


def test_source_derived_bounds_and_production_ceiling():
    assert DISK_CAP == 2 * 1024**3
    assert MARGIN == OP_CAP == 16 * MIB
    assert row_pages(8) == 1188
    assert [operation_bound(n, 1188) for n in range(1, 5)] == [
        4_968_368, 9_862_928, 14_757_488, 19_684_816,
    ]
    assert row_pages(856) > row_pages(8)


def test_pinned_schema_counts_the_implicit_primary_key_tree(tmp_path):
    with queue(tmp_path) as q:
        assert q.batch == 3 and q.lease_batch == 1
        assert q._db.execute("SELECT count(*) FROM sqlite_schema WHERE type IN ('table','index')").fetchone()[0] == 5
        assert q._db.execute("PRAGMA auto_vacuum").fetchone()[0] == 0
        assert q._db.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE)
        with q.reader() as reader:
            assert reader.getconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE)


@pytest.mark.parametrize("hostile", ["secure_delete=ON", "temp_store=FILE", "wal_autocheckpoint=1000", "cache_spill=ON"])
def test_creation_corrects_injected_hostile_defaults_before_schema(tmp_path, hostile):
    def connect(*args, **kwargs):
        db = sqlite3.connect(*args, **kwargs)
        db.execute("PRAGMA " + hostile)
        return db

    with queue(tmp_path, connection=connect) as q:
        q._assert_geometry(q._db, writer=True)
        assert q._db.execute("PRAGMA auto_vacuum").fetchone()[0] == 0


@pytest.mark.parametrize("pragma", ["page_size=8192", "page_size=65536", "auto_vacuum=FULL", "auto_vacuum=INCREMENTAL"])
def test_existing_incompatible_geometry_is_refused_not_rewritten(tmp_path, pragma):
    path = tmp_path / "queue.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA " + pragma)
        db.execute("PRAGMA journal_mode=WAL")
        for statement in SCHEMA.values():
            if statement:
                db.execute(statement)
    assert _refused_open(path)


@pytest.mark.parametrize("pragma", ["secure_delete=ON", "temp_store=FILE", "wal_autocheckpoint=1000", "cache_spill=ON"])
def test_live_pin_readback_rejects_every_connection_setting(tmp_path, pragma):
    with queue(tmp_path) as q:
        q._db.execute("PRAGMA " + pragma)
        refused = False
        try:
            q._assert_geometry(q._db, writer=True)
        except StoreRefusalError:
            refused = True
        assert refused, "a changed connection pin was accepted"
        q._db.execute("PRAGMA " + {"secure_delete=ON": "secure_delete=OFF", "temp_store=FILE": "temp_store=MEMORY", "wal_autocheckpoint=1000": "wal_autocheckpoint=0", "cache_spill=ON": "cache_spill=OFF"}[pragma])


def test_no_checkpoint_flag_must_read_back_true(tmp_path):
    class Connection(sqlite3.Connection):
        def getconfig(self, operation):
            if operation == sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE:
                return False
            return super().getconfig(operation)

    assert _refused_open(tmp_path / "queue.db", connection=lambda *a, **kw: sqlite3.connect(*a, factory=Connection, **kw))


@pytest.mark.parametrize("change", [
    "CREATE INDEX unpinned ON messages(lease_until)",
    "CREATE TABLE unpinned(value TEXT)",
    "CREATE VIEW unpinned AS SELECT id FROM messages",
    "CREATE TRIGGER unpinned AFTER INSERT ON messages BEGIN SELECT 1; END",
    "ALTER TABLE messages ADD COLUMN unpinned TEXT",
])
def test_schema_drift_is_refused(tmp_path, change):
    path = tmp_path / "queue.db"
    with queue(tmp_path):
        pass
    with sqlite3.connect(path) as db:
        db.execute(change)
    assert _refused_open(path)



def test_creation_geometry_is_set_before_any_other_sql(tmp_path):
    statements = []

    class Connection(sqlite3.Connection):
        def execute(self, statement, *args, **kwargs):
            statements.append(statement)
            return super().execute(statement, *args, **kwargs)

    with queue(tmp_path, connection=lambda *a, **kw: sqlite3.connect(*a, factory=Connection, **kw)):
        assert statements[:2] == ["PRAGMA page_size=4096", "PRAGMA auto_vacuum=NONE"]


def test_only_one_writer_owns_the_queue(tmp_path):
    with queue(tmp_path):
        assert _refused_open(tmp_path / "queue.db")


def test_overflowing_configured_names_either_fit_raised_bounds_or_refuse(tmp_path):
    with queue(tmp_path, fleets=("a" * 856, "b" * 856)) as q:
        assert q.fleet_bytes == 856
        assert q.pages > 1188 and q.batch >= 1 and q.lease_batch >= 1
    assert _refused_open(tmp_path / "oversized.db", fleets=("a" * 10000,))


def test_token_destination_lease_order_page_bytes_and_ack_scope(tmp_path):
    with queue(tmp_path) as q:
        ids = [q.send("peer-one", "receiver", bytes([i]) * MAX_BLOB, now=i + 1) for i in range(6)]
        other = q.send("peer-one", "peer-two", b"private", now=0)
        rows = q.poll("receiver", now=10)
        assert [r["id"] for r in rows] == ids[:2]
        assert len(json.dumps({"rows": rows}, separators=(",", ":")).encode()) <= MIB
        assert q.ack("peer-two", ids) == 0
        assert q.ack("receiver", [other]) == 0
        assert [r["id"] for r in q.poll("receiver", now=11)] == ids[2:4]
        assert [r["id"] for r in q.poll("receiver", now=60010)] == ids[:2]
        assert q.ack("receiver", ids) == 6
        assert q.ack("receiver", ids) == 0


def test_small_rows_poll_at_most_twenty_then_remain_leased(tmp_path):
    with queue(tmp_path) as q:
        for i in range(25):
            q.send("peer-one", "receiver", b"x", now=i)
        assert len(q.poll("receiver", now=30)) == 20
        assert len(q.poll("receiver", now=31)) == 5
        assert q.poll("receiver", now=32) == []


def test_expiry_sweeps_only_its_derived_batch_through_the_ttl_index(tmp_path):
    with queue(tmp_path) as q:
        for i in range(9):
            q.send("peer-one", "receiver", b"expired", now=i)
        q.send("peer-one", "receiver", b"live", now=TTL_MS)
        assert q.sweep(now=TTL_MS + 10) == 3
        assert q._db.execute("SELECT count(*) FROM messages").fetchone()[0] == 7
        assert q.observations[-2]["phase"] == "full"
        assert q.observations[-1]["phase"] == "truncate"
        assert len(q.poll("receiver", now=TTL_MS + 10)) == 1


def test_admission_equality_and_one_byte_over_including_double_wal(tmp_path, monkeypatch):
    with queue(tmp_path) as q:
        bound = operation_bound(1, q.pages)
        wal = MIB
        exact_db = q.cap - MARGIN - bound - 2 * wal - 32768
        monkeypatch.setattr(q, "sizes", lambda: {"db": exact_db, "wal": wal, "shm": 32768})
        assert q._admit(1) == bound
        monkeypatch.setattr(q, "sizes", lambda: {"db": exact_db + 1, "wal": wal, "shm": 32768})
        refused = False
        try:
            q._admit(1)
        except GrowthStopError:
            refused = True
        assert refused and q.stopped
        monkeypatch.undo()


def test_actual_commit_deltas_fit_bounds_for_max_blob_lease_ack_and_sweep(tmp_path):
    seen = []
    with queue(tmp_path, observer=seen.append) as q:
        ids = [q.send("peer-one", "receiver", b"x" * MAX_BLOB, now=i) for i in range(7)]
        q.poll("receiver", now=10)
        q.ack("receiver", ids[:q.batch])
        q.sweep(now=TTL_MS + 100)
        commits = [e for e in seen if e["phase"] in ("commit", "rollback")]
        assert {e["charged_rows"] for e in commits} >= {1, 2, 3}
        for event in commits:
            before, after = event["before"], event["after"]
            assert after["db"] == before["db"]
            assert after["wal"] + after["shm"] - before["wal"] - before["shm"] <= event["bound"] <= OP_CAP
        assert all(e["peak"] <= 96 * MIB for e in seen)


def test_scattered_small_expired_rows_use_pages_not_summed_row_bytes(tmp_path):
    seen = []
    with queue(tmp_path, observer=seen.append) as q:
        for i in range(80):
            q.send("peer-one", "receiver", b"x", now=i)
            q.send("peer-one", "receiver", b"y" * 2800, now=TTL_MS + i)
        q.barrier()
        seen.clear()
        try:
            q.sweep(now=TTL_MS + 100)
        except GrowthStopError:
            pass
        attempts = [e for e in seen if e["phase"] in ("commit", "rollback")]
        assert attempts
        assert all(e["after"]["wal"] + e["after"]["shm"] - e["before"]["wal"] - e["before"]["shm"] <= e["bound"] for e in attempts)


def test_longest_accepted_index_keys_and_maximum_blobs_fit_their_bound(tmp_path):
    seen = []
    first, second = "a" * 856, "b" * 856
    with queue(tmp_path, fleets=(first, second), observer=seen.append) as q:
        ids = [q.send(first, second, b"x" * MAX_BLOB, now=i) for i in range(4)]
        q.poll(second, now=10)
        q.ack(second, ids)
        for e in seen:
            if e["phase"] in ("commit", "rollback"):
                assert e["after"]["wal"] + e["after"]["shm"] - e["before"]["wal"] - e["before"]["shm"] <= e["bound"] <= OP_CAP


def test_incomplete_full_requires_a_new_observed_full_after_reader_release(tmp_path):
    seen = []
    with queue(tmp_path, observer=seen.append) as q:
        reader = q.reader()
        db = reader.__enter__()
        db.execute("SELECT count(*) FROM messages").fetchone()
        q.send("peer-one", "receiver", b"x" * MAX_BLOB)
        assert q.barrier() is False
        assert seen[-1]["phase"] == "full" and not seen[-1]["complete"]
        reader.__exit__(None, None, None)
        assert q.sizes()["wal"] > 0
        assert q.barrier() is True
        assert seen[-2]["phase"] == "full" and seen[-2]["complete"]
        assert seen[-1]["phase"] == "truncate"
        assert seen[-1]["before"]["db"] == seen[-1]["after"]["db"]


def test_reader_release_between_partial_full_and_next_phase_cannot_hide_a_copy(tmp_path):
    seen = []
    active = []

    def observe(event):
        seen.append(event)
        if event["phase"] == "full" and not event["complete"] and active:
            active.pop().__exit__(None, None, None)

    with queue(tmp_path, observer=observe) as q:
        seen.clear()
        reader = q.reader()
        db = reader.__enter__()
        active.append(reader)
        db.execute("SELECT count(*) FROM messages").fetchone()
        q.send("peer-one", "receiver", b"x" * MAX_BLOB)
        complete = None
        try:
            complete = q.barrier()
        except GrowthStopError:
            pass
        assert complete is False
        assert not any(e["phase"] == "truncate" for e in seen[2:])
        assert q.barrier()
        assert all(e["before"]["db"] == e["after"]["db"] for e in seen if e["phase"] == "truncate")


def test_shutdown_with_reader_keeps_wal_and_restart_observes_copy_before_writes(tmp_path):
    seen = []
    q = QueueStore(tmp_path / "queue.db", FLEETS, disk_cap=96 * MIB, observer=seen.append)
    reader = q.reader()
    db = reader.__enter__()
    db.execute("SELECT count(*) FROM messages").fetchone()
    q.send("peer-one", "receiver", b"x" * MAX_BLOB)
    q.close()
    reader.__exit__(None, None, None)
    assert q.sizes()["wal"] > 0
    closes = [e for e in seen if e["phase"].endswith("-close")]
    assert [e["phase"] for e in closes[-2:]] == ["reader-close", "writer-close"]
    assert all(e["before"]["db"] == e["after"]["db"] for e in closes)
    restart = []
    with queue(tmp_path, observer=restart.append) as opened:
        assert restart[0]["phase"] == "full"
        assert restart[0]["after"]["db"] > restart[0]["before"]["db"]
        assert opened.sizes()["wal"] == 0


def test_busy_wal_stop_threshold_independently_of_admission_with_injected_sizes(tmp_path, monkeypatch):
    # Threshold unit at the pinned production cap; physical integration tests use96MiB.
    with queue(tmp_path, disk_cap=DISK_CAP) as q:
        reader = q.reader()
        db = reader.__enter__()
        db.execute("SELECT count(*) FROM messages").fetchone()
        q.send("peer-one", "receiver", b"x")
        real = q.sizes
        monkeypatch.setattr(q, "sizes", lambda: {**real(), "wal": WAL_STOP + 1})
        assert q.barrier() is False
        assert q.stopped
        reader.__exit__(None, None, None)
        monkeypatch.undo()
        assert q.barrier() and not q.stopped


def test_growth_stop_blocks_all_writes_until_a_complete_barrier(tmp_path):
    with queue(tmp_path) as q:
        reader = q.reader()
        db = reader.__enter__()
        db.execute("SELECT count(*) FROM messages").fetchone()
        ident = q.send("peer-one", "receiver", b"x")
        q.stopped = True
        assert q.barrier() is False and q.stopped
        for callback in (lambda: q.send("peer-one", "receiver", b"x"), lambda: q.poll("receiver"), lambda: q.ack("receiver", [ident])):
            refused = False
            try:
                callback()
            except GrowthStopError:
                refused = True
            assert refused
        reader.__exit__(None, None, None)
        assert q.barrier() and not q.stopped


@pytest.mark.parametrize("sender,target_count,pair_count", [
    ("peer-one", 1000, 1000), ("peer-two", 2000, 1000),
])
def test_row_quota_thresholds_are_exact(tmp_path, sender, target_count, pair_count):
    with queue(tmp_path, fleets=(*FLEETS, "third")) as q:
        # Construct the quota boundary as a fixture, before any measured request.
        q._db.execute("BEGIN IMMEDIATE")
        q._db.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?)", [
            (str(i), "peer-one" if i < pair_count else "peer-two", "receiver", b"x", i, 0)
            for i in range(target_count)
        ])
        q._db.execute("COMMIT")
        q.barrier()
        refused = False
        try:
            q.send(sender if target_count == 1000 else "third", "receiver", b"x")
        except QueueFullError:
            refused = True
        assert refused
        assert q._db.execute("SELECT count(*) FROM messages").fetchone()[0] == target_count


@pytest.mark.parametrize("limit,pair", [(128 * MIB, True), (256 * MIB, False)])
def test_byte_quota_thresholds_without_writing_gigabytes(tmp_path, limit, pair):
    with queue(tmp_path) as q:
        q.send("peer-one", "receiver", b"fixture")
        # Inject logical occupancy only; physical-bound tests use real file sizes.
        q._db.create_function("length", 1, lambda value: limit - 1)
        refused = False
        try:
            q.send("peer-one" if pair else "peer-two", "receiver", b"xx")
        except QueueFullError:
            refused = True
        assert refused


def test_mixed_real_traffic_and_held_reader_stays_below_small_cap_then_recovers(tmp_path):
    seen = []
    with queue(tmp_path, observer=seen.append) as q:
        reader = q.reader()
        db = reader.__enter__()
        db.execute("SELECT count(*) FROM messages").fetchone()
        identifiers = []
        stopped = False
        for i in range(400):
            try:
                identifiers.append(q.send("peer-one", "receiver", b"x" * MAX_BLOB, now=1000 + i))
                if i and i % 8 == 0:
                    rows = q.poll("receiver", now=2000 + i)
                    q.ack("receiver", [row["id"] for row in rows])
            except GrowthStopError:
                stopped = True
                break
        assert stopped and q.stopped
        assert any(e["phase"] == "full" and not e["complete"] for e in seen)
        assert all(e["peak"] <= 96 * MIB for e in seen)
        assert all(e["before"]["db"] == e["after"]["db"] for e in seen if e["phase"] in ("commit", "rollback", "truncate", "reader-close", "writer-close"))
        reader.__exit__(None, None, None)
        assert q.barrier() and not q.stopped
        assert q.send("peer-one", "receiver", b"after recovery")


def test_failed_write_attempt_is_rolled_back_and_observed(tmp_path):
    seen = []
    with queue(tmp_path, observer=seen.append) as q:
        refused = False
        try:
            q._write(1, lambda db: db.execute("INSERT INTO missing_table VALUES(1)"))
        except sqlite3.DatabaseError:
            refused = True
        assert refused and not q._db.in_transaction
        assert seen[-1]["phase"] == "rollback"
        assert seen[-1]["before"]["db"] == seen[-1]["after"]["db"]
