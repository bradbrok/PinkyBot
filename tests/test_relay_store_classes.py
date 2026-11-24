"""Exact non-STRICT schemas still refuse rows outside the growth premises."""

import sqlite3
import sys
from contextlib import closing

import pytest

from pinky_relay.store import MIB, QueueStore, StoreRefusalError

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="relay requires Python >= 3.12")
FLEETS = ("local", "peer-one")
COLUMNS = ("id", "sender_fleet", "to_fleet", "blob", "received_at", "lease_until")
CLASSES = ("text", "text", "text", "blob", "integer", "integer")


def _fixture(path, column=None, value=None):
    store = QueueStore(path, FLEETS, disk_cap=96 * MIB)
    store.close()
    row = ["synthetic-row", "peer-one", "local", b"synthetic-sealed-blob", 100, 0]
    if column is not None:
        row[COLUMNS.index(column)] = value
    db = sqlite3.connect(path, isolation_level=None)
    db.setconfig(sqlite3.SQLITE_DBCONFIG_NO_CKPT_ON_CLOSE, True)
    db.execute("PRAGMA wal_autocheckpoint=0")
    db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", row)
    return db


def _classes(db):
    return db.execute("SELECT " + ",".join("typeof(" + c + ")" for c in COLUMNS)
                      + " FROM messages").fetchone()


@pytest.mark.parametrize("column,value,storage_class", [
    ("id", b"synthetic-row", "blob"),
    ("sender_fleet", b"peer-one", "blob"),
    ("to_fleet", b"local", "blob"),
    ("blob", "synthetic-text-blob", "text"),
    ("blob", 7, "integer"),
    ("blob", 1.5, "real"),
    ("received_at", "synthetic-text-time", "text"),
    ("received_at", 1.5, "real"),
    ("received_at", b"100", "blob"),
    ("lease_until", "synthetic-text-lease", "text"),
    ("lease_until", 1.5, "real"),
    ("lease_until", b"0", "blob"),
], ids=["id-blob", "sender-blob", "target-blob", "blob-text", "blob-integer", "blob-real",
        "received-text", "received-real", "received-blob", "lease-text", "lease-real", "lease-blob"])
def test_startup_refuses_each_wrong_storage_class_without_rewriting(tmp_path, column, value, storage_class):
    path = tmp_path / "queue.db"
    with closing(_fixture(path, column, value)) as db:
        before_classes = _classes(db)
        assert before_classes[COLUMNS.index(column)] == storage_class
        before = {p: p.read_bytes() for p in (path, path.with_name(path.name + "-wal"))}
        outcome = None
        try:
            opened = QueueStore(path, FLEETS, disk_cap=96 * MIB)
        except Exception as exc:
            outcome = exc
        else:
            opened.close()
        assert isinstance(outcome, StoreRefusalError), "wrong storage class was not a fixed store refusal"
        assert str(outcome) == "stored relay row violates geometry"
        assert _classes(db) == before_classes
        assert {p: p.read_bytes() for p in before} == before, "refused store rewrote database or WAL"


def test_valid_storage_classes_open_with_the_original_row(tmp_path):
    path = tmp_path / "queue.db"
    with closing(_fixture(path)) as db:
        assert _classes(db) == CLASSES
    store = QueueStore(path, FLEETS, disk_cap=96 * MIB)
    try:
        assert _classes(store._db) == CLASSES
        assert store._db.execute("SELECT id,blob FROM messages").fetchall() == [
            ("synthetic-row", b"synthetic-sealed-blob"),
        ]
    finally:
        store.close()


async def test_real_send_and_poll_rows_reopen_with_valid_storage_classes(tmp_path):
    from pinky_relay.app import create_relay_app
    from tests.test_relay_service import TOKENS, encoded, request, service

    with service(tmp_path) as (app, store):
        config = app.state.relay.config
        first = await request(app, "/v1/send", method="POST", body=encoded(target="receiver"))
        second = await request(app, "/v1/send", method="POST", body=encoded(target="peer-two"))
        assert first[0] == second[0] == 200
        leased_id, unleased_id = first[1]["id"], second[1]["id"]
        assert leased_id != unleased_id
        page = await request(app, "/v1/poll", token=TOKENS["receiver"])
        assert page[0] == 200 and [row["id"] for row in page[1]["rows"]] == [leased_id]
        with store.reader() as reader:
            before = reader.execute("SELECT id,lease_until FROM messages ORDER BY id").fetchall()
        assert set(dict(before)) == {leased_id, unleased_id}
        assert dict(before)[leased_id] > 0 and dict(before)[unleased_id] == 0

    reopened = None
    refusal = None
    try:
        try:
            reopened = QueueStore(tmp_path / "queue.db", config.digests, disk_cap=96 * MIB)
        except StoreRefusalError as exc:
            refusal = exc
        assert refusal is None, "production send/poll rows refused on reopen: " + str(refusal)
        assert reopened is not None
        with reopened.reader() as reader:
            assert reader.execute("SELECT id,lease_until FROM messages ORDER BY id").fetchall() == before
            rows = reader.execute("SELECT id," + ",".join("typeof(" + c + ")" for c in COLUMNS)
                                  + " FROM messages ORDER BY id").fetchall()
        assert [row[0] for row in rows] == sorted([leased_id, unleased_id])
        assert [row[1:] for row in rows] == [CLASSES, CLASSES]
        app = create_relay_app(config, reopened)
        page = await request(app, "/v1/poll", token=TOKENS["peer-two"])
        assert page[0] == 200 and [row["id"] for row in page[1]["rows"]] == [unleased_id]
    finally:
        if reopened is not None:
            reopened.close()
