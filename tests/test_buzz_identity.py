"""Security and migration tests for encrypted Buzz identities."""

from __future__ import annotations

import json
import sqlite3

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.buzz_identity import (
    BUZZ_TOS_POLICY,
    BUZZ_TOS_POLICY_VERSION,
    BuzzIdentityUnhealthyError,
    BuzzKeyEnvelope,
    unwrap_buzz_private_key,
    wrap_buzz_private_key,
)
from pinky_identity.keystore import DeviceKey
from pinky_outreach.buzz import BuzzNostrSigner

PRIVATE_KEY = "11" * 32
OTHER_PRIVATE_KEY = "22" * 32
RELAY_SIGNING_PUBKEY = BuzzNostrSigner(bytes.fromhex("66" * 32)).pubkey
PRODUCTION_RELAY_SIGNING_PUBKEY = (
    "12f6870117eff1a6318bd38c82a65d51dd19879b7489f57247114d0ee8a96de3"
)


@pytest.fixture
def registry(tmp_path):
    result = AgentRegistry(
        str(tmp_path / "agents.db"),
        buzz_device_key_path=str(tmp_path / "identity" / ".device_key"),
    )
    result.register("barsik", model="sonnet", working_dir=str(tmp_path / "barsik"))
    yield result
    result.close()


def _bind(registry: AgentRegistry, **overrides) -> dict:
    values = {
        "private_key": PRIVATE_KEY,
        "relay_url": "wss://example.communities.buzz.xyz",
        "community_id": "example",
        "relay_signing_pubkey": RELAY_SIGNING_PUBKEY,
        "enabled": True,
        "owner_actor": "ui:admin",
    }
    values.update(overrides)
    return registry.bind_buzz_identity_owner_control("barsik", **values)


def test_private_key_is_encrypted_and_absent_from_public_surfaces(registry, tmp_path):
    public = _bind(registry)
    raw_db = (tmp_path / "agents.db").read_bytes()

    assert PRIVATE_KEY.encode() not in raw_db
    assert b"nsec1" not in raw_db
    assert "private_key" not in public
    assert "nonce" not in public
    assert "ciphertext" not in public
    assert "tos_receipt" not in public
    columns = {row[1] for row in registry._db.execute("PRAGMA table_info(buzz_identities)")}
    assert "private_key" not in columns
    assert "secret_key" not in columns
    unique_indexes = {
        row[1] for row in registry._db.execute("PRAGMA index_list(buzz_identities)") if row[2]
    }
    assert "idx_buzz_identities_pubkey" in unique_indexes
    assert "idx_buzz_identities_approval_ref" in unique_indexes
    assert "idx_buzz_identities_tos_receipt" in unique_indexes
    assert public["tos_approved"] is True
    assert public["relay_signing_pubkey"] == RELAY_SIGNING_PUBKEY

    row = registry._db.execute(
        "SELECT nonce, ciphertext, tos_receipt FROM buzz_identities WHERE agent='barsik'"
    ).fetchone()
    assert len(row[0]) == 24
    assert len(row[1]) == 48
    assert row[1] != bytes.fromhex(PRIVATE_KEY)
    receipt = json.loads(row[2])
    assert receipt == {
        "action": "buzz.identity.bind",
        "agent": "barsik",
        "community_id": "example",
        "policy": BUZZ_TOS_POLICY,
        "policy_version": BUZZ_TOS_POLICY_VERSION,
        "pubkey": public["pubkey"],
        "relay_url": "wss://example.communities.buzz.xyz",
    }

    material = registry.get_buzz_signing_material("barsik")
    assert material.private_key == bytes.fromhex(PRIVATE_KEY)
    assert material.relay_signing_pubkey == RELAY_SIGNING_PUBKEY
    assert PRIVATE_KEY not in repr(material)


@pytest.mark.parametrize("column", ["nonce", "ciphertext", "pubkey"])
def test_tampered_envelope_or_pubkey_disables_identity(registry, column):
    _bind(registry)
    if column == "pubkey":
        value = "f" * 64
    else:
        original = registry._db.execute(
            f"SELECT {column} FROM buzz_identities WHERE agent='barsik'"
        ).fetchone()[0]
        tampered = bytearray(original)
        tampered[0] ^= 1
        value = bytes(tampered)
    registry._db.execute(f"UPDATE buzz_identities SET {column}=? WHERE agent='barsik'", (value,))
    registry._db.commit()

    with pytest.raises(BuzzIdentityUnhealthyError):
        registry.get_buzz_signing_material("barsik")

    state = registry.get_buzz_identity("barsik")
    assert state["enabled"] is False
    assert state["status"] == "unhealthy"


def test_malformed_blob_type_also_disables_identity(registry):
    _bind(registry)
    registry._db.execute("UPDATE buzz_identities SET ciphertext='not-a-blob' WHERE agent='barsik'")
    registry._db.commit()

    with pytest.raises(BuzzIdentityUnhealthyError):
        registry.get_buzz_signing_material("barsik")
    assert registry.get_buzz_identity("barsik")["status"] == "unhealthy"


def test_aad_is_bound_to_agent_and_pubkey():
    key = DeviceKey.from_bytes(b"k" * 32)
    wrapped = wrap_buzz_private_key(PRIVATE_KEY, agent="barsik", device_key=key)
    foreign_agent = BuzzKeyEnvelope(
        agent="murzik",
        pubkey=wrapped.pubkey,
        wrap_version=wrapped.wrap_version,
        nonce=wrapped.nonce,
        ciphertext=wrapped.ciphertext,
    )
    foreign_pubkey = BuzzKeyEnvelope(
        agent=wrapped.agent,
        pubkey="f" * 64,
        wrap_version=wrapped.wrap_version,
        nonce=wrapped.nonce,
        ciphertext=wrapped.ciphertext,
    )

    with pytest.raises(BuzzIdentityUnhealthyError):
        unwrap_buzz_private_key(foreign_agent, device_key=key)
    with pytest.raises(BuzzIdentityUnhealthyError):
        unwrap_buzz_private_key(foreign_pubkey, device_key=key)


def test_wrong_device_kek_disables_identity(registry, tmp_path):
    _bind(registry)
    registry.close()
    other = AgentRegistry(
        str(tmp_path / "agents.db"),
        buzz_device_key_path=str(tmp_path / "other-identity" / ".device_key"),
    )
    try:
        with pytest.raises(BuzzIdentityUnhealthyError):
            other.get_buzz_signing_material("barsik")
        assert other.get_buzz_identity("barsik")["status"] == "unhealthy"
    finally:
        other.close()


def test_enable_refuses_untrusted_owner_actor(registry):
    with pytest.raises(ValueError, match="owner approval actor"):
        _bind(registry, owner_actor="agent:forged")
    assert registry.get_buzz_identity("barsik") is None


def test_server_generated_tos_receipt_is_immutable_without_explicit_rotation(registry):
    original = _bind(registry)
    stored_receipt = registry._db.execute(
        "SELECT tos_receipt FROM buzz_identities WHERE agent='barsik'"
    ).fetchone()[0]
    rebound = _bind(registry, owner_actor="ui:different-owner")
    assert rebound["tos_approval_ref"] == original["tos_approval_ref"]
    assert rebound["tos_approved_by"] == original["tos_approved_by"]
    assert (
        registry._db.execute(
            "SELECT tos_receipt FROM buzz_identities WHERE agent='barsik'"
        ).fetchone()[0]
        == stored_receipt
    )

    with pytest.raises(ValueError, match="rotation"):
        _bind(registry, community_id="different")
    assert registry.get_buzz_identity("barsik")["tos_approval_ref"] == original["tos_approval_ref"]


def test_identity_rotation_is_not_implicit(registry):
    _bind(registry)
    with pytest.raises(ValueError, match="rotation"):
        _bind(registry, private_key=OTHER_PRIVATE_KEY)


def test_relay_signing_authority_rotation_is_not_implicit(registry):
    _bind(registry)
    replacement = BuzzNostrSigner(bytes.fromhex("77" * 32)).pubkey
    with pytest.raises(ValueError, match="relay signing authority rotation"):
        _bind(registry, relay_signing_pubkey=replacement)


def test_one_approval_reference_cannot_be_reused_for_a_second_identity(registry, tmp_path):
    _bind(registry)
    registry.register("murzik", model="sonnet", working_dir=str(tmp_path / "murzik"))
    source = registry._db.execute(
        "SELECT tos_receipt, tos_approved_by, tos_approved_at, tos_approval_ref "
        "FROM buzz_identities WHERE agent='barsik'"
    ).fetchone()

    with pytest.raises(sqlite3.IntegrityError):
        registry._db.execute(
            """INSERT INTO buzz_identities (
                   agent, pubkey, wrap_version, nonce, ciphertext, relay_url,
                   community_id, enabled, status, last_error, tos_receipt,
                   tos_approved_by, tos_approved_at, tos_approval_ref,
                   created_at, updated_at
               ) VALUES (?, ?, 1, X'00', X'00', ?, ?, 1, 'active', '',
                         ?, ?, ?, ?, 1, 1)""",
            (
                "murzik",
                "f" * 64,
                "wss://example.communities.buzz.xyz",
                "example",
                *source,
            ),
        )


@pytest.mark.parametrize(
    ("column", "value"),
    [
        ("tos_receipt", "arbitrary caller text"),
        ("tos_approved_by", "agent:forged"),
        ("tos_approval_ref", "owner-control:" + "b" * 32 + ":sha256:" + "c" * 64),
    ],
)
def test_tampered_owner_authority_disables_identity(registry, column, value):
    _bind(registry)
    registry._db.execute(
        f"UPDATE buzz_identities SET {column}=? WHERE agent='barsik'",
        (value,),
    )
    registry._db.commit()

    with pytest.raises(BuzzIdentityUnhealthyError):
        registry.get_buzz_signing_material("barsik")
    state = registry.get_buzz_identity("barsik")
    assert state["enabled"] is False
    assert state["status"] == "unhealthy"


def test_legacy_buzz_table_gets_all_forward_columns(tmp_path):
    db_path = tmp_path / "legacy.db"
    with sqlite3.connect(db_path) as db:
        db.execute("CREATE TABLE buzz_identities (agent TEXT PRIMARY KEY)")
    registry = AgentRegistry(
        str(db_path),
        buzz_device_key_path=str(tmp_path / "identity" / ".device_key"),
    )
    try:
        columns = {row[1] for row in registry._db.execute("PRAGMA table_info(buzz_identities)")}
        assert {
            "agent",
            "pubkey",
            "wrap_version",
            "nonce",
            "ciphertext",
            "relay_url",
            "community_id",
            "relay_signing_pubkey",
            "enabled",
            "status",
            "last_error",
            "tos_receipt",
            "tos_approved_by",
            "tos_approved_at",
            "tos_approval_ref",
            "created_at",
            "updated_at",
        } <= columns
    finally:
        registry.close()


def test_existing_barsik_identity_migration_seeds_verified_relay_authority(tmp_path):
    db_path = tmp_path / "agents.db"
    registry = AgentRegistry(
        str(db_path),
        buzz_device_key_path=str(tmp_path / "identity" / ".device_key"),
    )
    registry.register("barsik", model="sonnet", working_dir=str(tmp_path / "barsik"))
    _bind(registry)
    registry.close()

    with sqlite3.connect(db_path) as db:
        db.execute("ALTER TABLE buzz_identities DROP COLUMN relay_signing_pubkey")

    migrated = AgentRegistry(
        str(db_path),
        buzz_device_key_path=str(tmp_path / "identity" / ".device_key"),
    )
    try:
        assert (
            migrated.get_buzz_identity("barsik")["relay_signing_pubkey"]
            == PRODUCTION_RELAY_SIGNING_PUBKEY
        )
    finally:
        migrated.close()

    with sqlite3.connect(db_path) as db:
        db.execute(
            "UPDATE buzz_identities SET relay_signing_pubkey='' WHERE agent='barsik'"
        )
    reopened = AgentRegistry(
        str(db_path),
        buzz_device_key_path=str(tmp_path / "identity" / ".device_key"),
    )
    try:
        assert reopened.get_buzz_identity("barsik")["relay_signing_pubkey"] == ""
    finally:
        reopened.close()
