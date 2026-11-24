"""Existing transport paths must reserve relay destinations before dispatch."""

import json
import sqlite3
from types import SimpleNamespace

import pytest

from pinky_daemon.ferry.config import FerryConfig
from pinky_daemon.ferry.host_pinky import HostPinky
from pinky_daemon.ferry.outbound import (
    HttpMeshSender,
    MeshSender,
    SendResult,
    build_envelope,
    select_mesh_sender,
)
from pinky_federation.fingerprint import fingerprint
from pinky_federation.keys import EncryptionKeyPair, SigningKeyPair
from pinky_federation.sealed_box import seal, unseal
from tests.test_codex_launch_env_boundary import KINDS, harness as harness


def _environment():
    return {
        "PINKYBOT_FLEET_NAME": "local",
        "PINKYBOT_FERRY_ENABLED": "1",
        "PINKYBOT_FERRY_BIND_HOST": "127.0.0.1",
        "PINKYBOT_FERRY_SHARED_SECRET": "synthetic-global-credential-0123456789",
        "PINKYBOT_FERRY_PEERS": json.dumps(
            {"peer-one": "http://peer-one.example.invalid:8899"}
        ),
        "PINKYBOT_FERRY_RELAY_PEERS": json.dumps(
            {"peer-one": {"fingerprint": "invalid", "signing_pk": "", "encryption_pk": ""}}
        ),
        "PINKYBOT_FERRY_RELAY_URL": "https://relay.example.invalid/ferry-relay/",
        "PINKYBOT_FERRY_RELAY_TOKEN": "synthetic-relay-credential-0123456789",
        "PINKYBOT_FERRY_RELAY_KEY_FILE": "/missing/synthetic-relay-key.json",
    }


@pytest.mark.parametrize("omitted", [None, "URL", "TOKEN", "KEY_FILE"])
def test_reserved_unusable_relay_never_calls_http_or_nats(monkeypatch, omitted):
    env = _environment()
    if omitted is not None:
        env.pop("PINKYBOT_FERRY_RELAY_" + omitted)
    calls = []

    def capture(transport):
        def send(self, envelope):
            calls.append(transport)
            return SendResult(True, envelope.correlation_id, "synthetic", 0)

        return send

    monkeypatch.setattr(HttpMeshSender, "send", capture("http"))
    monkeypatch.setattr(MeshSender, "send", capture("nats"))
    result = select_mesh_sender(FerryConfig.from_env(env)).send(
        build_envelope(
            from_="sender@local", to="receiver@peer-one", body={"kind": "msg", "text": "synthetic"}
        )
    )
    assert calls == [], "reserved relay destination reached a legacy transport"
    assert result.sent is False and result.error


def test_overlap_reservation_removes_http_entry_and_warns(caplog):
    config = FerryConfig.from_env(_environment())
    assert "peer-one" not in config.peers
    assert any("relay" in record.getMessage() for record in caplog.records)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("name", [
    "PINKYBOT_FERRY_RELAY_TOKEN", "PINKYBOT_FERRY_RELAY_KEY_FILE",
])
def test_relay_credentials_are_absent_from_actual_codex_payload(harness, kind, name):
    sentinel = "synthetic-relay-private-value-0123456789"
    harness.patch.setenv(name, sentinel)
    _, build = harness.make(kind)
    payload = build()
    assert name not in payload, "relay daemon authority entered an agent payload"
    assert sentinel not in json.dumps(payload)


@pytest.mark.asyncio
async def test_acl_load_transient_is_explicitly_pre_dispatch():
    class Registry:
        def has_agent(self, name):
            return name == "receiver"

        def get_peer_fleet_acl(self, name):
            raise sqlite3.OperationalError("synthetic unavailable ACL store")

    dispatched = []

    async def dispatch(*args, **kwargs):
        dispatched.append((args, kwargs))

    host = HostPinky(
        registry=Registry(), broker=SimpleNamespace(handle_inbound=dispatch), fleet_name="local"
    )
    result = await host.deliver(
        build_envelope(
            from_="sender@peer-one", to="receiver@local", body={"kind": "msg", "text": "synthetic"}
        )
    )
    assert result.status == "transient_failure"
    assert getattr(result, "pre_dispatch", False) is True
    assert dispatched == []


def test_nonreserved_http_selection_is_unchanged():
    env = _environment()
    env.pop("PINKYBOT_FERRY_RELAY_PEERS")
    assert isinstance(select_mesh_sender(FerryConfig.from_env(env)), HttpMeshSender)


def test_existing_seal_api_hides_and_recovers_the_plaintext():
    signing = SigningKeyPair.generate()
    encryption = EncryptionKeyPair.generate()
    recipient = EncryptionKeyPair.generate()
    recipient_signing = SigningKeyPair.generate()
    own_fp = fingerprint("peer-one", signing.public_key, encryption.public_key)
    recipient_fp = fingerprint("local", recipient_signing.public_key, recipient.public_key)
    plaintext = b"synthetic-sealed-plaintext-sentinel-0123456789"
    envelope = seal(
        plaintext,
        sender_signing=signing,
        sender_fingerprint=own_fp,
        recipient_encryption=recipient.public_key,
        recipient_fingerprint=recipient_fp,
    )
    assert plaintext not in envelope.to_bytes()
    assert len(own_fp.hex()) == len(recipient_fp.hex()) == 32
    assert unseal(
        envelope, recipient_encryption=recipient, sender_signing_public=signing.public_key
    ) == plaintext
