"""Federation state store tests: schema, CRUD, lifecycle, validation."""

from __future__ import annotations

import pytest

from pinky_federation.state import (
    INBOX_ARCHIVED,
    INBOX_NEW,
    INBOX_READ,
    INSTANCE_KEY_ACTIVE,
    INSTANCE_KEY_DECRYPT_ONLY,
    INSTANCE_KEY_KIND_ENCRYPTION,
    INSTANCE_KEY_KIND_SIGNING,
    INSTANCE_KEY_RETIRED,
    INVITE_ACTIVE,
    INVITE_REDEEMED,
    OUTBOX_FAILED,
    OUTBOX_PENDING,
    OUTBOX_SENT,
    PEER_PIN_PINNED,
    PEER_PIN_REJECTED,
    PEER_PIN_ROTATED,
    ROLE_MEMBER,
    ROLE_OWNER,
    AttachmentRecord,
    FederationStateStore,
    InboxRecord,
    InstanceKeyRecord,
    InviteRecord,
    OutboxRecord,
    PeerPinRecord,
    TenantRecord,
)


@pytest.fixture
def store(tmp_path):
    db_path = tmp_path / "fed.db"
    s = FederationStateStore(str(db_path))
    yield s
    s.close()


def _tenant(tenant_id: str = "t1", role: str = ROLE_MEMBER) -> TenantRecord:
    return TenantRecord(
        tenant_id=tenant_id,
        address=f"{tenant_id}@example.com",
        signing_pk=b"\x01" * 32,
        role=role,
    )


# -- schema ---------------------------------------------------------------


def test_schema_creates_all_tables(store: FederationStateStore) -> None:
    expected = {
        "tenants",
        "tenant_signing_keys",
        "instance_keys",
        "peer_pins",
        "outbox",
        "inbox",
        "issued_invites",
        "attachments",
    }
    rows = store._db.execute(  # noqa: SLF001
        "SELECT name FROM sqlite_master WHERE type='table'"
    ).fetchall()
    names = {r[0] for r in rows}
    assert expected <= names


def test_init_is_idempotent(tmp_path) -> None:
    db_path = tmp_path / "fed.db"
    s1 = FederationStateStore(str(db_path))
    s1.close()
    s2 = FederationStateStore(str(db_path))
    s2.close()  # second open does not raise


def test_stats_counts_only(store: FederationStateStore) -> None:
    s = store.stats()
    assert all(v == 0 for v in s.values())
    assert "tenants" in s and "tenant_signing_keys" in s


# -- tenants --------------------------------------------------------------


def test_tenant_upsert_and_get(store: FederationStateStore) -> None:
    rec = store.upsert_tenant(_tenant("brad", ROLE_OWNER))
    assert rec.created_at > 0
    got = store.get_tenant("brad")
    assert got is not None
    assert got.address == "brad@example.com"
    assert got.role == ROLE_OWNER
    assert got.signing_pk == b"\x01" * 32


def test_tenant_upsert_overwrites_address_and_role(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("brad", ROLE_MEMBER))
    rec = TenantRecord(
        tenant_id="brad",
        address="brad+new@example.com",
        signing_pk=b"\x02" * 32,
        role=ROLE_OWNER,
    )
    store.upsert_tenant(rec)
    got = store.get_tenant("brad")
    assert got is not None
    assert got.address == "brad+new@example.com"
    assert got.role == ROLE_OWNER


def test_tenant_upsert_rejects_bad_role(store: FederationStateStore) -> None:
    with pytest.raises(ValueError):
        store.upsert_tenant(
            TenantRecord(
                tenant_id="x", address="x@y", signing_pk=b"\x00" * 32, role="admin"
            )
        )


def test_tenant_upsert_rejects_bad_signing_pk(store: FederationStateStore) -> None:
    with pytest.raises(ValueError):
        store.upsert_tenant(
            TenantRecord(tenant_id="x", address="x@y", signing_pk=b"short", role=ROLE_MEMBER)
        )


def test_tenant_list_orders_by_created_at(store: FederationStateStore) -> None:
    a = store.upsert_tenant(TenantRecord(
        tenant_id="a", address="a@x", signing_pk=b"\x01" * 32, created_at=1.0))
    b = store.upsert_tenant(TenantRecord(
        tenant_id="b", address="b@x", signing_pk=b"\x02" * 32, created_at=2.0))
    listed = store.list_tenants()
    assert [t.tenant_id for t in listed] == ["a", "b"]
    assert a.tenant_id == "a" and b.tenant_id == "b"


# -- instance keys --------------------------------------------------------


def _ik(kid: str, tenant_id: str = "t1", state: str = INSTANCE_KEY_ACTIVE) -> InstanceKeyRecord:
    return InstanceKeyRecord(
        kid=kid,
        tenant_id=tenant_id,
        kind=INSTANCE_KEY_KIND_ENCRYPTION,
        public_key=b"\x10" * 32,
        encrypted_secret=b"\x20" * 48,
        state=state,
    )


def test_instance_key_add_and_get(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("t1"))
    rec = store.add_instance_key(_ik("k1"))
    assert rec.created_at > 0
    got = store.get_instance_key("k1")
    assert got is not None
    assert got.tenant_id == "t1"
    assert got.state == INSTANCE_KEY_ACTIVE


def test_instance_key_validates_kind_and_state(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("t1"))
    with pytest.raises(ValueError):
        store.add_instance_key(InstanceKeyRecord(
            kid="bad", tenant_id="t1", kind="garbage", public_key=b"\x00" * 32,
            encrypted_secret=b"\x00", state=INSTANCE_KEY_ACTIVE))
    with pytest.raises(ValueError):
        store.add_instance_key(InstanceKeyRecord(
            kid="bad2", tenant_id="t1", kind=INSTANCE_KEY_KIND_SIGNING,
            public_key=b"\x00" * 32, encrypted_secret=b"\x00", state="weird"))


def test_instance_key_lifecycle_active_to_decrypt_only(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("t1"))
    store.add_instance_key(_ik("k1"))
    store.transition_instance_key("k1", INSTANCE_KEY_DECRYPT_ONLY)
    assert store.get_instance_key("k1").state == INSTANCE_KEY_DECRYPT_ONLY
    store.transition_instance_key("k1", INSTANCE_KEY_RETIRED)
    got = store.get_instance_key("k1")
    assert got.state == INSTANCE_KEY_RETIRED
    assert got.retired_at > 0


def test_instance_key_lifecycle_active_directly_to_retired(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("t1"))
    store.add_instance_key(_ik("k1"))
    store.transition_instance_key("k1", INSTANCE_KEY_RETIRED)
    assert store.get_instance_key("k1").state == INSTANCE_KEY_RETIRED


def test_instance_key_lifecycle_rejects_decrypt_only_to_active(
    store: FederationStateStore,
) -> None:
    store.upsert_tenant(_tenant("t1"))
    store.add_instance_key(_ik("k1", state=INSTANCE_KEY_DECRYPT_ONLY))
    with pytest.raises(ValueError):
        store.transition_instance_key("k1", INSTANCE_KEY_ACTIVE)


def test_instance_key_lifecycle_rejects_retired_to_anything(
    store: FederationStateStore,
) -> None:
    store.upsert_tenant(_tenant("t1"))
    store.add_instance_key(_ik("k1", state=INSTANCE_KEY_RETIRED))
    for target in (INSTANCE_KEY_ACTIVE, INSTANCE_KEY_DECRYPT_ONLY):
        with pytest.raises(ValueError):
            store.transition_instance_key("k1", target)


def test_instance_key_transition_no_op_same_state(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("t1"))
    store.add_instance_key(_ik("k1"))
    store.transition_instance_key("k1", INSTANCE_KEY_ACTIVE)  # no error
    assert store.get_instance_key("k1").state == INSTANCE_KEY_ACTIVE


def test_instance_key_transition_unknown_kid(store: FederationStateStore) -> None:
    with pytest.raises(KeyError):
        store.transition_instance_key("nope", INSTANCE_KEY_RETIRED)


def test_list_instance_keys_filters(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("t1"))
    store.upsert_tenant(_tenant("t2"))
    store.add_instance_key(_ik("k1", tenant_id="t1"))
    store.add_instance_key(_ik("k2", tenant_id="t1", state=INSTANCE_KEY_DECRYPT_ONLY))
    store.add_instance_key(_ik("k3", tenant_id="t2"))
    assert {k.kid for k in store.list_instance_keys(tenant_id="t1")} == {"k1", "k2"}
    assert {k.kid for k in store.list_instance_keys(state=INSTANCE_KEY_ACTIVE)} == {"k1", "k3"}
    assert {
        k.kid for k in store.list_instance_keys(kind=INSTANCE_KEY_KIND_ENCRYPTION)
    } == {"k1", "k2", "k3"}


# -- peer pins ------------------------------------------------------------


def _pin(addr: str = "alice@example.com", status: str = PEER_PIN_PINNED) -> PeerPinRecord:
    return PeerPinRecord(
        peer_address=addr,
        sig_pk=b"\x11" * 32,
        enc_pk=b"\x22" * 32,
        fingerprint=b"\xaa" * 16,
        status=status,
    )


def test_peer_pin_upsert_sets_first_and_last_seen(store: FederationStateStore) -> None:
    rec = store.upsert_peer_pin(_pin())
    assert rec.first_seen > 0
    assert rec.last_seen >= rec.first_seen
    got = store.get_peer_pin("alice@example.com")
    assert got is not None
    assert got.fingerprint == b"\xaa" * 16


def test_peer_pin_upsert_preserves_first_seen_on_update(
    store: FederationStateStore,
) -> None:
    first = store.upsert_peer_pin(_pin())
    original_first_seen = first.first_seen
    second = store.upsert_peer_pin(
        PeerPinRecord(
            peer_address="alice@example.com",
            sig_pk=b"\x33" * 32,
            enc_pk=b"\x44" * 32,
            fingerprint=b"\xbb" * 16,
            status=PEER_PIN_ROTATED,
            first_seen=original_first_seen,
        )
    )
    got = store.get_peer_pin("alice@example.com")
    assert got.first_seen == original_first_seen
    assert got.last_seen >= second.last_seen - 1
    assert got.status == PEER_PIN_ROTATED
    assert got.sig_pk == b"\x33" * 32


def test_peer_pin_validates_lengths(store: FederationStateStore) -> None:
    with pytest.raises(ValueError):
        store.upsert_peer_pin(
            PeerPinRecord(
                peer_address="x", sig_pk=b"short", enc_pk=b"\x00" * 32,
                fingerprint=b"\x00" * 16))
    with pytest.raises(ValueError):
        store.upsert_peer_pin(
            PeerPinRecord(
                peer_address="x", sig_pk=b"\x00" * 32, enc_pk=b"\x00" * 32,
                fingerprint=b"\x00" * 8))


def test_peer_pin_rejects_bad_status(store: FederationStateStore) -> None:
    with pytest.raises(ValueError):
        store.upsert_peer_pin(_pin(status="weird"))


def test_peer_pin_status_rejected_round_trip(store: FederationStateStore) -> None:
    store.upsert_peer_pin(_pin(status=PEER_PIN_REJECTED))
    got = store.get_peer_pin("alice@example.com")
    assert got.status == PEER_PIN_REJECTED


# -- outbox ---------------------------------------------------------------


def _ob(msg_id: str = "m1") -> OutboxRecord:
    return OutboxRecord(
        msg_id=msg_id,
        tenant_id="t1",
        recipient_address="alice@example.com",
        envelope_blob=b"envelope-bytes",
    )


def test_outbox_enqueue_and_list(store: FederationStateStore) -> None:
    store.enqueue_outbound(_ob("m1"))
    store.enqueue_outbound(_ob("m2"))
    pending = store.list_outbound(status=OUTBOX_PENDING)
    assert {r.msg_id for r in pending} == {"m1", "m2"}


def test_outbox_mark_sent_increments_attempts(store: FederationStateStore) -> None:
    store.enqueue_outbound(_ob("m1"))
    store.mark_outbound_status("m1", OUTBOX_SENT)
    sent = store.list_outbound(status=OUTBOX_SENT)
    assert len(sent) == 1
    assert sent[0].attempts == 1


def test_outbox_mark_failed_records_error(store: FederationStateStore) -> None:
    store.enqueue_outbound(_ob("m1"))
    store.mark_outbound_status("m1", OUTBOX_FAILED, last_error="relay 503", next_retry_at=42.0)
    failed = store.list_outbound(status=OUTBOX_FAILED)
    assert failed[0].last_error == "relay 503"
    assert failed[0].next_retry_at == 42.0


def test_outbox_mark_unknown_msg_raises(store: FederationStateStore) -> None:
    with pytest.raises(KeyError):
        store.mark_outbound_status("nope", OUTBOX_SENT)


def test_outbox_status_filter_validation(store: FederationStateStore) -> None:
    with pytest.raises(ValueError):
        store.list_outbound(status="weird")


# -- inbox ----------------------------------------------------------------


def _ib(msg_id: str = "i1", tenant_id: str = "t1") -> InboxRecord:
    return InboxRecord(
        msg_id=msg_id,
        tenant_id=tenant_id,
        sender_address="alice@example.com",
        plaintext_blob=b"hello",
    )


def test_inbox_store_and_list(store: FederationStateStore) -> None:
    store.store_inbound(_ib("i1"))
    store.store_inbound(_ib("i2"))
    rows = store.list_inbound(tenant_id="t1")
    assert {r.msg_id for r in rows} == {"i1", "i2"}


def test_inbox_mark_read_then_archived(store: FederationStateStore) -> None:
    store.store_inbound(_ib("i1"))
    store.mark_inbound_status("i1", INBOX_READ)
    assert store.list_inbound(status=INBOX_READ)[0].msg_id == "i1"
    store.mark_inbound_status("i1", INBOX_ARCHIVED)
    assert store.list_inbound(status=INBOX_ARCHIVED)[0].status == INBOX_ARCHIVED


def test_inbox_default_status_is_new(store: FederationStateStore) -> None:
    store.store_inbound(_ib("i1"))
    assert store.list_inbound(status=INBOX_NEW)[0].status == INBOX_NEW


def test_inbox_mark_unknown_raises(store: FederationStateStore) -> None:
    with pytest.raises(KeyError):
        store.mark_inbound_status("nope", INBOX_READ)


# -- invites --------------------------------------------------------------


def _inv(invite_id: str = "v1") -> InviteRecord:
    return InviteRecord(
        invite_id=invite_id,
        tenant_id="t1",
        recipient_hint="alice@example.com",
        token_hash=b"\xdd" * 32,
        expires_at=999999999.0,
    )


def test_invite_add_and_list(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("t1"))
    store.add_invite(_inv("v1"))
    store.add_invite(_inv("v2"))
    listed = store.list_invites(tenant_id="t1")
    assert {i.invite_id for i in listed} == {"v1", "v2"}


def test_invite_token_hash_must_be_32_bytes(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("t1"))
    with pytest.raises(ValueError):
        store.add_invite(InviteRecord(
            invite_id="v1", tenant_id="t1", token_hash=b"short", expires_at=1.0))


def test_invite_status_lifecycle(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("t1"))
    store.add_invite(_inv("v1"))
    assert store.list_invites(status=INVITE_ACTIVE)[0].invite_id == "v1"
    store.mark_invite_status("v1", INVITE_REDEEMED)
    assert store.list_invites(status=INVITE_REDEEMED)[0].invite_id == "v1"


def test_invite_mark_unknown_raises(store: FederationStateStore) -> None:
    with pytest.raises(KeyError):
        store.mark_invite_status("nope", INVITE_REDEEMED)


# -- attachments ----------------------------------------------------------


def _att(att_id: str = "a1", msg_id: str = "i1") -> AttachmentRecord:
    return AttachmentRecord(
        attachment_id=att_id,
        msg_id=msg_id,
        sha256=b"\x99" * 32,
        size=1024,
        mime="image/png",
        local_path="/tmp/blob",
        encrypted=True,
    )


def test_attachment_add_and_list(store: FederationStateStore) -> None:
    store.add_attachment(_att("a1"))
    store.add_attachment(_att("a2"))
    rows = store.list_attachments("i1")
    assert {a.attachment_id for a in rows} == {"a1", "a2"}
    assert rows[0].encrypted is True
    assert rows[0].size == 1024


def test_attachment_validates_sha_and_size(store: FederationStateStore) -> None:
    with pytest.raises(ValueError):
        store.add_attachment(AttachmentRecord(
            attachment_id="a", msg_id="m", sha256=b"short", size=1, local_path="/x"))
    with pytest.raises(ValueError):
        store.add_attachment(AttachmentRecord(
            attachment_id="a", msg_id="m", sha256=b"\x00" * 32, size=-1, local_path="/x"))


# -- foreign keys ---------------------------------------------------------


def test_tenant_signing_keys_fk_cascades_on_tenant_delete(store: FederationStateStore) -> None:
    store.upsert_tenant(_tenant("t1"))
    # Insert a row directly via the connection (key_store does this normally).
    store._db.execute(  # noqa: SLF001
        "INSERT INTO tenant_signing_keys (tenant_id, encrypted_seed, nonce, "
        "kdf_version, created_at) VALUES (?, ?, ?, ?, ?)",
        ("t1", b"ct", b"n", 1, 1.0),
    )
    store._db.commit()  # noqa: SLF001
    store._db.execute("DELETE FROM tenants WHERE tenant_id = ?", ("t1",))  # noqa: SLF001
    store._db.commit()  # noqa: SLF001
    rows = store._db.execute(  # noqa: SLF001
        "SELECT * FROM tenant_signing_keys WHERE tenant_id = ?", ("t1",)
    ).fetchall()
    assert rows == []


def test_db_uses_wal_mode(store: FederationStateStore) -> None:
    mode = store._db.execute("PRAGMA journal_mode").fetchone()[0]  # noqa: SLF001
    assert mode.lower() == "wal"
