"""Measured boundary observations distinguish unsafe checkpoint shortcuts."""

import contextlib
import sys

import pytest

from pinky_relay.store import MAX_BLOB, MIB, GrowthStopError, QueueStore

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="relay requires Python >= 3.12")


def test_double_wal_admission_keeps_the_observed_full_copy_peak_within_cap(tmp_path):
    seen = []
    store = QueueStore(tmp_path / "queue.db", ("peer-one", "receiver"), disk_cap=96 * MIB, observer=seen.append)
    reader = store.reader()
    db = reader.__enter__()
    active = True
    try:
        db.execute("SELECT count(*) FROM messages").fetchone()
        for _ in range(400):
            try:
                store.send("peer-one", "receiver", b"x" * MAX_BLOB)
            except GrowthStopError:
                break
        assert store.stopped
        reader.__exit__(None, None, None)
        active = False
        with contextlib.suppress(GrowthStopError):
            store.barrier()
        full = [event for event in seen if event["phase"] == "full"]
        assert full and any(event["complete"] for event in full)
        assert all(event["peak"] <= 96 * MIB for event in full), "observed FULL copied beyond the cap"
    finally:
        if active:
            reader.__exit__(None, None, None)
        with contextlib.suppress(GrowthStopError):
            store.close()


def test_autocheckpoint_cannot_change_database_across_a_commit(tmp_path):
    seen = []
    store = QueueStore(tmp_path / "queue.db", ("peer-one", "receiver"), disk_cap=96 * MIB, observer=seen.append)
    try:
        for _ in range(30):
            try:
                store.send("peer-one", "receiver", b"x" * MAX_BLOB)
            except GrowthStopError:
                break
        commits = [event for event in seen if event["phase"] == "commit"]
        assert commits and sum(event["charged_rows"] for event in commits) >= 10
        assert all(event["before"]["db"] == event["after"]["db"] for event in commits), "an unobserved autocheckpoint copied during commit"
    finally:
        with contextlib.suppress(GrowthStopError):
            store.close()
