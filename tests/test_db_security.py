"""Tests for SQLite filesystem hardening.

Covers ``pinky_daemon.db_security`` (the startup sweep + per-file helper)
and the chmod-on-init retrofit in the crown-jewel identity stores.
"""

from __future__ import annotations

import os
import secrets
import sqlite3
import stat
from pathlib import Path

from pinky_daemon.db_security import (
    DB_FILE_MODE,
    SECRET_DIR_MODE,
    harden_db_file,
    sweep_db_permissions,
)
from pinky_identity.fs_security import harden_secret_file


def _mode(p) -> int:
    return stat.S_IMODE(os.stat(p).st_mode)


def _mk(path: Path, mode: int = 0o644) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"x")
    os.chmod(path, mode)
    return path


# -- harden_db_file ----------------------------------------------------------


def test_harden_db_file_locks_0644_to_0600(tmp_path):
    db = _mk(tmp_path / "x.db", 0o644)
    assert harden_db_file(db) == 1
    assert _mode(db) == DB_FILE_MODE


def test_harden_db_file_locks_sidecars(tmp_path):
    db = _mk(tmp_path / "x.db", 0o644)
    wal = _mk(tmp_path / "x.db-wal", 0o644)
    shm = _mk(tmp_path / "x.db-shm", 0o644)
    assert harden_db_file(db) == 3
    assert _mode(db) == _mode(wal) == _mode(shm) == DB_FILE_MODE


def test_harden_db_file_idempotent(tmp_path):
    db = _mk(tmp_path / "x.db", 0o644)
    assert harden_db_file(db) == 1
    assert harden_db_file(db) == 0  # already locked → no change
    assert _mode(db) == DB_FILE_MODE


def test_harden_db_file_missing_is_noop(tmp_path):
    assert harden_db_file(tmp_path / "nope.db") == 0


# -- sweep_db_permissions ----------------------------------------------------


def test_sweep_recurses_and_locks_all_dbs(tmp_path):
    a = _mk(tmp_path / "a.db", 0o644)
    b = _mk(tmp_path / "sub" / "b.db", 0o666)
    c = _mk(tmp_path / "agents" / "x" / "data" / "memory.db", 0o644)
    other = _mk(tmp_path / "notes.txt", 0o644)  # non-db, must be left alone
    assert sweep_db_permissions(tmp_path) == 3
    assert _mode(a) == _mode(b) == _mode(c) == DB_FILE_MODE
    assert _mode(other) == 0o644


def test_sweep_tightens_identity_dir(tmp_path):
    idir = tmp_path / "identity"
    idir.mkdir()
    os.chmod(idir, 0o755)
    _mk(idir / "signer_store.db", 0o644)
    sweep_db_permissions(tmp_path)
    assert _mode(idir) == SECRET_DIR_MODE


def test_sweep_missing_dir_is_noop(tmp_path):
    assert sweep_db_permissions(tmp_path / "nope") == 0


def test_sweep_idempotent(tmp_path):
    _mk(tmp_path / "a.db", 0o644)
    assert sweep_db_permissions(tmp_path) >= 1
    assert sweep_db_permissions(tmp_path) == 0


def test_sweep_does_not_chmod_through_symlink(tmp_path):
    # A .db symlink must not have its target chmod'd (would retarget the link).
    target = _mk(tmp_path / "outside" / "real.db", 0o644)
    data = tmp_path / "data"
    data.mkdir()
    (data / "link.db").symlink_to(target)
    sweep_db_permissions(data)
    assert _mode(target) == 0o644


# -- crown-jewel stores lock their DB on init --------------------------------


def test_signer_store_locks_db_on_init(tmp_path):
    from pinky_identity.keystore import DEVICE_KEY_BYTES, DeviceKey
    from pinky_identity.signer_store import EncryptedSignerStore

    dk = DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))
    db = tmp_path / "ss.db"
    store = EncryptedSignerStore(db_path=db, device_key=dk)
    try:
        assert _mode(db) == DB_FILE_MODE
    finally:
        store.close()


def test_bearer_token_store_locks_db_on_init(tmp_path):
    from pinky_identity.bearer_tokens import BearerTokenStore

    db = tmp_path / "bearer.db"
    store = BearerTokenStore(db_path=db)
    try:
        assert _mode(db) == DB_FILE_MODE
    finally:
        store.close()


# -- symlink safety (no chmod through a link) --------------------------------


def _real_db(path: Path, mode: int = 0o644) -> Path:
    """Create a valid (empty) SQLite DB at ``path`` with the given mode."""
    path.parent.mkdir(parents=True, exist_ok=True)
    sqlite3.connect(str(path)).close()
    os.chmod(path, mode)
    return path


def test_harden_secret_file_skips_symlink(tmp_path):
    target = _mk(tmp_path / "real.db", 0o644)
    link = tmp_path / "link.db"
    link.symlink_to(target)
    assert harden_secret_file(link) == 0  # refused to open through the link
    assert _mode(target) == 0o644  # target untouched


def test_signer_store_does_not_harden_through_symlink(tmp_path):
    from pinky_identity.keystore import DEVICE_KEY_BYTES, DeviceKey
    from pinky_identity.signer_store import EncryptedSignerStore

    victim = _real_db(tmp_path / "outside" / "victim.db", 0o644)
    link = tmp_path / "identity" / "ss.db"
    link.parent.mkdir(parents=True)
    link.symlink_to(victim)

    dk = DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))
    store = EncryptedSignerStore(db_path=link, device_key=dk)
    try:
        # Init hardens its path, but must not follow the link to chmod victim.
        assert _mode(victim) == 0o644
    finally:
        store.close()


def test_bearer_token_store_does_not_harden_through_symlink(tmp_path):
    from pinky_identity.bearer_tokens import BearerTokenStore

    victim = _real_db(tmp_path / "outside" / "victim.db", 0o644)
    link = tmp_path / "identity" / "bearer.db"
    link.parent.mkdir(parents=True)
    link.symlink_to(victim)

    store = BearerTokenStore(db_path=link)
    try:
        assert _mode(victim) == 0o644
    finally:
        store.close()
