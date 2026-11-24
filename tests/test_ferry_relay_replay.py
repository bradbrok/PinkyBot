"""Durable at-most-once markers survive restarts and hostile wall clocks."""

import sqlite3
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from pinky_daemon.ferry.relay_replay import (
    GLOBAL_QUOTA,
    PEER_QUOTA,
    RETAIN_MS,
    ReplayStore,
    ReplayStoreError,
    floor_command,
)


class Clock:
    def __init__(self):
        self.wall = 1_800_000_000_000
        self.mono = 0

    def wall_ms(self):
        return self.wall

    def monotonic_ns(self):
        return self.mono

    def advance(self, ms):
        self.wall += ms
        self.mono += ms * 1_000_000


def store_at(path, clock):
    return ReplayStore(path, wall_ms=clock.wall_ms, monotonic_ns=clock.monotonic_ns)


def claim(store, *, sender=b"a" * 16, ident="inner-one", digest=b"d" * 32, expires=None):
    return store.claim(sender, ident, digest, "row-one", expires or store.wall_ms() + 10_000, store.wall_ms())


def test_restart_recovers_only_claimed_as_retry_and_handoff_as_unknown(tmp_path):
    clock = Clock()
    path = tmp_path / "replay.db"
    store = store_at(path, clock)
    assert claim(store) == "claimed"
    assert claim(store, ident="not-handed-off") == "claimed"
    assert store.handoff((b"a" * 16, "inner-one"))
    assert store._db.execute("SELECT state FROM relay_claims WHERE inner_id='inner-one'").fetchone() == ("HANDING_OFF",)
    store.close()
    resumed = store_at(path, clock)
    try:
        states = dict(resumed._db.execute("SELECT inner_id,state FROM relay_claims"))
        assert states == {"inner-one": "UNKNOWN", "not-handed-off": "RETRY"}
        assert claim(resumed) == "terminal"
        assert claim(resumed, ident="not-handed-off") == "claimed"
    finally:
        resumed.close()


def test_two_sender_namespace_and_equivocation_never_overwrite_claim(tmp_path):
    store = store_at(tmp_path / "replay.db", Clock())
    try:
        assert claim(store) == "claimed"
        assert claim(store, sender=b"b" * 16) == "claimed"
        assert claim(store, digest=b"x" * 32) == "equivocation"
        rows = store._db.execute("SELECT sender_fp,digest,state FROM relay_claims").fetchall()
        assert set(rows) == {(b"a" * 16, b"d" * 32, "CLAIMED"), (b"b" * 16, b"d" * 32, "CLAIMED")}
    finally:
        store.close()


def test_two_simultaneous_due_retry_acquisitions_have_one_owner(tmp_path):
    clock = Clock()
    store = store_at(tmp_path / "replay.db", clock)
    try:
        assert claim(store) == "claimed" and store.handoff((b"a" * 16, "inner-one"))
        assert store.retry((b"a" * 16, "inner-one")) == "RETRY"
        assert claim(store) == "busy"
        clock.advance(1000)
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: claim(store), range(2)))
        assert sorted(results) == ["busy", "claimed"]
        assert store.handoff((b"a" * 16, "inner-one"))
        assert not store.handoff((b"a" * 16, "inner-one"))
    finally:
        store.close()


def test_quota_check_and_insert_are_atomic_without_eviction(tmp_path, monkeypatch):
    import pinky_daemon.ferry.relay_replay as module

    assert PEER_QUOTA == 50_000 and GLOBAL_QUOTA == 200_000
    monkeypatch.setattr(module, "PEER_QUOTA", 1)
    monkeypatch.setattr(module, "GLOBAL_QUOTA", 2)
    store = store_at(tmp_path / "replay.db", Clock())
    try:
        assert claim(store) == "claimed"
        before = store._db.execute("SELECT * FROM relay_claims").fetchall()
        assert claim(store, ident="second") == "quota"
        assert store._db.execute("SELECT * FROM relay_claims").fetchall() == before
        assert claim(store, sender=b"b" * 16) == "claimed"
        assert claim(store, sender=b"c" * 16) == "quota"
        assert store._db.execute("SELECT count(*) FROM relay_claims").fetchone() == (2,)
    finally:
        store.close()


def test_single_receiver_process_lock_and_owner_row(tmp_path):
    clock = Clock()
    path = tmp_path / "replay.db"
    store = store_at(path, clock)
    try:
        with pytest.raises(ReplayStoreError, match="already owned"):
            store_at(path, clock)
        store._db.execute("UPDATE relay_floor SET owner='different'")
        with pytest.raises(ReplayStoreError, match="ownership changed"):
            store.clock()
    finally:
        store.close()
    resumed = store_at(path, clock)
    resumed.close()


def test_second_process_cannot_own_the_same_receiver(tmp_path):
    path = tmp_path / "replay.db"
    store = store_at(path, Clock())
    try:
        code = """import sys
sys.path.insert(0, sys.argv[1])
from pinky_daemon.ferry.relay_replay import ReplayStore, ReplayStoreError
try:
    replay = ReplayStore(sys.argv[2])
except ReplayStoreError:
    print('refused')
else:
    replay.close()
    print('unexpected owner')
"""
        child = subprocess.run([sys.executable, "-I", "-B", "-c", code,
                                str(Path(__file__).resolve().parents[1] / "src"), str(path)],
                               text=True, capture_output=True, timeout=5)
        assert child.returncode == 0 and child.stdout.strip() == "refused"
    finally:
        store.close()


@pytest.mark.parametrize("restart", [False, True])
def test_actual_prune_then_clock_rollback_cannot_reaccept(tmp_path, restart):
    clock = Clock()
    path = tmp_path / "replay.db"
    store = store_at(path, clock)
    expires = clock.wall + 10_000
    try:
        assert claim(store, expires=expires) == "claimed"
        assert store.handoff((b"a" * 16, "inner-one"))
        store.finish((b"a" * 16, "inner-one"), "DELIVERED")
        clock.advance(10_000 + RETAIN_MS)
        assert store.prune() == 0, "strict floor boundary must retain the row"
        clock.advance(1)
        assert store.prune() == 1
        assert store._db.execute("SELECT count(*) FROM relay_claims").fetchone() == (0,)
        old_floor = store.clock()["P"]
        assert store.clock()["H"] == expires + RETAIN_MS
        clock.wall = expires - 1000
        if restart:
            store.close()
            store = store_at(path, clock)
        assert store.clock()["P"] == old_floor
        assert store.clock()["state"] == "clock behind floor"
        assert claim(store, expires=expires) == "clock"
        assert store._db.execute("SELECT count(*) FROM relay_claims").fetchone() == (0,)
    finally:
        store.close()


def test_retention_uses_expiry_not_first_seen_and_ttl_boundary(tmp_path):
    clock = Clock()
    store = store_at(tmp_path / "replay.db", clock)
    expires = clock.wall + 7 * 86_400_000
    try:
        assert claim(store, expires=expires) == "claimed"
        clock.advance(RETAIN_MS + 1000)
        assert store.prune() == 0, "first-seen retention would delete this live record"
        clock.advance(expires + RETAIN_MS - 1000 - clock.wall)
        assert store.prune() == 0
        clock.advance(1001)
        assert store.prune() == 1
    finally:
        store.close()


def test_wall_jump_at_warmup_restart_and_many_updates_never_advances_floor(tmp_path):
    clock = Clock()
    path = tmp_path / "replay.db"
    store = store_at(path, clock)
    initial = clock.wall
    try:
        assert claim(store) == "claimed"
        clock.wall += 100 * 86_400_000
        for _ in range(100):
            clock.mono += 100_000  # Sub-millisecond parts must accumulate durably.
            assert store.prune() == 0
        assert store.clock()["P"] == initial + 10
        assert store.clock()["state"] == "clock ahead or long downtime"
        store.close()
        clock.mono += 10 * 86_400_000 * 1_000_000  # Time while stopped is not earned.
        store = store_at(path, clock)
        assert store.clock()["P"] == initial + 10
        assert store.prune() == 0
        assert store._db.execute("SELECT count(*) FROM relay_claims").fetchone() == (1,)
    finally:
        store.close()


def test_floor_operator_is_the_only_forward_wall_catchup(tmp_path, monkeypatch, capsys):
    from pinky_cli.__main__ import main

    clock = Clock()
    path = tmp_path / "replay.db"
    store = store_at(path, clock)
    try:
        original = store.clock()["P"]
        with pytest.raises(ReplayStoreError):
            floor_command(path, reset=True, confirm=True, wall_ms=lambda: original - 1)
        with pytest.raises(ReplayStoreError):
            floor_command(path, reset=True, wall_ms=lambda: original + 1)
        assert floor_command(path, reset=True, confirm=True, wall_ms=lambda: original + 1000)["P"] == original + 1000
        assert store.clock()["P"] == original + 1000
        monkeypatch.setattr("sys.argv", ["pinky", "ferry", "relay-floor", "--show", "--store", str(path)])
        main()
        assert '"P"' in capsys.readouterr().out
    finally:
        store.close()


def test_busy_timeout_is_bounded_and_corruption_is_not_reinitialized(tmp_path):
    clock = Clock()
    path = tmp_path / "replay.db"
    store = store_at(path, clock)
    assert store._db.execute("PRAGMA busy_timeout").fetchone() == (50,)
    store.close()
    db = sqlite3.connect(path)
    db.execute("DELETE FROM relay_floor")
    db.commit()
    db.close()
    with pytest.raises(ReplayStoreError, match="missing"):
        store_at(path, clock)


def test_replay_catalog_integrity_failure_never_recommends_stale_restore_or_rebuild(tmp_path):
    from pinky_daemon.store_catalog import StoreCatalog, StoreCatalogError, StoreIntegrityTarget

    path = tmp_path / "replay.db"
    target = StoreIntegrityTarget("ferry_replay", str(path), recovery="manual", journal_mode="wal")
    catalog = StoreCatalog(expected_root=tmp_path, manifest={"ferry_replay": target})
    replay = ReplayStore(path, catalog=catalog)
    try:
        catalog.validate()
        assert catalog.snapshot()[0].recovery == "manual"
        with pytest.raises(StoreCatalogError) as error:
            catalog._raise_integrity_error([target], str(path), "synthetic corruption")
        text = str(error.value)
        assert "must not be rebuilt or restored from a stale snapshot" in text
        assert "Delete the derived store" not in text and "Restore the authoritative store" not in text
    finally:
        replay.close()
        catalog.close()
