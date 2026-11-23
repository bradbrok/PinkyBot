"""Canonical identities and header-safe credentials fail closed at each boundary."""

import http.client
import json
import urllib.error
from contextlib import closing
from dataclasses import replace
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.ferry.config import FerryConfig
from pinky_daemon.ferry.host_pinky import HostPinky
from pinky_daemon.ferry.inbound_server import build_ferry_app
from pinky_daemon.ferry.outbound import HttpMeshSender, parse_address
from pinky_daemon.ferry.types import AgentCardSelector
from tests.test_ferry_host_pinky import FakeBroker, FakeRegistry
from tests.test_ferry_peer_secrets import (
    GLOBAL_SECRET,
    PEER_SECRET,
    _config,
    _DeliveryRecorder,
    _environment,
    _post,
)


@pytest.mark.parametrize("character", ["\r", "\n", "\x00", "\t", "\x7f", "\x85", " ", "é", "\ud800"])
def test_peer_credential_rejects_non_visible_header_characters(caplog, character):
    sentinel = "synthetic-header-credential-0123456789"
    config = _config(PINKYBOT_FERRY_PEER_SECRETS=json.dumps({"peer-one": sentinel + character}))
    assert config.peer_secrets == {}
    records = [r for r in caplog.records if r.name == "pinky_daemon.ferry.config"]
    assert len(records) == 1
    assert "credential contains characters not allowed in a header" in records[0].getMessage()
    assert sentinel not in records[0].getMessage() + repr(records[0].args)


@pytest.mark.parametrize("character", ["\r", "\n", "\x00", "\t", "\x7f", "\x85", "\ud800", "\udcff"])
def test_global_credential_rejects_controls_and_non_utf8_before_stripping(caplog, character):
    sentinel = "synthetic-global-header-credential-0123456789"
    env = _environment(PINKYBOT_FERRY_SHARED_SECRET=sentinel + character, PINKYBOT_FERRY_PEER_SECRETS="{}")
    config = FerryConfig.from_env(env)
    assert config.shared_secret == ""
    assert not config.enabled
    records = [r for r in caplog.records if r.name == "pinky_daemon.ferry.config"]
    assert len(records) == 1
    assert records[0].getMessage() == (
        "ferry global credential ignored: credential contains characters not allowed in a header"
    )
    assert sentinel not in records[0].getMessage() + repr(records[0].args)


@pytest.mark.parametrize("secret", ["s", "working legacy credential", " legacy padded ", "é" * 32])
def test_working_legacy_global_credentials_keep_the_existing_strip_contract(secret):
    assert _config(PINKYBOT_FERRY_SHARED_SECRET=secret).shared_secret == secret.strip()


@pytest.mark.parametrize("invalid_candidate", ["global", "peer"])
def test_invalid_utf8_candidate_cannot_break_any_auth_path(invalid_candidate):
    invalid = "synthetic-invalid-utf8-credential-0123456789\udcff"
    config = _config(**({"PINKYBOT_FERRY_SHARED_SECRET": invalid} if invalid_candidate == "global" else {
        "PINKYBOT_FERRY_PEER_SECRETS": json.dumps({"peer-one": invalid})
    }))
    token = PEER_SECRET if invalid_candidate == "global" else GLOBAL_SECRET
    sender = "sender@peer-one" if invalid_candidate == "global" else "sender@peer-two"
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=config), raise_server_exceptions=False) as client:
        statuses = [_post(client, token=value, sender=sender).status_code for value in (token, "unknown", "")]
    assert statuses == [200, 401, 401]
    assert len(recorder.delivered) == 1


@pytest.mark.parametrize("sender", [
    " ferry://peer-one/sender", "ferry://peer-one/sender ", "\tferry://peer-one/sender",
    "ferry:// peer-one/sender", "ferry://peer-one /sender", " sender@peer-one",
])
def test_global_noncanonical_sender_cannot_cross_an_agent_only_acl(sender):
    selector = AgentCardSelector(agent_id="sender@peer-one", fleet=None)
    host = HostPinky(registry=FakeRegistry({"receiver": [selector]}), broker=FakeBroker(), fleet_name="local")
    assert host._acl_allows("receiver", sender), "the actual ACL parser must normalize this fixture"
    spy = AsyncMock(wraps=host.deliver)
    host.deliver = spy
    with TestClient(build_ferry_app(host_pinky=host, config=_config())) as client:
        response = _post(client, token=GLOBAL_SECRET, sender=sender)
    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "sender fleet does not match credential"}
    spy.assert_not_awaited()


@pytest.mark.parametrize("token", [GLOBAL_SECRET, PEER_SECRET])
@pytest.mark.parametrize("sender", [
    "ferry://peer-one/sender\t", "ferry://peer-one/sender\x00", "ferry://peer-one/sender\x85",
    "ferry://peer-one/ sender", "ferry://peer-one/", "ferry:///sender", "FERRY://peer-one/sender",
])
def test_every_credential_refuses_noncanonical_sender_before_delivery(token, sender):
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=_config())) as client:
        response = _post(client, token=token, sender=sender)
    assert response.status_code == 403
    assert response.json() == {"ok": False, "error": "sender fleet does not match credential"}
    assert not recorder.delivered


@pytest.mark.parametrize("sender", ["Sender@other-fleet", "ferry://other-fleet/Sender", "sender@PEER-ONE"])
def test_global_canonical_other_fleet_preserves_parser_case_and_delivery(sender):
    parsed = parse_address(sender)
    assert parsed and parsed[0] in {"other-fleet", "PEER-ONE"}
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=_config())) as client:
        response = _post(client, token=GLOBAL_SECRET, sender=sender)
    assert response.status_code == 200
    assert len(recorder.delivered) == 1


@pytest.mark.usefixtures("stub_sdk_transport")
@pytest.mark.parametrize("failure", ["real-header", "constructor", "value", "http-client", "urllib", "http-status"])
def test_outbound_header_failure_has_fixed_api_detail_and_audit_log(tmp_path, monkeypatch, caplog, capsys, failure):
    from pinky_daemon.api import create_api

    sentinel = "SYNTHETIC_OUTBOUND_HEADER_SECRET_0123456789"
    config = replace(_config(), peer_secrets={"peer-one": sentinel + "\r\nInjected: x"})
    sender = HttpMeshSender(config=config)
    app = create_api(db_path=str(tmp_path / "conversations.db"), default_working_dir=str(tmp_path))
    audit = []
    try:
        app.state.agents.register("sender", model="sonnet", working_dir=str(tmp_path / "sender"))
        app.state.agents.set_mesh_outbound_allowlist("sender", ["*@peer-one"])
        monkeypatch.setattr("pinky_daemon.ferry.outbound.select_mesh_sender", lambda: sender)
        monkeypatch.setattr("pinky_daemon.mesh_store.MeshStore.log_message", lambda self, **kw: audit.append(kw))

        def urlopen(request, timeout=None):
            header = request.get_header("Authorization")
            assert sentinel in header
            if failure == "real-header":
                connection = http.client.HTTPConnection("127.0.0.1")
                connection.putrequest("POST", "/")
                connection.putheader("Authorization", header)
                pytest.fail("the real header validator accepted the CR/LF fixture")
            if failure == "value":
                raise ValueError(header)
            if failure == "http-client":
                raise http.client.BadStatusLine(header)
            if failure == "http-status":
                raise urllib.error.HTTPError(request.full_url, 401, header, {}, None)
            raise urllib.error.URLError(header)

        if failure == "constructor":
            def broken_request(*args, **kwargs):
                raise ValueError(kwargs["headers"]["Authorization"])
            monkeypatch.setattr("urllib.request.Request", broken_request)
        else:
            monkeypatch.setattr("urllib.request.urlopen", urlopen)
        with closing(TestClient(app, raise_server_exceptions=False)) as client:
            response = client.post("/broker/send", json={
                "agent_name": "sender", "platform": "ferry", "chat_id": "receiver@peer-one",
                "content": "fixture message",
            })
        assert response.status_code == 502
        expected = {
            "real-header": "ferry POST failed (ValueError)",
            "constructor": "ferry POST failed (ValueError)",
            "value": "ferry POST failed (ValueError)",
            "http-client": "ferry POST failed (BadStatusLine)",
            "urllib": "ferry POST failed (URLError)",
            "http-status": "ferry POST failed: HTTP 401",
        }[failure]
        assert response.json()["detail"] == f"ferry send failed: {expected}"
        assert len(audit) == 1 and audit[0]["error"] == expected
        captured = capsys.readouterr()
        logs = "\n".join(record.getMessage() + repr(record.args) for record in caplog.records)
        assert sentinel not in response.text + json.dumps(audit) + logs + captured.out + captured.err
    finally:
        app.state.store_catalog.close()
