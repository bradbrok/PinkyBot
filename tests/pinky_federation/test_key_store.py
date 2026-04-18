"""Encrypted tenant key store tests.

Focus areas:
- Round-trip: encrypt -> persist -> decrypt yields original signing seed.
- Repr / log discipline: seed bytes never appear in any exposed string.
- Permissions: device key file is created 0600; loose perms are rejected.
- Tenant binding: ciphertext for tenant A cannot be replayed under tenant B.
- Export gate: raw seed export requires explicit operator acknowledgement.
- Lifecycle: ``has_signing_key`` reflects state; ``get_signing_key`` raises
  ``KeyError`` when no key is stored.
- Wrong device key surfaces as ``DecryptionError`` (not raw libsodium type).
"""

from __future__ import annotations

import io
import logging
import os
import secrets
import stat

import pytest

from pinky_federation.errors import DecryptionError
from pinky_federation.key_store import (
    DEFAULT_DEVICE_KEY_PATH,
    DEVICE_KEY_BYTES,
    DeviceKey,
    EncryptedTenantKeyStore,
)
from pinky_federation.keys import SigningKeyPair
from pinky_federation.state import FederationStateStore, TenantRecord


@pytest.fixture
def state(tmp_path):
    s = FederationStateStore(str(tmp_path / "fed.db"))
    yield s
    s.close()


@pytest.fixture
def device_key(tmp_path):
    return DeviceKey.load_or_create(str(tmp_path / "device.key"))


@pytest.fixture
def key_store(state, device_key):
    return EncryptedTenantKeyStore(state, device_key)


def _register_tenant(state: FederationStateStore, tenant_id: str = "t1") -> None:
    state.upsert_tenant(
        TenantRecord(
            tenant_id=tenant_id,
            address=f"{tenant_id}@example.com",
            signing_pk=b"\x01" * 32,
        )
    )


# -- DeviceKey ------------------------------------------------------------


def test_device_key_creates_file_with_0600_perms(tmp_path) -> None:
    path = tmp_path / "device.key"
    dk = DeviceKey.load_or_create(str(path))
    assert path.exists()
    mode = stat.S_IMODE(path.stat().st_mode)
    # On macOS / Linux this should be exactly 0o600.
    assert mode == 0o600, f"expected 0o600, got {oct(mode)}"
    assert len(dk.material_insecure()) == DEVICE_KEY_BYTES


def test_device_key_is_persistent_across_loads(tmp_path) -> None:
    path = tmp_path / "device.key"
    dk1 = DeviceKey.load_or_create(str(path))
    dk2 = DeviceKey.load_or_create(str(path))
    assert dk1.material_insecure() == dk2.material_insecure()


def test_device_key_rejects_loose_permissions(tmp_path) -> None:
    path = tmp_path / "device.key"
    DeviceKey.load_or_create(str(path))
    os.chmod(str(path), 0o644)
    with pytest.raises(PermissionError):
        DeviceKey.load_or_create(str(path))


def test_device_key_rejects_wrong_size_file(tmp_path) -> None:
    path = tmp_path / "device.key"
    path.write_bytes(b"\x00" * 31)
    os.chmod(str(path), 0o600)
    with pytest.raises(ValueError):
        DeviceKey.load_or_create(str(path))


def test_device_key_repr_does_not_expose_full_material(tmp_path) -> None:
    dk = DeviceKey.load_or_create(str(tmp_path / "device.key"))
    text = repr(dk)
    assert dk.material_insecure().hex() not in text
    # Truncated tag (4 bytes = 8 hex chars) is fine; full key is not.
    assert "DeviceKey(" in text


def test_device_key_validates_constructor_input() -> None:
    with pytest.raises(ValueError):
        DeviceKey(b"too-short")


def test_default_device_key_path_constant() -> None:
    # Sanity: the documented default path lives under data/federation/.
    assert "data/federation/" in DEFAULT_DEVICE_KEY_PATH
    assert DEFAULT_DEVICE_KEY_PATH.endswith(".device_key")


# -- Round-trip -----------------------------------------------------------


def test_put_then_get_round_trip(key_store, state) -> None:
    _register_tenant(state)
    original = SigningKeyPair.generate()
    key_store.put_signing_key("t1", original)
    restored = key_store.get_signing_key("t1")
    # Equality on Ed25519 keys: same public key implies same seed.
    assert restored.public_key.raw == original.public_key.raw


def test_put_overwrites_previous_key(key_store, state) -> None:
    _register_tenant(state)
    first = SigningKeyPair.generate()
    second = SigningKeyPair.generate()
    key_store.put_signing_key("t1", first)
    key_store.put_signing_key("t1", second)
    restored = key_store.get_signing_key("t1")
    assert restored.public_key.raw == second.public_key.raw
    assert restored.public_key.raw != first.public_key.raw


def test_has_signing_key_reflects_state(key_store, state) -> None:
    _register_tenant(state)
    assert key_store.has_signing_key("t1") is False
    key_store.put_signing_key("t1", SigningKeyPair.generate())
    assert key_store.has_signing_key("t1") is True


def test_get_signing_key_raises_keyerror_when_missing(key_store) -> None:
    with pytest.raises(KeyError):
        key_store.get_signing_key("nope")


def test_put_requires_existing_tenant(key_store) -> None:
    with pytest.raises(ValueError):
        key_store.put_signing_key("ghost", SigningKeyPair.generate())


def test_put_validates_types(key_store, state) -> None:
    _register_tenant(state)
    with pytest.raises(TypeError):
        key_store.put_signing_key("t1", "not-a-keypair")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        key_store.put_signing_key("", SigningKeyPair.generate())


# -- Tenant binding (AAD) -------------------------------------------------


def test_ciphertext_cannot_be_replayed_to_other_tenant(key_store, state) -> None:
    _register_tenant(state, "alice")
    _register_tenant(state, "bob")
    key_store.put_signing_key("alice", SigningKeyPair.generate())
    # Forge: copy alice's stored ciphertext into bob's row.
    row = state._db.execute(  # noqa: SLF001
        "SELECT encrypted_seed, nonce, kdf_version FROM tenant_signing_keys "
        "WHERE tenant_id = ?",
        ("alice",),
    ).fetchone()
    state._db.execute(  # noqa: SLF001
        "INSERT INTO tenant_signing_keys (tenant_id, encrypted_seed, nonce, "
        "kdf_version, created_at) VALUES (?, ?, ?, ?, ?)",
        ("bob", bytes(row["encrypted_seed"]), bytes(row["nonce"]), int(row["kdf_version"]), 1.0),
    )
    state._db.commit()  # noqa: SLF001
    with pytest.raises(DecryptionError):
        key_store.get_signing_key("bob")


# -- Wrong device key -----------------------------------------------------


def test_wrong_device_key_raises_decryption_error(state, tmp_path) -> None:
    real_dk = DeviceKey.load_or_create(str(tmp_path / "device.key"))
    other_dk = DeviceKey(secrets.token_bytes(DEVICE_KEY_BYTES))
    real_store = EncryptedTenantKeyStore(state, real_dk)
    other_store = EncryptedTenantKeyStore(state, other_dk)
    _register_tenant(state)
    real_store.put_signing_key("t1", SigningKeyPair.generate())
    with pytest.raises(DecryptionError):
        other_store.get_signing_key("t1")


def test_corrupt_ciphertext_raises_decryption_error(key_store, state) -> None:
    _register_tenant(state)
    key_store.put_signing_key("t1", SigningKeyPair.generate())
    state._db.execute(  # noqa: SLF001
        "UPDATE tenant_signing_keys SET encrypted_seed = ? WHERE tenant_id = ?",
        (b"\x00" * 48, "t1"),
    )
    state._db.commit()  # noqa: SLF001
    with pytest.raises(DecryptionError):
        key_store.get_signing_key("t1")


def test_unsupported_kdf_version_raises(key_store, state) -> None:
    _register_tenant(state)
    key_store.put_signing_key("t1", SigningKeyPair.generate())
    state._db.execute(  # noqa: SLF001
        "UPDATE tenant_signing_keys SET kdf_version = 999 WHERE tenant_id = ?", ("t1",)
    )
    state._db.commit()  # noqa: SLF001
    with pytest.raises(DecryptionError):
        key_store.get_signing_key("t1")


# -- Export gate ----------------------------------------------------------


def test_export_requires_explicit_acknowledgement(key_store, state) -> None:
    _register_tenant(state)
    kp = SigningKeyPair.generate()
    key_store.put_signing_key("t1", kp)
    with pytest.raises(PermissionError):
        key_store.export_signing_seed_explicit(
            "t1", operator_acknowledged=False, reason="reason"
        )


def test_export_requires_non_empty_reason(key_store, state) -> None:
    _register_tenant(state)
    key_store.put_signing_key("t1", SigningKeyPair.generate())
    with pytest.raises(ValueError):
        key_store.export_signing_seed_explicit(
            "t1", operator_acknowledged=True, reason="   "
        )


def test_export_returns_seed_when_acknowledged(key_store, state) -> None:
    _register_tenant(state)
    kp = SigningKeyPair.generate()
    key_store.put_signing_key("t1", kp)
    seed = key_store.export_signing_seed_explicit(
        "t1", operator_acknowledged=True, reason="operator initiated migration"
    )
    assert isinstance(seed, bytes)
    assert len(seed) == 32
    # The seed actually reproduces the same keypair.
    assert SigningKeyPair.from_seed(seed).public_key.raw == kp.public_key.raw


# -- Repr / log discipline ------------------------------------------------


def test_repr_does_not_leak_seed(key_store, state) -> None:
    _register_tenant(state)
    kp = SigningKeyPair.generate()
    key_store.put_signing_key("t1", kp)
    seed_hex = kp.seed_bytes_insecure().hex()
    text = repr(key_store)
    assert seed_hex not in text


def test_logging_module_does_not_capture_seed_via_repr(
    key_store, state, caplog
) -> None:
    """Even if a careless caller logs the store object, the seed must not appear."""
    _register_tenant(state)
    kp = SigningKeyPair.generate()
    key_store.put_signing_key("t1", kp)
    seed_hex = kp.seed_bytes_insecure().hex()

    log = logging.getLogger("test-fed-keystore-repr")
    buf = io.StringIO()
    handler = logging.StreamHandler(buf)
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        log.debug("store snapshot: %r", key_store)
    finally:
        log.removeHandler(handler)

    output = buf.getvalue()
    assert seed_hex not in output


def test_state_stats_contains_count_only_not_material(key_store, state) -> None:
    _register_tenant(state)
    kp = SigningKeyPair.generate()
    key_store.put_signing_key("t1", kp)
    stats = state.stats()
    # stats is a dict[str, int] — there is nowhere for key bytes to hide.
    assert isinstance(stats, dict)
    assert stats["tenant_signing_keys"] == 1
    for v in stats.values():
        assert isinstance(v, int)


def test_signing_keypair_repr_does_not_leak_seed(key_store, state) -> None:
    """Returned SigningKeyPair must follow the same repr discipline."""
    _register_tenant(state)
    kp = SigningKeyPair.generate()
    seed_hex = kp.seed_bytes_insecure().hex()
    key_store.put_signing_key("t1", kp)
    restored = key_store.get_signing_key("t1")
    assert seed_hex not in repr(restored)


# -- Constructor validation -----------------------------------------------


def test_constructor_rejects_wrong_state_type(device_key) -> None:
    with pytest.raises(TypeError):
        EncryptedTenantKeyStore(state="not-a-store", device_key=device_key)  # type: ignore[arg-type]


def test_constructor_rejects_wrong_device_key_type(state) -> None:
    with pytest.raises(TypeError):
        EncryptedTenantKeyStore(state=state, device_key=b"raw-bytes")  # type: ignore[arg-type]
