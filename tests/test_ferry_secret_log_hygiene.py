"""Credential-map mistakes never disclose keys through operator output."""

import json

import pytest
from fastapi.testclient import TestClient

from pinky_daemon.ferry.config import FerryConfig
from pinky_daemon.ferry.inbound_server import build_ferry_app
from pinky_daemon.ferry.outbound import HttpMeshSender
from tests.test_ferry_peer_secrets import (
    GLOBAL_SECRET,
    PEER_SECRET,
    _DeliveryRecorder,
    _environment,
    _post,
)


def _configuration(entries):
    return FerryConfig.from_env(_environment(PINKYBOT_FERRY_PEER_SECRETS=json.dumps(entries)))


def _assert_no_disclosure(caplog, config, *sentinels):
    logs = "\n".join(record.getMessage() + repr(record.args) for record in caplog.records)
    diagnostics = json.dumps([config.diagnostics(), HttpMeshSender(config=config).diagnostics()])
    assert all(value not in logs for value in sentinels), "credential input reached a log record"
    assert all(value not in diagnostics for value in sentinels), (
        "credential input reached diagnostics"
    )


def test_swapped_peer_secret_uppercase_key_is_never_disclosed(caplog):
    sentinel = "SYNTHETIC_SECRET_KEY_01234567890123456789"
    config = _configuration({sentinel: "peer-one"})
    assert config.peer_secrets == {}
    assert len(caplog.records) == 1
    _assert_no_disclosure(caplog, config, sentinel, GLOBAL_SECRET, PEER_SECRET)


def test_swapped_peer_secret_invalid_key_is_never_disclosed(caplog):
    sentinel = "SYNTHETIC_SECRET_KEY_01234567890123456789/="
    config = _configuration({sentinel: "peer-one"})
    assert config.peer_secrets == {}
    assert len(caplog.records) == 1
    _assert_no_disclosure(caplog, config, sentinel, GLOBAL_SECRET, PEER_SECRET)


def test_swapped_peer_secret_slug_key_with_short_value_is_never_disclosed(caplog):
    sentinel = "synthetic-secret-key-01234567890123456789"
    config = _configuration({sentinel: "peer-one"})
    assert config.peer_secrets == {}
    assert len(caplog.records) == 1
    _assert_no_disclosure(caplog, config, sentinel, GLOBAL_SECRET, PEER_SECRET)


def test_swapped_peer_secret_slug_key_with_global_value_is_never_disclosed(caplog):
    sentinel = "synthetic-secret-key-01234567890123456789"
    config = _configuration({sentinel: GLOBAL_SECRET})
    assert config.peer_secrets == {}
    assert len(caplog.records) == 1
    _assert_no_disclosure(caplog, config, sentinel, GLOBAL_SECRET, PEER_SECRET)


def test_duplicate_peer_secret_keys_are_never_disclosed(caplog):
    sentinels = (
        "synthetic-first-secret-key-01234567890123456789",
        "synthetic-second-secret-key-01234567890123456789",
    )
    config = _configuration(dict.fromkeys(sentinels, PEER_SECRET))
    assert config.peer_secrets == {}
    assert len(caplog.records) == 2
    _assert_no_disclosure(caplog, config, *sentinels, GLOBAL_SECRET, PEER_SECRET)


def test_configured_peer_short_secret_names_the_configured_fleet(caplog):
    config = _configuration({"peer-one": "short"})
    assert config.peer_secrets == {}
    assert len(caplog.records) == 1
    assert 'fleet "peer-one"' in caplog.records[0].getMessage()
    _assert_no_disclosure(caplog, config, "short", GLOBAL_SECRET, PEER_SECRET)


def test_unconfigured_valid_peer_name_is_omitted_from_diagnostics(caplog):
    sentinel = "synthetic-secret-key-01234567890123456789"
    config = _configuration({sentinel: PEER_SECRET})
    assert config.peer_secrets == {sentinel: PEER_SECRET}
    _assert_no_disclosure(caplog, config, sentinel, GLOBAL_SECRET, PEER_SECRET)


@pytest.mark.parametrize("source", ["claimed", "credential", "global-claimed"])
def test_refusal_logs_only_independently_configured_fleet_names(caplog, source):
    sentinel = "synthetic-secret-key-01234567890123456789"
    config = _configuration({sentinel: PEER_SECRET}) if source != "claimed" else _configuration(
        {"peer-one": PEER_SECRET}
    )
    sender = "sender@peer-two" if source == "credential" else f"sender@{sentinel}"
    token = GLOBAL_SECRET if source == "global-claimed" else PEER_SECRET
    recorder = _DeliveryRecorder()
    with TestClient(build_ferry_app(host_pinky=recorder, config=config)) as client:
        response = _post(client, token=token, sender=sender)
    assert response.status_code == 403
    assert recorder.delivered == []
    assert len(caplog.records) == 1
    _assert_no_disclosure(caplog, config, sentinel, GLOBAL_SECRET, PEER_SECRET)
