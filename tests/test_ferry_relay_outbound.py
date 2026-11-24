"""Real sealed outbound bytes traverse the relay without legacy fallback."""

import asyncio
import sys

import pytest

from pinky_daemon.ferry.config import FerryConfig
from pinky_daemon.ferry.outbound import build_envelope, envelope_to_wire, select_mesh_sender
from pinky_federation.envelope import Envelope
from pinky_federation.sealed_box import unseal
from tests.test_ferry_relay_config import TOKEN, relay_environment
from tests.test_relay_loopback import serve_relay

pytestmark = pytest.mark.skipif(sys.version_info < (3, 12), reason="relay requires Python >= 3.12")


async def test_real_outbound_decoded_stored_blob_hides_plaintext_and_unseals_exact_wire(tmp_path):
    tokens = {"local": TOKEN, "peer-one": "synthetic-recipient-token-0123456789012345"}
    async with serve_relay(tmp_path / "server", tokens) as (url, store):
        env, own, peer = relay_environment(tmp_path, url=url)
        config = FerryConfig.from_env(env)
        assert not config.enabled, "relay-only sending must not enable the direct listener"
        sentinel = "synthetic-plaintext-only-sentinel-0123456789"
        message = build_envelope(from_="sender@local", to="receiver@peer-one", body={"kind": "msg", "text": sentinel})
        plaintext = envelope_to_wire(message).encode()
        assert sentinel.encode() in plaintext
        result = await asyncio.to_thread(select_mesh_sender(config).send, message)
        assert result.sent and result.error is None
        row = store._db.execute("SELECT sender_fleet,to_fleet,blob FROM messages").fetchone()
        assert row[:2] == ("local", "peer-one")
        assert sentinel.encode() not in row[2] and sentinel not in repr(row)
        outer = Envelope.from_bytes(row[2])
        assert outer.sender_fingerprint == own.fingerprint
        assert outer.recipient_fingerprint == peer.fingerprint
        assert unseal(outer, recipient_encryption=peer.encryption, sender_signing_public=own.signing.public_key) == plaintext


async def test_reserved_relay_unreachable_returns_fixed_error_and_never_legacy(tmp_path, monkeypatch):
    from pinky_daemon.ferry.outbound import HttpMeshSender, MeshSender

    env, own, peer = relay_environment(tmp_path, url="http://127.0.0.1:1/ferry-relay/")
    calls = []
    monkeypatch.setattr(HttpMeshSender, "send", lambda *a: calls.append("http"))
    monkeypatch.setattr(MeshSender, "send", lambda *a: calls.append("nats"))
    sender = select_mesh_sender(FerryConfig.from_env(env))
    message = build_envelope(from_="sender@local", to="receiver@peer-one", body={"kind": "msg", "text": "fixture"})
    result = await asyncio.to_thread(sender.send, message)
    assert not result.sent and result.error.startswith("relay request failed (")
    assert calls == [] and TOKEN not in result.error
    assert env["PINKYBOT_FERRY_RELAY_KEY_FILE"] not in result.error
