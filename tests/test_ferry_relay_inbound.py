"""Real crypto, poison isolation, committed handoffs and bounded retry pacing."""

import asyncio
import base64
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon.ferry.config import FerryConfig
from pinky_daemon.ferry.outbound import build_envelope, envelope_to_wire
from pinky_daemon.ferry.relay_identity import generate_identity
from pinky_daemon.ferry.relay_inbound import RelayPacer, RelayReceiver
from pinky_daemon.ferry.relay_replay import ReplayStoreError
from pinky_daemon.ferry.types import DeliveryResult
from pinky_federation.keys import SigningKeyPair
from pinky_federation.sealed_box import seal
from tests.test_ferry_relay_config import relay_environment
from tests.test_ferry_relay_replay import Clock, store_at


@pytest.fixture
def receiver(tmp_path):
    env, own, peer = relay_environment(tmp_path)
    clock = Clock()
    replay = store_at(tmp_path / "replay.db", clock)
    host = SimpleNamespace(deliver=AsyncMock(return_value=DeliveryResult("delivered")))
    value = RelayReceiver(FerryConfig.from_env(env).relay, replay, host)
    value.fixture = SimpleNamespace(env=env, own=own, peer=peer, clock=clock, path=tmp_path)
    yield value
    replay.close()


def sealed_row(receiver, *, fields=None, ident="row-one", mutate=None, signing=None,
               sender_fp=None, recipient_fp=None):
    fix = receiver.fixture
    message = build_envelope(from_="sender@peer-one", to="receiver@local",
                             body={"kind": "msg", "text": "synthetic-inner-plaintext-sentinel"})
    data = json.loads(envelope_to_wire(message))
    data.update(id="inner-one", ts=fix.clock.wall)
    if fields:
        data.update(fields)
    plaintext = json.dumps(data, separators=(",", ":")).encode()
    outer = seal(plaintext, sender_signing=signing or fix.peer.signing,
                 sender_fingerprint=sender_fp or fix.peer.fingerprint,
                 recipient_encryption=fix.own.encryption.public_key,
                 recipient_fingerprint=recipient_fp or fix.own.fingerprint)
    if mutate:
        outer = mutate(outer)
    return {"id": ident, "sender_fleet": "peer-one",
            "blob_b64": base64.b64encode(outer.to_bytes()).decode()}


async def test_authenticated_handoff_commits_before_deliver_and_terminal_precedes_ack(receiver):
    events = []

    async def deliver(message):
        state = receiver.replay._db.execute("SELECT state FROM relay_claims").fetchone()[0]
        events.append(("deliver", state))
        return DeliveryResult("delivered")

    async def ack(ids):
        state = receiver.replay._db.execute("SELECT state FROM relay_claims").fetchone()[0]
        events.append(("ack", state, ids))

    receiver.host.deliver = deliver
    row = sealed_row(receiver)
    client = SimpleNamespace(poll=AsyncMock(return_value=[row]), ack=ack)
    await receiver.run_once(client)
    assert events == [("deliver", "HANDING_OFF"), ("ack", "DELIVERED", ["row-one"])]
    data = b"".join(path.read_bytes() for path in receiver.fixture.path.glob("replay.db*"))
    assert b"synthetic-inner-plaintext-sentinel" not in data
    assert receiver.replay.snapshot()["states"] == {"DELIVERED": 1}


@pytest.mark.parametrize("attack", ["wrong-signer", "signature", "ciphertext", "sender-fp", "recipient-fp", "inner-from", "inner-to", "stamp"])
async def test_cryptographic_and_fleet_binding_failures_ack_without_replay_record(receiver, attack):
    kwargs = {}
    if attack == "wrong-signer":
        kwargs["signing"] = SigningKeyPair.generate()  # AEAD remains completely valid.
    elif attack in {"signature", "ciphertext"}:
        attr = attack
        def flip(outer):
            value = getattr(outer, attr)
            return replace(outer, **{attr: bytes([value[0] ^ 1]) + value[1:]})
        kwargs["mutate"] = flip
    elif attack == "sender-fp":
        kwargs["sender_fp"] = b"x" * 16
    elif attack == "recipient-fp":
        kwargs["recipient_fp"] = b"x" * 16  # Genuine sender, correct encryption key.
    elif attack == "inner-from":
        kwargs["fields"] = {"from": "sender@other-fleet"}
    elif attack == "inner-to":
        kwargs["fields"] = {"to": "receiver@other-fleet"}
    row = sealed_row(receiver, **kwargs)
    if attack == "stamp":
        row["sender_fleet"] = "other-fleet"
    assert await receiver.process_row(row) is True
    receiver.host.deliver.assert_not_awaited()
    assert receiver.replay.snapshot()["states"] == {}
    assert sum(receiver.counters.values()) == 1


@pytest.mark.parametrize("fields", [
    {"id": 42}, {"id": ""}, {"id": "x" * 129}, {"id": "non-ascii-é"}, {"id": "a\nb"},
    {"ts": "1800000000000"}, {"ts": True}, {"ts": 1.8e12}, {"ts": 2**63},
    {"ttl_ms": 0}, {"ttl_ms": True}, {"ttl_ms": "1"}, {"ttl_ms": 604800001}, {"ttl_ms": None},
])
async def test_strict_inner_types_are_checked_before_coercing_wire_helper(receiver, fields):
    assert await receiver.process_row(sealed_row(receiver, fields=fields))
    receiver.host.deliver.assert_not_awaited()
    assert receiver.replay.snapshot()["states"] == {}


@pytest.mark.parametrize("delta,ttl,accepted", [(600000, None, True), (600001, None, False), (-1000, 999, False), (-1000, 1000, True)])
async def test_exact_future_and_short_ttl_freshness_boundaries(receiver, delta, ttl, accepted):
    fields = {"ts": receiver.fixture.clock.wall + delta}
    if ttl is not None:
        fields["ttl_ms"] = ttl
    assert await receiver.process_row(sealed_row(receiver, fields=fields))
    assert receiver.host.deliver.await_count == int(accepted)


async def test_replay_and_equivocation_do_not_dispatch_again(receiver):
    row = sealed_row(receiver)
    assert await receiver.process_row(row)
    assert await receiver.process_row({**row, "id": "different-relay-id"})
    assert await receiver.process_row(sealed_row(receiver, fields={"body": {"kind": "msg", "text": "different"}}))
    assert receiver.host.deliver.await_count == 1
    assert receiver.counters["equivocation"] == 1
    assert receiver.replay.snapshot()["states"] == {"DELIVERED": 1}


async def test_two_pinned_senders_can_deliver_the_same_inner_id(receiver):
    second = generate_identity(receiver.fixture.path / "second.json", "peer-two")
    env = dict(receiver.fixture.env)
    env["PINKYBOT_FERRY_RELAY_PEERS"] = json.dumps({
        "peer-one": receiver.fixture.peer.public(), "peer-two": second.public(),
    })
    receiver.settings = FerryConfig.from_env(env).relay
    assert await receiver.process_row(sealed_row(receiver))
    row = sealed_row(receiver, fields={"from": "sender@peer-two"},
                     signing=second.signing, sender_fp=second.fingerprint)
    row["sender_fleet"] = "peer-two"
    assert await receiver.process_row(row)
    assert receiver.host.deliver.await_count == 2
    assert receiver.replay.snapshot()["states"] == {"DELIVERED": 2}


@pytest.mark.parametrize("result", ["queued", "rejected", "ambiguous", "exception", "cancel", "terminal-write"])
async def test_every_ambiguous_handoff_stays_terminal_without_second_dispatch(receiver, monkeypatch, result):
    row = sealed_row(receiver)
    if result in {"queued", "rejected"}:
        receiver.host.deliver.return_value = DeliveryResult(result)
    elif result == "ambiguous":
        receiver.host.deliver.return_value = DeliveryResult("transient_failure")
    elif result == "exception":
        receiver.host.deliver.side_effect = RuntimeError("synthetic-private-text")
    elif result == "cancel":
        receiver.host.deliver.side_effect = asyncio.CancelledError()
    else:
        monkeypatch.setattr(receiver.replay, "finish", lambda *a: (_ for _ in ()).throw(ReplayStoreError("unavailable")))
    if result == "cancel":
        with pytest.raises(asyncio.CancelledError):
            await receiver.process_row(row)
        receiver.host.deliver.side_effect = None
    else:
        assert await receiver.process_row(row)
    assert await receiver.process_row(row)
    assert receiver.host.deliver.await_count == 1
    expected = result.upper() if result in {"queued", "rejected"} else "UNKNOWN"
    assert receiver.replay.snapshot()["states"] == {expected: 1}


async def test_explicit_pre_dispatch_retry_is_durable_and_fifth_attempt_is_dropped(receiver):
    receiver.host.deliver.return_value = DeliveryResult("transient_failure", pre_dispatch=True)
    row = sealed_row(receiver)
    for attempt in range(1, 6):
        ack = await receiver.process_row(row)
        assert ack is (attempt == 5)
        state, count = receiver.replay._db.execute("SELECT state,attempts FROM relay_claims").fetchone()
        assert (state, count) == ("DROPPED" if attempt == 5 else "RETRY", attempt)
        if attempt < 5:
            assert await receiver.process_row(row) is False
            assert receiver.host.deliver.await_count == attempt
            receiver.fixture.clock.advance(1000 * 2 ** (attempt - 1))
    assert await receiver.process_row(row)
    assert receiver.host.deliver.await_count == 5


@pytest.mark.parametrize("failure", ["key", "database"])
async def test_local_failure_before_claim_never_acks_or_creates_claim(receiver, monkeypatch, failure):
    row = sealed_row(receiver)
    client = SimpleNamespace(poll=AsyncMock(return_value=[row]), ack=AsyncMock())
    if failure == "key":
        receiver.fixture.path.joinpath("local.json").chmod(0o640)
    else:
        monkeypatch.setattr(receiver.replay, "claim", lambda *a: (_ for _ in ()).throw(ReplayStoreError("unavailable")))
    with pytest.raises(Exception):
        await receiver.run_once(client)
    client.ack.assert_not_awaited()
    receiver.host.deliver.assert_not_awaited()
    assert receiver.replay.snapshot()["states"] == {}


async def test_clock_behind_floor_suspends_poison_ack_and_handoff(receiver):
    receiver.fixture.clock.advance(400000)
    receiver.replay.clock()
    receiver.fixture.clock.wall -= 400000
    client = SimpleNamespace(poll=AsyncMock(return_value=[{"id": "poison"}]), ack=AsyncMock())
    receiver.pacer.sleep = AsyncMock()
    await receiver.run_once(client)
    client.poll.assert_not_awaited()
    client.ack.assert_not_awaited()
    receiver.host.deliver.assert_not_awaited()
    assert receiver.state == "clock behind floor"


class PacingClock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []

    async def sleep(self, delay):
        self.sleeps.append(delay)
        self.now += delay

    def monotonic(self):
        return self.now


async def test_empty_repeated_pages_back_off_and_new_page_resets():
    clock = PacingClock()
    pacer = RelayPacer(monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(8):
        await pacer.after_page([], 0, 25)
    assert clock.sleeps == [1, 2, 4, 8, 16, 32, 60, 60]
    await pacer.after_page([{"id": "new"}], 0, 25)
    assert pacer.backoff == 0
    await pacer.after_page([{"id": "new"}], 0, 25)
    assert clock.sleeps[-1] == 1


async def test_alternating_pages_cannot_bypass_independent_poll_ceiling():
    clock = PacingClock()
    pacer = RelayPacer(monotonic=clock.monotonic, sleep=clock.sleep)
    for index in range(60):
        await pacer.before_poll()
        await pacer.after_page([{"id": "a" if index % 2 else "b"}], 0, 25)
    assert clock.now >= 110
    assert pacer.backoff == 0, "this fixture must not rely on repeated-page backoff"


async def test_fresh_poison_ids_still_consume_the_six_hundred_row_window():
    clock = PacingClock()
    pacer = RelayPacer(monotonic=clock.monotonic, sleep=clock.sleep)
    for _ in range(600):
        await pacer.before_row()
    assert clock.now == 0
    await pacer.before_row()
    assert clock.now >= 60
    assert len(pacer.rows) <= 600
