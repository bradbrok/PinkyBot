"""Tests for pinky_identity.signer_store (SQLite-backed wrapped keypair store)."""

from __future__ import annotations

import secrets
import time
from pathlib import Path

import pytest

from pinky_identity import (
    SignatureError,
    fingerprint,
    generate_keypair,
)
from pinky_identity.keystore import (
    DEVICE_KEY_BYTES,
    DeviceKey,
    KeystoreError,
    wrap_keypair,
)
from pinky_identity.signer_store import (
    DEFAULT_SIGNER_STORE_PATH,
    EncryptedSignerStore,
    SignerStoreError,
)

# -- Fixtures ----------------------------------------------------------------


@pytest.fixture
def device_key():
    return DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))


@pytest.fixture
def store(tmp_path, device_key):
    s = EncryptedSignerStore(db_path=tmp_path / "ss.db", device_key=device_key)
    try:
        yield s
    finally:
        s.close()


@pytest.fixture
def keypair():
    return generate_keypair()


# -- Construction ------------------------------------------------------------


def test_construct_creates_parent_directory(tmp_path, device_key):
    nested = tmp_path / "a" / "b" / "ss.db"
    assert not nested.exists()
    s = EncryptedSignerStore(db_path=nested, device_key=device_key)
    try:
        assert nested.exists()
    finally:
        s.close()


def test_construct_rejects_non_device_key(tmp_path):
    with pytest.raises(TypeError):
        EncryptedSignerStore(db_path=tmp_path / "ss.db", device_key=b"\x00" * 32)  # type: ignore[arg-type]


def test_default_path_constant_is_under_data_identity():
    assert DEFAULT_SIGNER_STORE_PATH.startswith("data/identity/")


# -- put_keypair / get_signing_keypair ---------------------------------------


def test_put_keypair_round_trips(store, keypair):
    row = store.put_keypair(keypair)
    assert row.kid
    assert row.fingerprint == fingerprint(keypair.public)
    assert row.public_key_raw == keypair.public.raw
    assert len(row.ciphertext) == 48  # 32 seed + 16 Poly1305 tag

    recovered = store.get_signing_keypair(row.kid)
    assert recovered.public == keypair.public
    assert (
        recovered.secret.secret_bytes_insecure()
        == keypair.secret.secret_bytes_insecure()
    )


def test_put_keypair_rejects_non_keypair(store):
    with pytest.raises(TypeError):
        store.put_keypair("not a keypair")  # type: ignore[arg-type]


def test_put_wrapped_round_trips(store, keypair, device_key):
    wrapped = wrap_keypair(keypair, device_key=device_key)
    row = store.put_wrapped(wrapped)
    assert row.kid == wrapped.kid

    out = store.get_signing_keypair(wrapped.kid)
    assert out.public == keypair.public


def test_put_wrapped_rejects_non_wrapped(store):
    with pytest.raises(TypeError):
        store.put_wrapped({"kid": "abc"})  # type: ignore[arg-type]


def test_put_is_idempotent_on_kid(store, keypair):
    row1 = store.put_keypair(keypair)
    row2 = store.put_keypair(keypair)
    # Same kid, but ciphertexts differ (random nonce).
    assert row1.kid == row2.kid
    assert row1.ciphertext != row2.ciphertext
    # Only one row total.
    assert store.list_kids() == [row1.kid]
    # Still recoverable.
    assert store.get_signing_keypair(row1.kid).public == keypair.public


def test_reput_returns_persisted_created_at(store, keypair):
    """On a re-put the conflict clause keeps the original created_at; the
    returned row must echo what the DB holds, not a fresh timestamp."""
    row1 = store.put_keypair(keypair)
    time.sleep(0.02)
    row2 = store.put_keypair(keypair)
    assert row2.created_at == pytest.approx(row1.created_at)
    assert store.get_row(row2.kid).created_at == pytest.approx(row2.created_at)


# -- has / list / iter -------------------------------------------------------


def test_has_returns_false_for_unknown(store):
    assert store.has("does-not-exist") is False


def test_has_returns_true_after_put(store, keypair):
    row = store.put_keypair(keypair)
    assert store.has(row.kid) is True


def test_list_kids_orders_lexicographically(store):
    from pinky_identity import kid_from_public_key

    kps = [generate_keypair() for _ in range(3)]
    expected = {kid_from_public_key(kp.public) for kp in kps}
    for kp in kps:
        store.put_keypair(kp)
    listed = store.list_kids()
    assert listed == sorted(listed)
    assert set(listed) == expected


def test_iter_rows_yields_one_per_stored_keypair(store):
    from pinky_identity import kid_from_public_key

    kps = [generate_keypair() for _ in range(5)]
    expected = {kid_from_public_key(kp.public) for kp in kps}
    for kp in kps:
        store.put_keypair(kp)
    rows = list(store.iter_rows())
    assert len(rows) == 5
    assert {r.kid for r in rows} == expected


# -- get_row / get_row_by_fingerprint ----------------------------------------


def test_get_row_returns_full_row(store, keypair):
    inserted = store.put_keypair(keypair)
    row = store.get_row(inserted.kid)
    assert row.kid == inserted.kid
    assert row.fingerprint == inserted.fingerprint
    assert row.public_key_raw == inserted.public_key_raw
    assert row.wrap_version == inserted.wrap_version
    assert row.nonce == inserted.nonce
    assert row.ciphertext == inserted.ciphertext
    assert row.created_at == pytest.approx(inserted.created_at)


def test_get_row_raises_signer_store_error_on_miss(store):
    with pytest.raises(SignerStoreError):
        store.get_row("does-not-exist")


def test_signer_store_error_is_key_error(store):
    # generic except KeyError clauses should still catch SignerStoreError.
    with pytest.raises(KeyError):
        store.get_row("does-not-exist")


def test_get_signing_keypair_raises_on_miss(store):
    with pytest.raises(SignerStoreError):
        store.get_signing_keypair("does-not-exist")


def test_get_row_by_fingerprint_round_trips(store, keypair):
    row = store.put_keypair(keypair)
    found = store.get_row_by_fingerprint(row.fingerprint)
    assert found.kid == row.kid


def test_get_row_by_fingerprint_raises_on_miss(store):
    with pytest.raises(SignerStoreError):
        store.get_row_by_fingerprint("0" * 64)


# -- delete ------------------------------------------------------------------


def test_delete_removes_row(store, keypair):
    row = store.put_keypair(keypair)
    assert store.has(row.kid)
    deleted = store.delete(row.kid)
    assert deleted is True
    assert not store.has(row.kid)


def test_delete_returns_false_when_missing(store):
    assert store.delete("does-not-exist") is False


def test_delete_after_get_signing_keypair_makes_kid_unsignable(store, keypair):
    row = store.put_keypair(keypair)
    store.get_signing_keypair(row.kid)  # warm-up
    store.delete(row.kid)
    with pytest.raises(SignerStoreError):
        store.get_signing_keypair(row.kid)


# -- Decrypt failures --------------------------------------------------------


def test_get_signing_keypair_fails_with_wrong_device_key(tmp_path, keypair):
    dk1 = DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))
    dk2 = DeviceKey.from_bytes(secrets.token_bytes(DEVICE_KEY_BYTES))
    path = tmp_path / "ss.db"
    s1 = EncryptedSignerStore(db_path=path, device_key=dk1)
    try:
        row = s1.put_keypair(keypair)
    finally:
        s1.close()
    s2 = EncryptedSignerStore(db_path=path, device_key=dk2)
    try:
        with pytest.raises(KeystoreError):
            s2.get_signing_keypair(row.kid)
        # SignatureError catches both KeystoreError and SignerStoreError.
        with pytest.raises(SignatureError):
            s2.get_signing_keypair(row.kid)
    finally:
        s2.close()


def test_persistence_across_reopen(tmp_path, device_key, keypair):
    path = tmp_path / "ss.db"
    s1 = EncryptedSignerStore(db_path=path, device_key=device_key)
    try:
        row = s1.put_keypair(keypair)
    finally:
        s1.close()

    s2 = EncryptedSignerStore(db_path=path, device_key=device_key)
    try:
        assert s2.has(row.kid)
        recovered = s2.get_signing_keypair(row.kid)
        assert recovered.public == keypair.public
    finally:
        s2.close()


# -- Context manager ---------------------------------------------------------


def test_context_manager_closes_db(tmp_path, device_key, keypair):
    path = tmp_path / "ss.db"
    with EncryptedSignerStore(db_path=path, device_key=device_key) as s:
        s.put_keypair(keypair)
    # After exit, attempting another op via the same connection raises.
    with pytest.raises(Exception):  # noqa: BLE001 — sqlite3.ProgrammingError
        s.get_row("anything")


# -- Row repr does not leak --------------------------------------------------


def test_signer_store_row_repr_redacts_ciphertext(store, keypair):
    row = store.put_keypair(keypair)
    text = repr(row)
    assert "redacted" in text
    assert row.ciphertext.hex() not in text


def test_signer_store_row_to_wrapped_round_trips(store, keypair):
    row = store.put_keypair(keypair)
    wrapped = row.to_wrapped()
    assert wrapped.kid == row.kid
    assert wrapped.ciphertext == row.ciphertext
    assert wrapped.public_key_raw == row.public_key_raw


# -- Two stores with same path see each other -------------------------------


def test_two_stores_same_path_share_rows(tmp_path, device_key, keypair):
    path = tmp_path / "ss.db"
    s1 = EncryptedSignerStore(db_path=path, device_key=device_key)
    s2 = EncryptedSignerStore(db_path=path, device_key=device_key)
    try:
        row = s1.put_keypair(keypair)
        # s2 sees the row immediately (WAL mode, single host).
        assert s2.has(row.kid)
    finally:
        s1.close()
        s2.close()


def test_path_via_string_or_path(tmp_path, device_key):
    s_str = EncryptedSignerStore(db_path=str(tmp_path / "a.db"), device_key=device_key)
    s_path = EncryptedSignerStore(db_path=tmp_path / "b.db", device_key=device_key)
    try:
        assert isinstance(s_str.list_kids(), list)
        assert isinstance(s_path.list_kids(), list)
    finally:
        s_str.close()
        s_path.close()


def test_path_is_resolved_to_absolute(tmp_path, device_key):
    # Ensure the parent-dir-mkdir works with absolute paths.
    abs_path = (tmp_path / "subdir" / "ss.db").resolve()
    s = EncryptedSignerStore(db_path=abs_path, device_key=device_key)
    try:
        assert Path(abs_path).exists()
    finally:
        s.close()
