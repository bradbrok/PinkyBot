"""Sector-padding frames are budgeted even when the VFS permits them."""

import math
import sqlite3
import sys
from pathlib import Path

import pytest

from pinky_relay.store import MAX_BLOB, MIB, QueueStore, operation_bound


@pytest.mark.parametrize("rows,expected", [(1, 5_034_288), (2, 9_928_848), (3, 14_823_408)])
def test_commit_sector_padding_is_in_both_wal_and_shm_bound(rows, expected):
    frames = 2 + rows * 1188 + 16
    assert operation_bound(rows, 1188) == expected
    assert expected == 32 + frames * 4120 + 32768 * (math.ceil(frames / 4096) + 1)


def test_sector_padding_crossing_index_block_is_charged_to_shm():
    # P_op=4096 exactly; the extra frames require another index block.
    frames = 4112
    assert operation_bound(1, 4094) == 32 + frames * 4120 + 32768 * 3


@pytest.mark.skipif(sys.version_info < (3, 12), reason="relay requires Python >= 3.12")
def test_real_commit_with_psow_disabled_fits_padded_bound(tmp_path, record_property):
    opened, seen = [], []

    def connect(path, **kwargs):
        uri = Path(path).as_uri() + "?psow=0"
        opened.append(uri)
        return sqlite3.connect(uri, uri=True, **kwargs)

    store = QueueStore(tmp_path / "queue.db", ("peer-one", "receiver"),
                       disk_cap=96 * MIB, connection=connect, observer=seen.append)
    try:
        for index in range(8):
            store.send("peer-one", "receiver", b"x" * MAX_BLOB, now=index)
        assert opened and all(uri.endswith("?psow=0") for uri in opened)
        commits = [event for event in seen if event["phase"] == "commit"]
        assert len(commits) == 8
        deltas = []
        for event in commits:
            before, after = event["before"], event["after"]
            delta = after["wal"] + after["shm"] - before["wal"] - before["shm"]
            deltas.append(delta)
            assert after["db"] == before["db"]
            assert delta <= event["bound"] == operation_bound(1, store.pages)
        record_property("sqlite_version", sqlite3.sqlite_version)
        record_property("maximum_observed_wal_shm_growth", max(deltas))
        record_property("padded_operation_bound", operation_bound(1, store.pages))
    finally:
        store.close()
