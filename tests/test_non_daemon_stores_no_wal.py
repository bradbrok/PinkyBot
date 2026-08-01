"""The stores outside ``pinky_daemon`` must not run on WAL either.

The orphaned-WAL failure mode documented in :mod:`pinky_daemon.sqlite_journal`
is a property of *how the process holds SQLite open*, not of which package the
store lives in. These four modules keep a long-lived
``check_same_thread=False`` connection open for the life of the process —
exactly the shape that loses committed writes when an outside opener unlinks
the ``-wal``.

The daemon stores are pinned by their own per-store tests; this file pins the
remainder so the contract cannot regress package by package.
"""

from __future__ import annotations

import secrets
import sqlite3

import pytest

from pinky_hub.hub_store import HubStore
from pinky_identity.bearer_tokens import BearerTokenStore
from pinky_identity.keystore import DEVICE_KEY_BYTES, DeviceKey
from pinky_identity.signer_store import EncryptedSignerStore
from pinky_memory.store import ReflectionStore


def _mode(conn: sqlite3.Connection) -> str:
    return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()


def _mode_on_disk(path) -> str:
    """Journal mode a *fresh* opener sees.

    Only WAL is sticky in the database header; TRUNCATE is a per-connection
    setting, so an independent connection reports the ``delete`` default. What
    matters for this contract is simply that it is not ``wal``.
    """
    conn = sqlite3.connect(str(path))
    try:
        return str(conn.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    finally:
        conn.close()


@pytest.fixture
def device_key():
    return DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))


def test_hub_store_uses_rollback_journalling(tmp_path):
    store = HubStore(db_path=str(tmp_path / "hub.db"))
    assert _mode(store._db) == "truncate"  # noqa: SLF001


def test_bearer_token_store_uses_rollback_journalling(tmp_path):
    store = BearerTokenStore(db_path=tmp_path / "bearer.db")
    assert _mode(store._db) == "truncate"  # noqa: SLF001


def test_signer_store_uses_rollback_journalling(tmp_path, device_key):
    store = EncryptedSignerStore(db_path=tmp_path / "signer.db", device_key=device_key)
    assert _mode(store._db) == "truncate"  # noqa: SLF001


def test_reflection_store_uses_rollback_journalling(tmp_path):
    store = ReflectionStore(db_path=str(tmp_path / "reflections.db"))
    assert _mode(store._conn) == "truncate"  # noqa: SLF001


def test_reflection_store_reopen_does_not_fall_back_to_wal(tmp_path):
    """``reopen()`` builds a fresh connection — it must configure it too."""
    store = ReflectionStore(db_path=str(tmp_path / "reflections.db"))
    store.reopen()
    assert _mode(store._conn) == "truncate"  # noqa: SLF001


def test_no_wal_sidecars_are_created(tmp_path):
    """Rollback journalling leaves no ``-wal``/``-shm`` for anyone to unlink."""
    path = tmp_path / "hub.db"
    store = HubStore(db_path=str(path))
    store.register_instance(
        label="alpha", url="https://example.invalid", api_key="k"
    )

    assert not path.with_name(path.name + "-wal").exists()
    assert not path.with_name(path.name + "-shm").exists()


def test_existing_wal_database_is_converted_in_place(tmp_path):
    """A store opened on a database left in WAL by an older build migrates it."""
    path = tmp_path / "hub.db"
    seed = sqlite3.connect(str(path))
    try:
        seed.execute("PRAGMA journal_mode=WAL")
        seed.execute("CREATE TABLE probe (id INTEGER PRIMARY KEY)")
        seed.commit()
    finally:
        seed.close()
    assert _mode_on_disk(path) == "wal"

    store = HubStore(db_path=str(path))

    assert _mode(store._db) == "truncate"  # noqa: SLF001
    assert _mode_on_disk(path) != "wal"
