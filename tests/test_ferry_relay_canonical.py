"""Real API and reply sends refuse noncanonical relay fleet identities."""

import json
from contextlib import closing

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.ferry.config import FerryConfig
from pinky_daemon.ferry.outbound import (
    HttpMeshSender,
    MeshSender,
    SendResult,
    build_envelope,
    select_mesh_sender,
)
from pinky_federation.envelope import Envelope
from pinky_federation.sealed_box import unseal
from tests.test_ferry_relay_config import relay_environment


def _capture_transports(monkeypatch):
    relay, legacy = [], []

    async def send(self, fleet, blob):
        relay.append((fleet, blob))
        return "synthetic-relay-row"

    def other(self, envelope):
        legacy.append(envelope)
        return SendResult(True, envelope.correlation_id, "legacy", envelope.ts)

    monkeypatch.setattr("pinky_daemon.ferry.relay_http.RelayHTTPClient.send", send)
    monkeypatch.setattr(HttpMeshSender, "send", other)
    monkeypatch.setattr(MeshSender, "send", other)
    return relay, legacy


@pytest.mark.usefixtures("stub_sdk_transport")
@pytest.mark.parametrize("path", ["mesh", "reply"])
@pytest.mark.parametrize("case", ["lowercase", "own-uppercase", "target-uppercase", "peer-uppercase"])
def test_api_and_reply_paths_refuse_noncanonical_relay_fleets(tmp_path, monkeypatch, path, case):
    from pinky_daemon.api import create_api

    env, own, peer = relay_environment(tmp_path)
    if case == "own-uppercase":
        env["PINKYBOT_FLEET_NAME"] = "LOCAL"
    if case == "peer-uppercase":
        env["PINKYBOT_FERRY_RELAY_PEERS"] = json.dumps({"PEER-ONE": peer.public()})
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    target = "receiver@" + ("PEER-ONE" if case == "target-uppercase" else "peer-one")
    relay, legacy = _capture_transports(monkeypatch)
    config = FerryConfig.from_env()
    app = create_api(db_path=str(tmp_path / "conversations.db"), default_working_dir=str(tmp_path))
    audit = []
    try:
        app.state.agents.register("sender", model="sonnet", working_dir=str(tmp_path / "sender"))
        app.state.agents.set_mesh_outbound_allowlist("sender", ["*@peer-one", "*@PEER-ONE"])
        monkeypatch.setattr("pinky_daemon.mesh_store.MeshStore.log_message", lambda self, **kw: audit.append(kw))
        with closing(TestClient(app, raise_server_exceptions=False)) as client:
            if path == "mesh":
                response = client.post("/agents/sender/mesh/send", json={
                    "target": target, "body": {"text": "synthetic relay message"},
                    "reply_to": "synthetic-prior-message",
                })
            else:
                response = client.post("/broker/send", json={
                    "agent_name": "sender", "platform": "ferry", "chat_id": target,
                    "content": "synthetic relay message", "reply_to": "synthetic-prior-message",
                })
        if case == "lowercase":
            assert response.status_code == 200
            if path == "mesh":
                assert response.json()["sent"] is True
            assert config.relay.ready
            assert len(relay) == 1 and relay[0][0] == "peer-one"
            outer = Envelope.from_bytes(relay[0][1])
            wire = json.loads(unseal(outer, recipient_encryption=peer.encryption,
                                     sender_signing_public=own.signing.public_key))
            assert wire["from"] == "ferry://local/sender" and wire["to"] == target
            assert wire["reply_to"] == "synthetic-prior-message"
            expected = None
        else:
            expected = {
                "own-uppercase": "relay route unavailable: own relay fleet must be canonical",
                "target-uppercase": "relay envelope fleets must be canonical",
                "peer-uppercase": "relay route unavailable: relay peer fleet must be canonical",
            }[case]
            assert relay == [], "a noncanonical relay envelope reached the HTTP send boundary"
            if path == "mesh":
                assert response.status_code == 200
                assert response.json()["sent"] is False and response.json()["error"] == expected
            else:
                assert response.status_code == 502
                assert response.json()["detail"] == "ferry send failed: " + expected
            if case != "target-uppercase":
                assert not config.relay.ready
                assert config.relay.diagnostics()["reason"] == expected.removeprefix("relay route unavailable: ")
        assert legacy == [], "a reserved relay route fell back to a legacy sender"
        assert len(audit) == 1 and audit[0]["error"] == expected
    finally:
        app.state.store_catalog.close()


@pytest.mark.parametrize("sender,target", [
    ("sender@LOCAL", "receiver@peer-one"),
    ("ferry://LOCAL/sender", "ferry://peer-one/receiver"),
    ("sender@local", "receiver@PEER-ONE"),
    ("ferry://local/sender", "ferry://PEER-ONE/receiver"),
])
def test_ready_relay_rejects_either_noncanonical_envelope_fleet(tmp_path, monkeypatch, sender, target):
    env, own, peer = relay_environment(tmp_path)
    config = FerryConfig.from_env(env)
    assert config.relay.ready
    relay, legacy = _capture_transports(monkeypatch)
    result = select_mesh_sender(config).send(build_envelope(
        from_=sender, to=target, body={"kind": "msg", "text": "synthetic"},
    ))
    assert not result.sent
    assert result.error == "relay envelope fleets must be canonical"
    assert relay == legacy == []


def test_nonrelay_fleet_spelling_keeps_legacy_semantics(monkeypatch):
    calls = []
    config = FerryConfig.from_env({"PINKYBOT_FLEET_NAME": "LOCAL"})
    assert config.relay is None and config.fleet_name == "LOCAL"
    monkeypatch.setattr(MeshSender, "send", lambda self, message: calls.append(message)
                        or SendResult(True, None, "legacy", message.ts))
    message = build_envelope(from_="sender@LOCAL", to="receiver@PEER-ONE",
                             body={"kind": "msg", "text": "synthetic"})
    assert select_mesh_sender(config).send(message).sent
    assert calls == [message]
