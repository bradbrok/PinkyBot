"""Security and persona-routing tests for native Buzz inbound."""

from __future__ import annotations

import sqlite3
import time

import pytest

import pinky_daemon.buzz_inbound as buzz_inbound
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.broker import MessageBroker
from pinky_daemon.buzz_inbound import (
    BuzzInboundDeliveryError,
    BuzzInboundProcessor,
    BuzzInboundRejectedError,
    validate_buzz_inbound_event,
    validate_buzz_membership_event,
)
from pinky_outreach.buzz import BuzzNostrSigner

AGENT_KEY = "11" * 32
OWNER_KEY = bytes.fromhex("33" * 32)
USER_KEY = bytes.fromhex("44" * 32)
STRANGER_KEY = bytes.fromhex("55" * 32)
OWNER = BuzzNostrSigner(OWNER_KEY)
USER = BuzzNostrSigner(USER_KEY)
STRANGER = BuzzNostrSigner(STRANGER_KEY)
RELAY_AUTHORITY = BuzzNostrSigner(bytes.fromhex("66" * 32))
CHANNEL = "00000000-0000-4000-8000-000000000001"
OTHER_CHANNEL = "00000000-0000-4000-8000-000000000002"
RELAY = "wss://example.communities.buzz.xyz"
COMMUNITY = "example"


class FakeBroker:
    def __init__(self, *, accepted: bool = True) -> None:
        self.accepted = accepted
        self.calls: list[tuple[str, object]] = []

    async def dispatch_pre_authorized(self, agent_name, message):  # noqa: ANN001
        self.calls.append((agent_name, message))
        return self.accepted


@pytest.fixture
def registry(tmp_path):
    result = AgentRegistry(
        str(tmp_path / "agents.db"),
        buzz_device_key_path=str(tmp_path / "identity" / ".device_key"),
    )
    result.register("barsik", model="sonnet", working_dir=str(tmp_path / "barsik"))
    identity = result.bind_buzz_identity_owner_control(
        "barsik",
        private_key=AGENT_KEY,
        relay_url=RELAY,
        community_id=COMMUNITY,
        relay_signing_pubkey=RELAY_AUTHORITY.pubkey,
        enabled=True,
        owner_actor="ui:admin",
    )
    result.configure_buzz_inbound_owner_control(
        "barsik",
        owner_pubkey=OWNER.pubkey,
        channels=[{"channel_id": CHANNEL, "label": "#general"}],
        approved_users=[{"pubkey": USER.pubkey, "display_name": "Brad"}],
        owner_actor="ui:admin",
    )
    yield result, identity["pubkey"]
    result.close()


def _processor(registry, identity_pubkey, broker=None):
    return BuzzInboundProcessor(
        registry=registry,
        broker=broker or FakeBroker(),
        agent_name="barsik",
        identity_pubkey=identity_pubkey,
        community_id=COMMUNITY,
        relay_url=RELAY,
    )


def _event(
    signer: BuzzNostrSigner = USER,
    *,
    kind: int = 9,
    channel: str = CHANNEL,
    content: str = "hello",
    p_tags: list[str] | None = None,
    created_at: int | None = None,
):
    tags = [["h", channel]]
    for pubkey in p_tags or []:
        tags.append(["p", pubkey])
    return signer.sign_event(
        kind=kind,
        tags=tags,
        content=content,
        created_at=created_at,
    )


def test_policy_is_identity_scoped_default_deny_and_owner_issued(registry):
    store, identity_pubkey = registry
    policy = store.get_buzz_inbound_policy("barsik")

    assert policy["community_id"] == COMMUNITY
    assert policy["relay_url"] == RELAY
    assert policy["channels"] == [{"channel_id": CHANNEL, "label": "#general"}]
    assert policy["owner_principal"] == f"buzz:{COMMUNITY}:{OWNER.pubkey}"
    assert policy["updated_by"] == "ui:admin"
    assert policy["owner_silence_days"] == 14
    assert {item["principal"] for item in policy["principals"]} == {
        f"buzz:{COMMUNITY}:{OWNER.pubkey}",
        f"buzz:{COMMUNITY}:{USER.pubkey}",
    }
    assert store.get_buzz_inbound_channel("barsik", COMMUNITY, RELAY, OTHER_CHANNEL) is None
    assert store.get_buzz_inbound_principal("barsik", COMMUNITY, STRANGER.pubkey) is None
    assert identity_pubkey not in {item["pubkey"] for item in policy["principals"]}


def test_policy_replace_is_owner_controlled_and_preserves_seen_same_principal(registry):
    store, _ = registry
    store.note_buzz_inbound_principal_seen("barsik", COMMUNITY, OWNER.pubkey, 1234)
    with pytest.raises(ValueError, match="owner approval actor"):
        store.configure_buzz_inbound_owner_control(
            "barsik",
            owner_pubkey=OWNER.pubkey,
            channels=[{"channel_id": CHANNEL, "label": "#general"}],
            approved_users=[],
            owner_actor="agent:forged",
        )

    policy = store.configure_buzz_inbound_owner_control(
        "barsik",
        owner_pubkey=OWNER.pubkey,
        channels=[{"channel_id": OTHER_CHANNEL, "label": "#ops"}],
        approved_users=[],
        owner_actor="ui:admin",
    )
    assert policy["channels"] == [{"channel_id": OTHER_CHANNEL, "label": "#ops"}]
    assert policy["owner_last_seen_at"] == 1234
    assert store.get_buzz_inbound_channel("barsik", COMMUNITY, RELAY, CHANNEL) is None


@pytest.mark.asyncio
async def test_verified_no_p_broadcast_reaches_persona_with_full_principal(registry):
    store, identity_pubkey = registry
    broker = FakeBroker()
    processor = _processor(store, identity_pubkey, broker)
    event = _event(content="ordinary channel message")

    assert await processor.process_event(event) == "delivered"
    assert len(broker.calls) == 1
    message = broker.calls[0][1]
    assert message.agent_name == "barsik"
    assert message.chat_id == CHANNEL
    assert message.is_group is True
    assert message.sender_id == f"buzz:{COMMUNITY}:{USER.pubkey}"
    assert message.metadata["buzz_mentioned_self"] is False
    assert message.metadata["buzz_display_name_untrusted"] is True
    assert message.metadata["buzz_verified_event"] == {
        "verified": True,
        "event_id": event["id"],
        "kind": 9,
        "author_pubkey": USER.pubkey,
        "channel_id": CHANNEL,
    }

    formatter = object.__new__(MessageBroker)
    formatter._registry = store
    prompt = MessageBroker._format_prompt(formatter, message)
    assert "[buzz | #general" in prompt
    assert "display_name(untrusted+collides:Brad):Brad" in prompt
    assert f"principal:buzz:{COMMUNITY}:{USER.pubkey}" in prompt
    assert "mentioned_self:false" in prompt
    [chat] = store.list_group_chats("barsik")
    assert (chat["platform"], chat["chat_id"], chat["chat_title"], chat["chat_type"]) == (
        "buzz",
        CHANNEL,
        "#general",
        "channel",
    )


@pytest.mark.asyncio
async def test_only_exact_single_self_p_tag_sets_mentioned_self(registry):
    store, identity_pubkey = registry
    broker = FakeBroker()
    processor = _processor(store, identity_pubkey, broker)

    exact = _event(content="ping", p_tags=[identity_pubkey])
    foreign = _event(content="foreign", p_tags=[STRANGER.pubkey])
    duplicate = USER.sign_event(
        kind=9,
        tags=[["h", CHANNEL], ["p", identity_pubkey], ["p", identity_pubkey]],
        content="duplicate",
    )
    display_only = _event(content="@Barsik in display text only")

    assert await processor.process_event(exact) == "delivered"
    assert broker.calls[-1][1].metadata["buzz_mentioned_self"] is True
    assert await processor.process_event(foreign) == "rejected"
    assert await processor.process_event(duplicate) == "rejected"
    assert await processor.process_event(display_only) == "delivered"
    assert broker.calls[-1][1].metadata["buzz_mentioned_self"] is False
    assert len(broker.calls) == 2


@pytest.mark.asyncio
async def test_both_gates_signature_self_and_dedupe_fail_closed(registry):
    store, identity_pubkey = registry
    broker = FakeBroker()
    processor = _processor(store, identity_pubkey, broker)

    unlisted = _event(channel=OTHER_CHANNEL)
    unapproved = _event(signer=STRANGER)
    invalid = _event(content="signed")
    invalid["content"] = "tampered"
    self_event = BuzzNostrSigner(bytes.fromhex(AGENT_KEY)).sign_event(
        kind=9,
        tags=[["h", CHANNEL]],
        content="self",
    )
    valid = _event(content="once")

    assert await processor.process_event(unlisted) == "unlisted_channel"
    assert await processor.process_event(unapproved) == "unapproved_principal"
    assert await processor.process_event(invalid) == "rejected"
    assert await processor.process_event(self_event) == "rejected"
    assert await processor.process_event(valid) == "delivered"
    assert await processor.process_event(valid) == "duplicate"
    assert len(broker.calls) == 1


@pytest.mark.asyncio
async def test_policy_drops_log_once_per_tuple_without_full_pubkeys(registry, capsys):
    store, identity_pubkey = registry
    broker = FakeBroker()
    now = [time.time()]
    processor = BuzzInboundProcessor(
        registry=store,
        broker=broker,
        agent_name="barsik",
        identity_pubkey=identity_pubkey,
        community_id=COMMUNITY,
        relay_url=RELAY,
        clock=lambda: now[0],
    )
    foreign_p = _event(
        signer=OWNER,
        content="@Ryan reply here again",
        p_tags=[STRANGER.pubkey],
    )
    unverified = _event(signer=STRANGER, content="no")
    unknown_kind = _event(signer=USER, kind=42, content="unknown")

    assert await processor.process_event(foreign_p) == "rejected"
    assert await processor.process_event(unverified) == "unapproved_principal"
    assert await processor.process_event(unknown_kind) == "rejected"
    assert broker.calls == []

    lines = capsys.readouterr().err.splitlines()
    assert len(lines) == 3
    assert any(
        f"reason=foreign_p sender=buzz:{COMMUNITY}:{OWNER.pubkey[:12]}… channel={CHANNEL}" in line
        for line in lines
    )
    assert any(
        f"reason=unverified_sender sender=buzz:{COMMUNITY}:{STRANGER.pubkey[:12]}… "
        f"channel={CHANNEL}" in line
        for line in lines
    )
    assert any(
        f"reason=unknown_kind sender=buzz:{COMMUNITY}:{USER.pubkey[:12]}… channel={CHANNEL}" in line
        for line in lines
    )
    for pubkey in (OWNER.pubkey, USER.pubkey, STRANGER.pubkey):
        assert pubkey not in "\n".join(lines)
    assert all(len(line) < 192 for line in lines)

    assert await processor.process_event(foreign_p) == "rejected"
    assert (
        await processor.process_event(_event(signer=STRANGER, content="still chatty"))
        == "unapproved_principal"
    )
    assert await processor.process_event(unknown_kind) == "rejected"
    assert capsys.readouterr().err == ""

    now[0] += buzz_inbound._POLICY_DROP_LOG_INTERVAL_SECONDS
    assert (
        await processor.process_event(_event(signer=STRANGER, content="after window"))
        == "unapproved_principal"
    )
    summary = capsys.readouterr().err
    assert summary.count("\n") == 1
    assert "reason=unverified_sender" in summary
    assert "suppressed_since_last=1" in summary
    assert STRANGER.pubkey not in summary


@pytest.mark.asyncio
async def test_membership_events_never_expand_channels_or_reach_persona(registry):
    store, identity_pubkey = registry
    broker = FakeBroker()
    processor = _processor(store, identity_pubkey, broker)
    before = store.get_buzz_inbound_policy("barsik")["channels"]
    membership = USER.sign_event(
        kind=30000,
        tags=[["h", OTHER_CHANNEL]],
        content="join",
    )

    assert await processor.process_event(membership) == "rejected"
    assert broker.calls == []
    assert store.get_buzz_inbound_policy("barsik")["channels"] == before
    assert store.get_buzz_inbound_channel("barsik", COMMUNITY, RELAY, OTHER_CHANNEL) is None


def test_membership_validation_requires_exact_self_p_and_canonical_h(registry):
    _store, identity_pubkey = registry
    valid = RELAY_AUTHORITY.sign_event(
        kind=44100,
        tags=[["p", identity_pubkey], ["h", OTHER_CHANNEL], ["name", "#support"]],
        content="",
    )
    membership = validate_buzz_membership_event(
        valid,
        identity_pubkey=identity_pubkey,
        relay_signing_pubkey=RELAY_AUTHORITY.pubkey,
    )
    assert (membership.channel_id, membership.chat_title) == (OTHER_CHANNEL, "#support")

    foreign = RELAY_AUTHORITY.sign_event(
        kind=44100,
        tags=[["p", STRANGER.pubkey], ["h", OTHER_CHANNEL]],
        content="",
    )
    duplicate = RELAY_AUTHORITY.sign_event(
        kind=44100,
        tags=[
            ["p", identity_pubkey],
            ["p", identity_pubkey],
            ["h", OTHER_CHANNEL],
        ],
        content="",
    )
    with pytest.raises(BuzzInboundRejectedError, match="membership_target_invalid"):
        validate_buzz_membership_event(
            foreign,
            identity_pubkey=identity_pubkey,
            relay_signing_pubkey=RELAY_AUTHORITY.pubkey,
        )
    with pytest.raises(BuzzInboundRejectedError, match="membership_target_invalid"):
        validate_buzz_membership_event(
            duplicate,
            identity_pubkey=identity_pubkey,
            relay_signing_pubkey=RELAY_AUTHORITY.pubkey,
        )


@pytest.mark.parametrize("kind", [44100, 44101])
@pytest.mark.parametrize(
    "signer",
    [OWNER, USER, STRANGER],
    ids=["owner", "other-user", "stranger-55-scalar"],
)
def test_membership_validation_rejects_every_non_relay_signer(
    registry,
    kind,
    signer,
):
    _store, identity_pubkey = registry
    forged = signer.sign_event(
        kind=kind,
        tags=[["p", identity_pubkey], ["h", OTHER_CHANNEL]],
        content="",
    )

    with pytest.raises(BuzzInboundRejectedError, match="membership_signer_invalid"):
        validate_buzz_membership_event(
            forged,
            identity_pubkey=identity_pubkey,
            relay_signing_pubkey=RELAY_AUTHORITY.pubkey,
        )


@pytest.mark.asyncio
async def test_community_scope_is_part_of_both_authorization_gates(registry):
    store, identity_pubkey = registry
    broker = FakeBroker()
    wrong_community = BuzzInboundProcessor(
        registry=store,
        broker=broker,
        agent_name="barsik",
        identity_pubkey=identity_pubkey,
        community_id="other-community",
        relay_url=RELAY,
    )

    assert await wrong_community.process_event(_event()) == "unlisted_channel"
    assert broker.calls == []


@pytest.mark.asyncio
async def test_kind_20002_is_verified_but_never_retained_or_dispatched(registry):
    store, identity_pubkey = registry
    broker = FakeBroker()
    processor = _processor(store, identity_pubkey, broker)
    ephemeral = _event(kind=20002, content="")
    policy_before = store.get_buzz_inbound_policy("barsik")
    changes_before = store._db.total_changes

    assert await processor.process_event(ephemeral) == "ephemeral_ignored"
    assert await processor.process_event(ephemeral) == "ephemeral_ignored"
    assert broker.calls == []
    assert processor._recent_ids == set()
    assert store.get_buzz_inbound_policy("barsik") == policy_before
    assert store._db.total_changes == changes_before
    assert store._db.execute("SELECT COUNT(*) FROM buzz_inbound_events").fetchone()[0] == 0
    with pytest.raises(ValueError, match="kind-9"):
        store.begin_buzz_inbound_event_delivery(
            "barsik",
            ephemeral,
            community_id=COMMUNITY,
            channel_id=CHANNEL,
        )
    with pytest.raises(sqlite3.IntegrityError):
        store._db.execute(
            """INSERT INTO buzz_inbound_events
               (agent, event_id, community_id, channel_id, author_pubkey, kind,
                event_created_at, event_json, claimed_at, attempts)
               VALUES (?, ?, ?, ?, ?, 20002, ?, '{}', 1, 1)""",
            (
                "barsik",
                ephemeral["id"],
                COMMUNITY,
                CHANNEL,
                USER.pubkey,
                ephemeral["created_at"],
            ),
        )
    store._db.rollback()


@pytest.mark.asyncio
async def test_subscription_floor_rejects_stale_relay_event_but_pending_replay_is_allowed(
    registry,
):
    store, identity_pubkey = registry
    now = int(time.time())
    stale = _event(content="historic signed command", created_at=now - 86400)
    broker = FakeBroker()
    processor = _processor(store, identity_pubkey, broker)

    assert await processor.process_event(stale, subscription_since=now - 60) == "rejected"
    assert broker.calls == []
    assert store._db.execute("SELECT COUNT(*) FROM buzz_inbound_events").fetchone()[0] == 0

    # Only the daemon's already-verified durable retry ledger may bypass a
    # newly computed relay subscription floor after restart/reconnect.
    assert store.begin_buzz_inbound_event_delivery(
        "barsik",
        stale,
        community_id=COMMUNITY,
        channel_id=CHANNEL,
    )
    store.mark_buzz_inbound_event_retry("barsik", stale["id"], "interrupted")
    assert await processor.replay_pending() == 1
    assert [call[1].content for call in broker.calls] == ["historic signed command"]


@pytest.mark.asyncio
async def test_pending_kind9_replays_after_failed_handoff_without_replaying_delivered(registry):
    store, identity_pubkey = registry
    event = _event(content="survive restart")
    failing = _processor(store, identity_pubkey, FakeBroker(accepted=False))

    with pytest.raises(BuzzInboundDeliveryError):
        await failing.process_event(event)
    row = store._db.execute(
        "SELECT delivery_status, attempts, last_error FROM buzz_inbound_events"
    ).fetchone()
    assert row == ("pending", 1, "BuzzInboundDeliveryError")

    success_broker = FakeBroker()
    resumed = _processor(store, identity_pubkey, success_broker)
    assert await resumed.replay_pending() == 1
    assert len(success_broker.calls) == 1
    assert await resumed.process_event(event) == "duplicate"
    assert store._db.execute(
        "SELECT delivery_status, attempts, event_json FROM buzz_inbound_events"
    ).fetchone() == ("delivered", 2, "")


def test_pending_claim_is_fenced_and_released_only_after_process_restart(registry):
    store, _ = registry
    event = _event(content="single claimant")
    assert store.begin_buzz_inbound_event_delivery(
        "barsik",
        event,
        community_id=COMMUNITY,
        channel_id=CHANNEL,
    )
    store.mark_buzz_inbound_event_retry("barsik", event["id"], "handoff_failed")

    assert store.begin_buzz_inbound_event_delivery(
        "barsik",
        event,
        community_id=COMMUNITY,
        channel_id=CHANNEL,
        replay=True,
    )
    assert not store.begin_buzz_inbound_event_delivery(
        "barsik",
        event,
        community_id=COMMUNITY,
        channel_id=CHANNEL,
        replay=True,
    )
    assert store.list_pending_buzz_inbound_events("barsik") == []

    assert store.reset_buzz_inbound_event_claims_after_restart() == 1
    assert store.list_pending_buzz_inbound_events("barsik") == [event]


@pytest.mark.asyncio
async def test_policy_replace_discards_pending_events_with_revoked_authority(registry):
    store, identity_pubkey = registry
    pending = _event(content="must not survive revocation")
    processor = _processor(store, identity_pubkey, FakeBroker(accepted=False))
    with pytest.raises(BuzzInboundDeliveryError):
        await processor.process_event(pending)
    assert store.list_pending_buzz_inbound_events("barsik") == [pending]

    store.configure_buzz_inbound_owner_control(
        "barsik",
        owner_pubkey=OWNER.pubkey,
        channels=[{"channel_id": OTHER_CHANNEL, "label": "#ops"}],
        approved_users=[],
        owner_actor="ui:admin",
    )

    assert store.list_pending_buzz_inbound_events("barsik") == []
    assert (
        store._db.execute(
            "SELECT COUNT(*) FROM buzz_inbound_events WHERE event_id=?",
            (pending["id"],),
        ).fetchone()[0]
        == 0
    )


def test_owner_rotation_silence_guard_is_durable_and_resets_on_verified_seen(registry):
    store, _ = registry
    policy = store.get_buzz_inbound_policy("barsik")
    due_at = policy["owner_configured_at"] + 15 * 86400

    due = store.buzz_owner_silence_alert_due("barsik", now=due_at)
    assert due["owner_principal"] == f"buzz:{COMMUNITY}:{OWNER.pubkey}"
    store.mark_buzz_owner_silence_notified("barsik", notified_at=due_at)
    assert store.buzz_owner_silence_alert_due("barsik", now=due_at + 10) is None

    store.note_buzz_inbound_principal_seen("barsik", COMMUNITY, OWNER.pubkey, due_at + 20)
    assert store.buzz_owner_silence_alert_due("barsik", now=due_at + 20 + 13 * 86400) is None
    assert store.buzz_owner_silence_alert_due("barsik", now=due_at + 20 + 15 * 86400) is not None


def test_strict_bounds_future_and_malformed_tags_are_rejected(registry):
    _, identity_pubkey = registry
    now = int(time.time())
    future = _event(created_at=now + 301)
    malformed_h = USER.sign_event(kind=9, tags=[["h", CHANNEL, "extra"]], content="x")
    noncanonical_h = USER.sign_event(
        kind=9,
        tags=[["h", "AAAAAAAA-0000-4000-8000-000000000001"]],
        content="x",
    )
    malformed_p = USER.sign_event(kind=9, tags=[["h", CHANNEL], ["p", "ABC"]], content="x")

    for event in (future, malformed_h, noncanonical_h, malformed_p):
        with pytest.raises(BuzzInboundRejectedError):
            validate_buzz_inbound_event(event, identity_pubkey=identity_pubkey, now=now)
