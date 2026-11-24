"""Credential digest comparison and private configuration loading."""

import hashlib
import json

import pytest

from pinky_relay.config import RelayConfig, parse_json


def test_auth_compares_every_candidate_even_after_a_match_and_for_empty(monkeypatch):
    config = RelayConfig.from_mapping({
        "peer-one": hashlib.sha256(b"first-token").hexdigest(),
        "peer-two": hashlib.sha256(b"second-token").hexdigest(),
        "empty": hashlib.sha256(b"").hexdigest(),
    })
    from pinky_relay.config import hmac

    compare = hmac.compare_digest
    calls = []

    def count(first, second):
        calls.append(True)
        return compare(first, second)

    monkeypatch.setattr(hmac, "compare_digest", count)
    for value, expected in (("first-token", "peer-one"), ("second-token", "peer-two"), ("wrong", None), ("", None), ("bad\udcff", None)):
        calls.clear()
        assert config.authenticate(value) == expected
        assert len(calls) == 3
    assert "first-token" not in repr(config)


@pytest.mark.parametrize("mapping", [
    {}, {"bad@fleet": "0" * 64}, {"a": "clear token"}, {"A": "0" * 64, "a": "1" * 64},
    {"a": "0" * 64, "b": "0" * 64}, {"a": False}, [],
])
def test_bad_credential_config_is_refused_with_fixed_error(mapping):
    refused = False
    try:
        RelayConfig.from_mapping(mapping)
    except ValueError:
        refused = True
    assert refused


def test_private_config_file_no_symlink_no_group_access(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"peer-one": hashlib.sha256(b"synthetic-token").hexdigest()}))
    path.chmod(0o600)
    assert RelayConfig.from_file(path).authenticate("synthetic-token") == "peer-one"
    link = tmp_path / "link.json"
    link.symlink_to(path)
    for candidate in (link,):
        with pytest.raises(ValueError, match="cannot be read safely"):
            RelayConfig.from_file(candidate)
    path.chmod(0o640)
    with pytest.raises(ValueError, match="mode 0600"):
        RelayConfig.from_file(path)


def test_private_config_requires_the_current_owner(tmp_path, monkeypatch):
    import os

    path = tmp_path / "config.json"
    path.write_text('{"a":"' + "0" * 64 + '"}')
    path.chmod(0o600)
    uid = os.geteuid()
    monkeypatch.setattr(os, "geteuid", lambda: uid + 1)
    with pytest.raises(ValueError, match="owner-owned"):
        RelayConfig.from_file(path)


@pytest.mark.parametrize("raw", ['{"x":1,"x":2}', '{"x":NaN}', '{"x":Infinity}', "[" * 21 + "0" + "]" * 21])
def test_strict_json_rejects_duplicates_constants_and_depth(raw):
    with pytest.raises(ValueError):
        parse_json(raw)
