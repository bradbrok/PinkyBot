"""Peer credentials constrain sender identity and outbound bearer selection."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.ferry.config import FerryConfig
from pinky_daemon.ferry.inbound_server import build_ferry_app
from pinky_daemon.ferry.outbound import HttpMeshSender, build_envelope, envelope_to_wire
from pinky_daemon.ferry.types import DeliveryResult

GLOBAL_SECRET = "synthetic-global-credential-0123456789"
PEER_SECRET = "synthetic-peer-credential-012345678901"


def _environment(**overrides):
    result = {
            "PINKYBOT_FLEET_NAME": "local",
            "PINKYBOT_FERRY_ENABLED": "1",
            "PINKYBOT_FERRY_BIND_HOST": "127.0.0.1",
            "PINKYBOT_FERRY_SHARED_SECRET": GLOBAL_SECRET,
            "PINKYBOT_FERRY_PEER_SECRETS": json.dumps({"peer-one": PEER_SECRET}),
            "PINKYBOT_FERRY_PEERS": json.dumps(
                {
                    "peer-one": "http://peer-one.example.invalid:8899",
                    "peer-two": "http://peer-two.example.invalid:8899",
                }
            ),
        }
    result.update(overrides)
    return result


def _config(**overrides) -> FerryConfig:
    return FerryConfig.from_env(_environment(**overrides))


class _DeliveryRecorder:
    def __init__(self):
        self.delivered = []

    async def deliver(self, envelope):
        self.delivered.append(envelope)
        return DeliveryResult(status="delivered")


def _post(client, *, token: str, sender: str):
    envelope = build_envelope(
        from_=sender,
        to="receiver@local",
        body={"kind": "msg", "text": "synthetic message"},
    )
    return client.post(
        "/ferry/inbound",
        json=json.loads(envelope_to_wire(envelope)),
        headers={"Authorization": f"Bearer {token}"},
    )


@pytest.mark.parametrize("sender", ["ferry://peer-two/sender", "sender@peer-two"])
def test_peer_token_cannot_claim_another_fleet(sender):
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=_config())) as client:
        response = _post(client, token=PEER_SECRET, sender=sender)
    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "sender fleet does not match credential"}
    assert recorder.delivered == []


@pytest.mark.parametrize("sender", ["ferry://peer-one/sender", "sender@peer-one"])
def test_global_token_cannot_claim_a_peer_credential_fleet(sender):
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=_config())) as client:
        response = _post(client, token=GLOBAL_SECRET, sender=sender)
    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "sender fleet does not match credential"}
    assert recorder.delivered == []


@pytest.mark.parametrize("sender", ["ferry://peer-two/sender", "sender@peer-two"])
def test_global_token_delivers_from_an_unassigned_fleet(sender):
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=_config())) as client:
        response = _post(client, token=GLOBAL_SECRET, sender=sender)
    assert response.status_code == 200
    assert response.json() == {"ok": True, "status": "delivered", "reason": None}
    assert [envelope.from_ for envelope in recorder.delivered] == [sender]


class _HttpResponse:
    def getcode(self):
        return 200

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_outbound_selects_the_target_fleets_credential(monkeypatch):
    captured = []

    def urlopen(request, timeout=None):
        captured.append(request)
        return _HttpResponse()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    sender = HttpMeshSender(config=_config())
    for fleet in ("peer-two", "peer-one"):
        result = sender.send(
            build_envelope(
                from_="sender@local",
                to=f"receiver@{fleet}",
                body={"kind": "msg", "text": "synthetic message"},
            )
        )
        assert result.sent is True
        assert result.error is None

    assert len(captured) == 2
    assert captured[0].full_url == "http://peer-two.example.invalid:8899/ferry/inbound"
    assert captured[0].get_header("Authorization") == f"Bearer {GLOBAL_SECRET}"
    assert captured[1].full_url == "http://peer-one.example.invalid:8899/ferry/inbound"
    assert captured[1].get_header("Authorization") == f"Bearer {PEER_SECRET}"
    assert GLOBAL_SECRET not in json.dumps(dict(captured[1].header_items()))


@pytest.mark.parametrize("sender", ["ferry://peer-one/sender", "sender@peer-one"])
def test_peer_token_delivers_only_as_its_own_fleet(sender):
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=_config())) as client:
        response = _post(client, token=PEER_SECRET, sender=sender)
    assert response.status_code == 200
    assert [envelope.from_ for envelope in recorder.delivered] == [sender]


@pytest.mark.parametrize("sender", [
    "", "sender", "sender@", "@peer-one", "ferry://peer-one", "ferry://peer-one/",
    "ferry://peer-one//", "ferry://peer-one/sender/extra", "ferry://peer-one/a b",
])
def test_peer_token_refuses_malformed_or_incomplete_sender(sender):
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=_config())) as client:
        response = _post(client, token=PEER_SECRET, sender=sender)
    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "sender fleet does not match credential"}
    assert recorder.delivered == []


@pytest.mark.parametrize("token", ["", "synthetic-unknown-credential-0123456789"])
@pytest.mark.parametrize("global_secret", [GLOBAL_SECRET, ""])
def test_unknown_and_empty_bearers_never_match(token, global_secret):
    recorder = _DeliveryRecorder()
    config = _config(PINKYBOT_FERRY_SHARED_SECRET=global_secret)
    with TestClient(build_ferry_app(host_pinky=recorder, config=config)) as client:
        response = _post(client, token=token, sender="sender@peer-one")
    assert response.status_code == 401
    assert response.json() == {"ok": False, "error": "unauthorized"}
    assert recorder.delivered == []


@pytest.mark.parametrize("sender", ["ferry://peer-one/sender", "sender@peer-one"])
def test_empty_peer_map_preserves_global_delivery(sender):
    recorder = _DeliveryRecorder()
    config = _config(PINKYBOT_FERRY_PEER_SECRETS="{}")
    with TestClient(build_ferry_app(host_pinky=recorder, config=config)) as client:
        response = _post(client, token=GLOBAL_SECRET, sender=sender)
    assert response.status_code == 200
    assert len(recorder.delivered) == 1


def test_missing_all_secrets_preserves_disabled_reason_and_send_error():
    env = _environment(PINKYBOT_FERRY_SHARED_SECRET="", PINKYBOT_FERRY_PEER_SECRETS="{}")
    config = FerryConfig.from_env(env)
    assert config.enabled is False
    assert config.why_disabled(env) == "PINKYBOT_FERRY_SHARED_SECRET not set"
    sender = HttpMeshSender(config=config)
    assert sender.configured is False
    result = sender.send(build_envelope(from_="sender@local", to="receiver@peer-one", body={"kind": "msg"}))
    assert result.sent is False
    assert result.error == "ferry shared secret not configured"
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=config)) as client:
        response = _post(client, token="", sender="sender@peer-one")
    assert response.status_code == 503
    assert response.json() == {"ok": False, "error": "ferry not configured"}
    assert recorder.delivered == []


def test_peer_only_readiness_and_outbound_missing_target_credential(monkeypatch):
    config = _config(PINKYBOT_FERRY_SHARED_SECRET="")
    assert config.enabled is True
    assert config.why_disabled(_environment()) == "enabled"
    sender = HttpMeshSender(config=config)
    assert sender.configured is True
    captured = []

    def urlopen(request, timeout=None):
        captured.append(request)
        return _HttpResponse()

    monkeypatch.setattr("urllib.request.urlopen", urlopen)
    for fleet in ("peer-one", "peer-two"):
        result = sender.send(build_envelope(from_="sender@local", to=f"receiver@{fleet}", body={"kind": "msg"}))
        assert result.sent is (fleet == "peer-one")
        assert result.error == (None if fleet == "peer-one" else "ferry shared secret not configured")
    assert len(captured) == 1
    assert captured[0].get_header("Authorization") == f"Bearer {PEER_SECRET}"
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=config)) as client:
        response = _post(client, token=PEER_SECRET, sender="sender@peer-one")
    assert response.status_code == 200
    assert len(recorder.delivered) == 1


@pytest.mark.parametrize("raw", ["{", "peer-one=value", "[]", "null", "true", "42"])
def test_invalid_peer_map_warns_once_without_echoing_input(caplog, raw):
    with caplog.at_level("WARNING", logger="pinky_daemon.ferry.config"):
        config = _config(PINKYBOT_FERRY_PEER_SECRETS=raw)
    assert config.peer_secrets == {}
    records = [record for record in caplog.records if record.name == "pinky_daemon.ferry.config"]
    assert len(records) == 1
    assert records[0].getMessage() == "ferry peer secrets ignored: expected a JSON object"


@pytest.mark.parametrize("value", ["", "x" * 31, 7, None, False, [], {}])
def test_invalid_peer_value_is_dropped_with_a_names_only_warning(caplog, value):
    config = _config(PINKYBOT_FERRY_PEER_SECRETS=json.dumps({"peer-one": value}))
    assert config.peer_secrets == {}
    records = [record.getMessage() for record in caplog.records if record.name == "pinky_daemon.ferry.config"]
    assert len(records) == 1
    assert '"peer-one"' in records[0]
    if isinstance(value, str) and value:
        absent = value not in records[0]
        assert absent, "configured secret appeared in warning"


@pytest.mark.parametrize("fleet", ["local", "", "*", "bad/name", "bad@name", "bad name", "bad\nname", "é-peer"])
def test_invalid_or_local_fleet_key_is_dropped_without_secret_values(caplog, fleet):
    config = _config(PINKYBOT_FERRY_PEER_SECRETS=json.dumps({fleet: PEER_SECRET}))
    assert config.peer_secrets == {}
    records = [record.getMessage() for record in caplog.records if record.name == "pinky_daemon.ferry.config"]
    assert len(records) == 1
    assert json.dumps(fleet) in records[0]
    absent = PEER_SECRET not in records[0] and GLOBAL_SECRET not in records[0]
    assert absent, "configured secret appeared in warning"
    assert "\n" not in records[0]


def test_peer_secret_equal_to_global_is_dropped_without_echo(caplog):
    config = _config(PINKYBOT_FERRY_PEER_SECRETS=json.dumps({"peer-one": GLOBAL_SECRET}))
    assert config.peer_secrets == {}
    records = [record.getMessage() for record in caplog.records if record.name == "pinky_daemon.ferry.config"]
    assert len(records) == 1
    assert "equals global credential" in records[0]
    absent = GLOBAL_SECRET not in records[0]
    assert absent, "configured secret appeared in warning"


def test_all_peers_sharing_a_secret_are_dropped(caplog):
    distinct = "synthetic-distinct-credential-0123456789"
    config = _config(PINKYBOT_FERRY_PEER_SECRETS=json.dumps({
        "peer-one": PEER_SECRET, "peer-two": PEER_SECRET, "peer-three": distinct,
    }))
    assert set(config.peer_secrets) == {"peer-three"}
    assert config.peer_secrets["peer-three"] == distinct
    records = [record.getMessage() for record in caplog.records if record.name == "pinky_daemon.ferry.config"]
    assert len(records) == 2
    assert all("duplicate credential" in line for line in records)
    assert all(secret not in "\n".join(records) for secret in (PEER_SECRET, GLOBAL_SECRET, distinct))


def test_json_peer_secrets_keep_commas_and_equals_and_diagnostics_are_names_only():
    token = "synthetic,credential=with=punctuation-0123456789"
    config = _config(PINKYBOT_FERRY_PEER_SECRETS=json.dumps({"peer-two": token, "peer-one": PEER_SECRET}))
    assert config.secret_for_fleet("peer-two") == token
    assert config.diagnostics()["peer_secret_fleets"] == ["peer-one", "peer-two"]
    report = json.dumps(HttpMeshSender(config=config).diagnostics())
    absent = all(secret not in report for secret in (token, PEER_SECRET, GLOBAL_SECRET))
    assert absent, "configured secret appeared in diagnostics"
    assert token not in repr(config) and PEER_SECRET not in repr(config)


@pytest.mark.parametrize("credential", ["global", "peer-one", "peer-two", "unknown", "empty"])
def test_authentication_compares_every_candidate_even_after_a_match(monkeypatch, credential):
    import hmac

    from pinky_daemon.ferry import inbound_server

    other = "synthetic-other-credential-012345678901"
    tokens = {"global": GLOBAL_SECRET, "peer-one": PEER_SECRET, "peer-two": other,
              "unknown": "synthetic-unknown-credential-0123456789", "empty": ""}
    config = _config(PINKYBOT_FERRY_PEER_SECRETS=json.dumps({"peer-one": PEER_SECRET, "peer-two": other}))
    seen = []

    def compare(left, right):
        seen.append(right)
        return hmac.compare_digest(left, right)

    monkeypatch.setattr(inbound_server, "hmac", SimpleNamespace(compare_digest=compare))
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=config)) as client:
        response = _post(client, token=tokens[credential], sender=f"sender@{credential if credential.startswith('peer-') else 'unassigned'}")
    all_compared = seen == [token.encode() for token in (GLOBAL_SECRET, PEER_SECRET, other)]
    assert all_compared, "authentication skipped a configured candidate"
    assert response.status_code == (401 if credential in {"unknown", "empty"} else 200)


@pytest.mark.parametrize("credential", ["peer", "global"])
def test_fleet_refusal_logs_one_names_only_line(caplog, credential):
    token = PEER_SECRET if credential == "peer" else GLOBAL_SECRET
    claimed = "peer-two" if credential == "peer" else "peer-one"
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=_config())) as client:
        response = _post(client, token=token, sender=f"sender@{claimed}")
    assert response.status_code == 403
    records = [record.getMessage() for record in caplog.records if record.name == "pinky_daemon.ferry.inbound_server"]
    assert len(records) == 1
    assert f'claimed_fleet="{claimed}"' in records[0]
    assert 'source_ip="testclient"' in records[0]
    assert all(secret not in records[0] for secret in (GLOBAL_SECRET, PEER_SECRET))
    assert recorder.delivered == []


def test_peer_credential_still_obeys_source_ip_allowlist():
    recorder = _DeliveryRecorder()
    config = _config(PINKYBOT_FERRY_ALLOWED_PEER_IPS="192.0.2.1")
    with TestClient(build_ferry_app(host_pinky=recorder, config=config)) as client:
        response = _post(client, token=PEER_SECRET, sender="sender@peer-one")
    assert response.status_code == 401
    assert recorder.delivered == []


@pytest.mark.parametrize("message", ["delivery acknowledged", "delivery refused"])
async def test_async_broker_reply_uses_the_peer_credential(tmp_path, monkeypatch, message):
    from pinky_daemon.api import create_api

    app = create_api(db_path=str(tmp_path / "conversations.db"), default_working_dir=str(tmp_path))
    captured = []

    def urlopen(request, timeout=None):
        captured.append(request)
        return _HttpResponse()

    try:
        app.state.agents.register("sender", model="sonnet", working_dir=str(tmp_path / "sender"))
        app.state.agents.set_mesh_outbound_allowlist("sender", ["*@peer-one"])
        for name, value in _environment().items():
            monkeypatch.setenv(name, value)
        monkeypatch.setattr("urllib.request.urlopen", urlopen)
        result = await app.state.broker.send_callback(
            "sender", "ferry", "receiver@peer-one", message, reply_to="original-message"
        )
        assert result["sent"] is True
        assert len(captured) == 1
        assert captured[0].get_header("Authorization") == f"Bearer {PEER_SECRET}"
        assert GLOBAL_SECRET not in json.dumps(dict(captured[0].header_items()))
        payload = json.loads(captured[0].data)
        assert payload["reply_to"] == "original-message"
        assert payload["body"]["text"] == message
    finally:
        app.state.store_catalog.close()


def test_standalone_loader_independently_refuses_the_peer_secret_map(tmp_path):
    from tests.tmux_env_r3_support import payload, run_loader

    path = payload(tmp_path, env={"PINKYBOT_FERRY_PEER_SECRETS": PEER_SECRET})
    data = json.loads(path.read_text())
    data["inherit"] = "none"
    path.write_text(json.dumps(data))
    result = run_loader(path)
    assert result.returncode == 1
    assert result.stdout == b""
    assert result.stderr.strip() == b"launch environment load failed"
    assert PEER_SECRET.encode() not in result.stderr
    assert not path.exists()
