"""Pinned identities and reservations fail closed without changing legacy routes."""

import hashlib
import json

import pytest

from pinky_daemon.ferry.config import FerryConfig
from pinky_daemon.ferry.outbound import (
    HttpMeshSender,
    MeshSender,
    SendResult,
    build_envelope,
    select_mesh_sender,
)
from pinky_daemon.ferry.relay_identity import generate_identity, load_identity

TOKEN = "synthetic-private-relay-token-0123456789"


def relay_environment(tmp_path, *, url="https://relay.example.invalid/ferry-relay/"):
    own_path, peer_path = tmp_path / "local.json", tmp_path / "peer.json"
    own = generate_identity(own_path, "local")
    peer = generate_identity(peer_path, "peer-one")
    env = {"PINKYBOT_FLEET_NAME": "local", "PINKYBOT_FERRY_RELAY_URL": url,
           "PINKYBOT_FERRY_RELAY_TOKEN": TOKEN, "PINKYBOT_FERRY_RELAY_KEY_FILE": str(own_path),
           "PINKYBOT_FERRY_RELAY_PEERS": json.dumps({"peer-one": peer.public()}),
           "PINKYBOT_FERRY_PEERS": json.dumps({"peer-one": "http://peer.example.invalid"})}
    return env, own, peer


def envelope(target="receiver@peer-one"):
    return build_envelope(from_="sender@local", to=target, body={"kind": "msg", "text": "fixture"})


@pytest.mark.parametrize("failure", ["pin", "URL", "TOKEN", "KEY_FILE", "mode", "symlink", "wrong-own"])
def test_each_unusable_reserved_route_never_falls_back(tmp_path, monkeypatch, failure):
    env, own, peer = relay_environment(tmp_path)
    if failure == "pin":
        data = peer.public()
        data["fingerprint"] = "0" * 32
        env["PINKYBOT_FERRY_RELAY_PEERS"] = json.dumps({"peer-one": data})
    elif failure in ("URL", "TOKEN", "KEY_FILE"):
        env.pop("PINKYBOT_FERRY_RELAY_" + failure)
    elif failure == "mode":
        (tmp_path / "local.json").chmod(0o640)
    elif failure == "symlink":
        (tmp_path / "linked.json").symlink_to(tmp_path / "local.json")
        env["PINKYBOT_FERRY_RELAY_KEY_FILE"] = str(tmp_path / "linked.json")
    else:
        env["PINKYBOT_FERRY_RELAY_KEY_FILE"] = str(tmp_path / "peer.json")
    calls = []

    def capture(kind):
        def send(self, env):
            calls.append(kind)
            return SendResult(True, None, kind, env.ts)
        return send

    monkeypatch.setattr(HttpMeshSender, "send", capture("http"))
    monkeypatch.setattr(MeshSender, "send", capture("nats"))
    config = FerryConfig.from_env(env)
    result = select_mesh_sender(config).send(envelope())
    assert calls == []
    assert not result.sent and result.error
    assert TOKEN not in result.error and env.get("PINKYBOT_FERRY_RELAY_KEY_FILE", "not-present") not in result.error


def test_relay_overlap_is_removed_loudly_without_credential_echo(tmp_path, caplog):
    env, own, peer = relay_environment(tmp_path)
    cfg = FerryConfig.from_env(env)
    assert cfg.peers == {}
    assert cfg.relay.ready and cfg.relay.peers["peer-one"].ok
    assert any("relay-reserved" in r.getMessage() for r in caplog.records)
    raw = json.dumps(cfg.diagnostics()) + repr(cfg) + repr(cfg.relay)
    assert TOKEN not in raw and env["PINKYBOT_FERRY_RELAY_KEY_FILE"] not in raw
    assert cfg.diagnostics()["relay"]["peers"]["peer-one"]["expected"] == peer.fingerprint.hex()


def test_case_alias_keys_are_refused_but_nonreserved_route_is_unchanged(tmp_path, monkeypatch):
    env, own, peer = relay_environment(tmp_path)
    env["PINKYBOT_FERRY_RELAY_PEERS"] = json.dumps({"peer-one": peer.public(), "PEER-ONE": peer.public()})
    cfg = FerryConfig.from_env(env)
    assert not cfg.relay.ready
    calls = []
    monkeypatch.setattr(MeshSender, "send", lambda self, item: calls.append(item.to) or SendResult(True, None, "nats", item.ts))
    assert select_mesh_sender(cfg).send(envelope("receiver@other-fleet")).sent
    assert calls == ["receiver@other-fleet"]
    assert not select_mesh_sender(cfg).send(envelope()).sent
    assert calls == ["receiver@other-fleet"]


def test_unicode_casefold_cannot_turn_an_invalid_fleet_key_into_a_valid_pin(tmp_path):
    env, own, peer = relay_environment(tmp_path)
    env["PINKYBOT_FERRY_RELAY_PEERS"] = json.dumps({"\u00dffleet": peer.public()})
    cfg = FerryConfig.from_env(env)
    assert not any(p.ok for p in cfg.relay.peers.values())
    assert cfg.relay.is_reserved("\u00dffleet")


@pytest.mark.parametrize("url", ["http://relay.example.invalid/", "https://user:password@relay.example.invalid/", "https://relay.example.invalid/?token=x", "https://relay.example.invalid/#x", "https://relay.example.invalid/\n"])
def test_unsafe_urls_block_the_reserved_route(tmp_path, url):
    env, own, peer = relay_environment(tmp_path, url=url)
    cfg = FerryConfig.from_env(env)
    assert not cfg.relay.ready
    assert not select_mesh_sender(cfg).send(envelope()).sent


def test_empty_relay_environment_retains_existing_diagnostics_and_transport():
    env = {"PINKYBOT_FLEET_NAME": "local"}
    first = FerryConfig.from_env(env)
    env.update({"PINKYBOT_FERRY_RELAY_" + name: "" for name in ("URL", "TOKEN", "KEY_FILE", "PEERS")})
    second = FerryConfig.from_env(env)
    assert first.relay is second.relay is None
    assert first.diagnostics() == second.diagnostics()
    assert repr(first) == repr(second)
    assert type(select_mesh_sender(first)) is type(select_mesh_sender(second)) is MeshSender


def test_keygen_is_private_exclusive_and_fingerprint_is_full_width(tmp_path):
    path = tmp_path / "identity.json"
    identity = generate_identity(path, "PEER-ONE")
    assert path.stat().st_mode & 0o777 == 0o600
    assert identity.fleet == "peer-one"
    public = identity.public()
    assert len(public["fingerprint"]) == 32
    before = hashlib.sha256(path.read_bytes()).hexdigest()
    with pytest.raises(ValueError):
        generate_identity(path, "peer-one")
    assert hashlib.sha256(path.read_bytes()).hexdigest() == before
    assert load_identity(path, own_fleet="peer-one").public() == public
    link = tmp_path / "alias.json"
    link.symlink_to(path)
    with pytest.raises(ValueError):
        generate_identity(link, "peer-one")
    with pytest.raises(ValueError):
        load_identity(link)


def test_key_cli_prints_one_public_json_line_and_never_overwrites(tmp_path, monkeypatch, capsys):
    from pinky_cli.__main__ import main

    path = tmp_path / "identity.json"
    monkeypatch.setattr("sys.argv", ["pinky", "ferry", "relay-keygen", "--fleet", "local", "--key-file", str(path)])
    main()
    output = capsys.readouterr()
    public = json.loads(output.out)
    assert len(output.out.splitlines()) == 1 and output.err == ""
    assert set(public) == {"fleet", "signing_pk", "encryption_pk", "fingerprint"}
    assert len(public["fingerprint"]) == 32 and str(path) not in output.out
    before = path.read_bytes()
    with pytest.raises(SystemExit):
        main()
    assert path.read_bytes() == before
    monkeypatch.setattr("sys.argv", ["pinky", "ferry", "relay-fingerprint", "--key-file", str(path)])
    main()
    assert json.loads(capsys.readouterr().out) == public
