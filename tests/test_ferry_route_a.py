"""Tests for ferry Route A — Tailscale-bridge cross-fleet transport (task #202).

Covers the pieces that turn the half-built ferry into a working barsik↔onesie
round-trip over Tailscale:

  - FerryConfig env parsing + fail-closed enable gating
  - envelope_from_wire ⇄ envelope_to_wire round-trip
  - fleet-aware inbound address parsing (canonical + at-form)
  - host-pinky kind dispatch: a plain mesh_remote_send (body.kind="msg")
    actually delivers (regression — used to be rejected as unknown kind)
  - HttpMeshSender outbound POST (bearer auth, peer URL map, error paths)
  - build_ferry_app listener: bearer + source-IP allowlist + ACL, fail-closed
  - end-to-end: wire JSON → ferry app → real HostPinky → broker
"""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from pinky_daemon.ferry.config import DEFAULT_FERRY_PORT, FerryConfig, fleet_name
from pinky_daemon.ferry.host_pinky import HostPinky, parse_pinkybot_address
from pinky_daemon.ferry.inbound_server import build_ferry_app, envelope_from_wire
from pinky_daemon.ferry.outbound import (
    HttpMeshSender,
    build_envelope,
    envelope_to_wire,
)
from pinky_daemon.ferry.types import AgentCardSelector, DeliveryResult

# -- Fakes --------------------------------------------------------------------


class FakeRegistry:
    def __init__(self, agents):
        self._agents = agents

    def has_agent(self, name):
        return name in self._agents

    def list_agents(self):
        return [{"name": n} for n in self._agents]

    def get_peer_fleet_acl(self, name):
        return list(self._agents.get(name, []))


class FakeBroker:
    def __init__(self):
        self.received = []

    async def dispatch_pre_authorized(self, agent_name, broker_msg):
        self.received.append((agent_name, broker_msg))


class _FakeResp:
    def __init__(self, code=200):
        self._code = code

    def getcode(self):
        return self._code

    def read(self):
        return b""

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# -- FerryConfig --------------------------------------------------------------


class TestFerryConfig:
    def test_fleet_name_default_and_override(self):
        assert fleet_name({}) == "pinkybot"
        assert fleet_name({"PINKYBOT_FLEET_NAME": "mini"}) == "mini"
        assert fleet_name({"PINKYBOT_FLEET_NAME": "  "}) == "pinkybot"

    def test_disabled_without_master_flag(self):
        cfg = FerryConfig.from_env(
            {
                "PINKYBOT_FERRY_SHARED_SECRET": "s",
                "PINKYBOT_FERRY_BIND_HOST": "100.1.1.1",
                # no PINKYBOT_FERRY_ENABLED
            }
        )
        assert cfg.enabled is False

    def test_disabled_without_secret_even_if_flag_on(self):
        cfg = FerryConfig.from_env(
            {"PINKYBOT_FERRY_ENABLED": "1", "PINKYBOT_FERRY_BIND_HOST": "100.1.1.1"}
        )
        assert cfg.enabled is False

    def test_disabled_without_bind_host(self):
        cfg = FerryConfig.from_env(
            {"PINKYBOT_FERRY_ENABLED": "1", "PINKYBOT_FERRY_SHARED_SECRET": "s"}
        )
        assert cfg.enabled is False

    def test_enabled_when_all_present(self):
        cfg = FerryConfig.from_env(
            {
                "PINKYBOT_FERRY_ENABLED": "true",
                "PINKYBOT_FERRY_SHARED_SECRET": "s",
                "PINKYBOT_FERRY_BIND_HOST": "100.112.44.64",  # tailnet IP (CGNAT)
                "PINKYBOT_FLEET_NAME": "tod",
            }
        )
        assert cfg.enabled is True
        assert cfg.fleet_name == "tod"
        assert cfg.bind_port == DEFAULT_FERRY_PORT

    def test_peers_json_and_kv_forms(self):
        j = FerryConfig.from_env(
            {"PINKYBOT_FERRY_PEERS": '{"tod": "http://100.112.44.64:8899"}'}
        )
        assert j.peers == {"tod": "http://100.112.44.64:8899"}
        kv = FerryConfig.from_env(
            {"PINKYBOT_FERRY_PEERS": "tod=http://t:8899,mini=http://m:8899"}
        )
        assert kv.peers == {"tod": "http://t:8899", "mini": "http://m:8899"}

    def test_peers_malformed_is_empty_not_crash(self):
        assert FerryConfig.from_env({"PINKYBOT_FERRY_PEERS": "{bad json"}).peers == {}
        assert FerryConfig.from_env({"PINKYBOT_FERRY_PEERS": "garbage"}).peers == {}

    def test_peer_url_appends_path(self):
        cfg = FerryConfig.from_env({"PINKYBOT_FERRY_PEERS": "tod=http://t:8899/"})
        assert cfg.peer_url("tod") == "http://t:8899/ferry/inbound"
        assert cfg.peer_url("nope") is None

    def test_allowed_peer_ips_parsed(self):
        cfg = FerryConfig.from_env(
            {"PINKYBOT_FERRY_ALLOWED_PEER_IPS": "100.1.1.1, 100.2.2.2 ,"}
        )
        assert cfg.allowed_peer_ips == frozenset({"100.1.1.1", "100.2.2.2"})

    def test_diagnostics_never_leaks_secret(self):
        cfg = FerryConfig.from_env({"PINKYBOT_FERRY_SHARED_SECRET": "topsecret"})
        diag = cfg.diagnostics()
        assert diag["shared_secret_set"] is True
        assert "topsecret" not in json.dumps(diag)

    @pytest.mark.parametrize("bad_host", ["0.0.0.0", "::", "8.8.8.8", "example.com", "*"])
    def test_unsafe_bind_host_fails_closed(self, bad_host):
        # Wildcard / public / non-literal bind host must NOT enable the listener
        # — defense-in-depth layer #4 (Tailscale-only) enforced at the gate.
        cfg = FerryConfig.from_env(
            {
                "PINKYBOT_FERRY_ENABLED": "1",
                "PINKYBOT_FERRY_SHARED_SECRET": "s",
                "PINKYBOT_FERRY_BIND_HOST": bad_host,
            }
        )
        assert cfg.enabled is False
        assert "wildcard/public" in cfg.why_disabled(
            {
                "PINKYBOT_FERRY_ENABLED": "1",
                "PINKYBOT_FERRY_SHARED_SECRET": "s",
                "PINKYBOT_FERRY_BIND_HOST": bad_host,
            }
        )

    @pytest.mark.parametrize("good_host", ["100.108.59.106", "100.112.44.64", "127.0.0.1", "192.168.1.5"])
    def test_safe_bind_host_enables(self, good_host):
        cfg = FerryConfig.from_env(
            {
                "PINKYBOT_FERRY_ENABLED": "1",
                "PINKYBOT_FERRY_SHARED_SECRET": "s",
                "PINKYBOT_FERRY_BIND_HOST": good_host,
            }
        )
        assert cfg.enabled is True

    def test_why_disabled_messages(self):
        assert "PINKYBOT_FERRY_ENABLED" in FerryConfig.from_env({}).why_disabled({})
        assert "SHARED_SECRET" in FerryConfig.from_env(
            {"PINKYBOT_FERRY_ENABLED": "1"}
        ).why_disabled({"PINKYBOT_FERRY_ENABLED": "1"})


# -- envelope_from_wire ⇄ envelope_to_wire ------------------------------------


class TestWireRoundTrip:
    def test_round_trip_preserves_core_fields(self):
        env = build_envelope(
            from_="ferry://mini/barsik",
            to="onesie@tod",
            body={"kind": "msg", "text": "ferry smoke 1"},
            correlation_id="cid-1",
            priority="high",
        )
        wire = json.loads(envelope_to_wire(env))
        rebuilt = envelope_from_wire(wire)
        assert rebuilt.from_ == "ferry://mini/barsik"
        assert rebuilt.to == "onesie@tod"
        assert rebuilt.body == {"kind": "msg", "text": "ferry smoke 1"}
        assert rebuilt.correlation_id == "cid-1"
        assert rebuilt.priority == "high"
        assert rebuilt.id == env.id
        assert rebuilt.ts == env.ts

    def test_rejects_missing_body(self):
        with pytest.raises(ValueError):
            envelope_from_wire({"v": "0.1", "from": "a@x", "to": "b@y"})

    def test_rejects_body_without_kind(self):
        with pytest.raises(ValueError):
            envelope_from_wire({"body": {"text": "no kind"}})

    def test_rejects_non_dict(self):
        with pytest.raises(ValueError):
            envelope_from_wire("not a dict")  # type: ignore[arg-type]


# -- fleet-aware inbound address parsing --------------------------------------


class TestParseLocalAddress:
    def test_default_fleet_canonical(self):
        assert parse_pinkybot_address("ferry://pinkybot/barsik") == "barsik"

    def test_default_fleet_rejects_other(self):
        assert parse_pinkybot_address("ferry://sigil/pulse") is None
        assert parse_pinkybot_address("misha@pinky.local") is None

    def test_custom_fleet_canonical_and_atform(self):
        assert parse_pinkybot_address("ferry://tod/onesie", "tod") == "onesie"
        assert parse_pinkybot_address("onesie@tod", "tod") == "onesie"

    def test_custom_fleet_rejects_mismatch(self):
        assert parse_pinkybot_address("onesie@tod", "mini") is None
        assert parse_pinkybot_address("barsik@mini", "tod") is None


# -- host-pinky kind dispatch (the round-trip bug fix) ------------------------


@pytest.mark.asyncio
class TestKindDispatch:
    async def _deliver(self, kind, fleet="mini", to="barsik@mini"):
        registry = FakeRegistry(
            {"barsik": [AgentCardSelector(fleet="tod", agent_id="onesie@tod")]}
        )
        broker = FakeBroker()
        host = HostPinky(registry=registry, broker=broker, fleet_name=fleet)
        env = build_envelope(from_="onesie@tod", to=to, body={"kind": kind, "text": "hi"})
        return await host.deliver(env), broker

    async def test_msg_kind_delivers(self):
        # Regression: mesh_remote_send defaults body.kind="msg"; this used to be
        # rejected as unsupported_payload_kind, so "sent:true" never delivered.
        result, broker = await self._deliver("msg")
        assert result.status == "delivered"
        assert len(broker.received) == 1

    async def test_message_kind_still_delivers(self):
        result, broker = await self._deliver("message")
        assert result.status == "delivered"

    @pytest.mark.parametrize("kind", ["smoke", "ack", "chat", "anything"])
    async def test_non_substrate_kinds_deliver(self, kind):
        result, _ = await self._deliver(kind)
        assert result.status == "delivered"

    async def test_unknown_substrate_kind_rejected(self):
        result, broker = await self._deliver("substrate.bogus")
        assert result.status == "rejected"
        assert result.reason == "unsupported_payload_kind"
        assert broker.received == []


# -- HttpMeshSender -----------------------------------------------------------


def _http_cfg(**over):
    base = {
        "PINKYBOT_FERRY_SHARED_SECRET": "sekret",
        "PINKYBOT_FERRY_PEERS": "tod=http://100.112.44.64:8899",
    }
    base.update(over)
    return FerryConfig.from_env(base)


class TestHttpMeshSender:
    def test_configured_requires_secret_and_peers(self):
        assert HttpMeshSender(config=_http_cfg()).configured is True
        assert HttpMeshSender(
            config=FerryConfig.from_env({"PINKYBOT_FERRY_PEERS": "tod=http://x:1"})
        ).configured is False
        assert HttpMeshSender(
            config=FerryConfig.from_env({"PINKYBOT_FERRY_SHARED_SECRET": "s"})
        ).configured is False

    def test_diagnostics_no_secret_leak(self):
        diag = HttpMeshSender(config=_http_cfg()).diagnostics()
        assert diag["transport"] == "http"
        assert "sekret" not in json.dumps(diag)

    def test_unparseable_target(self):
        env = build_envelope(from_="ferry://mini/barsik", to="onesie@tod", body={"kind": "msg"})
        object.__setattr__(env, "to", "::::")  # force unparseable
        res = HttpMeshSender(config=_http_cfg()).send(env)
        assert res.sent is False
        assert "unparseable" in res.error

    def test_no_peer_url_for_fleet(self):
        env = build_envelope(from_="ferry://mini/barsik", to="ghost@unknownfleet", body={"kind": "msg"})
        res = HttpMeshSender(config=_http_cfg()).send(env)
        assert res.sent is False
        assert "no ferry peer URL" in res.error

    def test_successful_post_sends_bearer_and_url(self, monkeypatch):
        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["req"] = req
            return _FakeResp(200)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        env = build_envelope(
            from_="ferry://mini/barsik", to="onesie@tod", body={"kind": "msg", "text": "hi"}
        )
        res = HttpMeshSender(config=_http_cfg()).send(env)
        assert res.sent is True
        req = captured["req"]
        assert req.full_url == "http://100.112.44.64:8899/ferry/inbound"
        assert req.get_header("Authorization") == "Bearer sekret"
        assert json.loads(req.data.decode())["to"] == "onesie@tod"

    def test_http_error_surfaced(self, monkeypatch):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 401, "Unauthorized", {}, None)

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        env = build_envelope(from_="ferry://mini/barsik", to="onesie@tod", body={"kind": "msg"})
        res = HttpMeshSender(config=_http_cfg()).send(env)
        assert res.sent is False
        assert "401" in res.error

    def test_network_error_surfaced(self, monkeypatch):
        import urllib.error

        def fake_urlopen(req, timeout=None):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr("urllib.request.urlopen", fake_urlopen)
        env = build_envelope(from_="ferry://mini/barsik", to="onesie@tod", body={"kind": "msg"})
        res = HttpMeshSender(config=_http_cfg()).send(env)
        assert res.sent is False
        assert "failed" in res.error


# -- build_ferry_app listener -------------------------------------------------


class _FakeHostPinky:
    def __init__(self, result):
        self._result = result
        self.delivered = []

    async def deliver(self, envelope):
        self.delivered.append(envelope)
        return self._result


def _client(host_pinky, **cfg_over):
    from fastapi.testclient import TestClient

    base = {
        "PINKYBOT_FERRY_ENABLED": "1",
        "PINKYBOT_FERRY_SHARED_SECRET": "sekret",
        "PINKYBOT_FERRY_BIND_HOST": "100.1.1.1",
        "PINKYBOT_FLEET_NAME": "mini",
    }
    base.update(cfg_over)
    cfg = FerryConfig.from_env(base)
    return TestClient(build_ferry_app(host_pinky=host_pinky, config=cfg)), cfg


def _wire(to="barsik@mini", from_="onesie@tod", kind="msg"):
    env = build_envelope(from_=from_, to=to, body={"kind": kind, "text": "hi"})
    return json.loads(envelope_to_wire(env))


class TestFerryApp:
    def test_health_ok(self):
        client, _ = _client(_FakeHostPinky(DeliveryResult(status="delivered")))
        r = client.get("/ferry/health")
        assert r.status_code == 200
        assert r.json()["fleet"] == "mini"

    def test_missing_bearer_401(self):
        client, _ = _client(_FakeHostPinky(DeliveryResult(status="delivered")))
        r = client.post("/ferry/inbound", json=_wire())
        assert r.status_code == 401

    def test_wrong_bearer_401(self):
        client, _ = _client(_FakeHostPinky(DeliveryResult(status="delivered")))
        r = client.post(
            "/ferry/inbound", json=_wire(), headers={"Authorization": "Bearer nope"}
        )
        assert r.status_code == 401

    def test_correct_bearer_delivers_200(self):
        hp = _FakeHostPinky(DeliveryResult(status="delivered"))
        client, _ = _client(hp)
        r = client.post(
            "/ferry/inbound", json=_wire(), headers={"Authorization": "Bearer sekret"}
        )
        assert r.status_code == 200
        assert r.json()["status"] == "delivered"
        assert len(hp.delivered) == 1

    def test_rejected_maps_403(self):
        hp = _FakeHostPinky(DeliveryResult(status="rejected", reason="acl_denied"))
        client, _ = _client(hp)
        r = client.post(
            "/ferry/inbound", json=_wire(), headers={"Authorization": "Bearer sekret"}
        )
        assert r.status_code == 403
        assert r.json()["reason"] == "acl_denied"

    def test_transient_maps_503(self):
        hp = _FakeHostPinky(DeliveryResult(status="transient_failure", reason="broker_error"))
        client, _ = _client(hp)
        r = client.post(
            "/ferry/inbound", json=_wire(), headers={"Authorization": "Bearer sekret"}
        )
        assert r.status_code == 503

    def test_bad_json_400(self):
        client, _ = _client(_FakeHostPinky(DeliveryResult(status="delivered")))
        r = client.post(
            "/ferry/inbound",
            content="{not json",
            headers={"Authorization": "Bearer sekret", "Content-Type": "application/json"},
        )
        assert r.status_code == 400

    def test_bad_envelope_400(self):
        client, _ = _client(_FakeHostPinky(DeliveryResult(status="delivered")))
        r = client.post(
            "/ferry/inbound",
            json={"v": "0.1", "from": "x@y", "to": "barsik@mini"},  # no body
            headers={"Authorization": "Bearer sekret"},
        )
        assert r.status_code == 400

    def test_source_ip_allowlist_blocks_non_peer(self):
        # TestClient's client host is "testclient"; allow only a different IP.
        hp = _FakeHostPinky(DeliveryResult(status="delivered"))
        client, _ = _client(hp, PINKYBOT_FERRY_ALLOWED_PEER_IPS="100.9.9.9")
        r = client.post(
            "/ferry/inbound", json=_wire(), headers={"Authorization": "Bearer sekret"}
        )
        assert r.status_code == 401
        assert hp.delivered == []

    def test_source_ip_allowlist_permits_listed(self):
        hp = _FakeHostPinky(DeliveryResult(status="delivered"))
        client, _ = _client(hp, PINKYBOT_FERRY_ALLOWED_PEER_IPS="testclient")
        r = client.post(
            "/ferry/inbound", json=_wire(), headers={"Authorization": "Bearer sekret"}
        )
        assert r.status_code == 200


# -- End-to-end: wire JSON → ferry app → real HostPinky → broker --------------


@pytest.mark.asyncio
class TestEndToEndInbound:
    async def test_wire_to_broker_via_real_host_pinky(self):
        from pinky_daemon.agent_registry import AgentRegistry

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            registry = AgentRegistry(db_path=path)
            registry.register("barsik", model="opus")
            # ACL configured the documented way — at-form agent_id.
            registry.add_peer_fleet_acl("barsik", fleet="tod", agent_id="onesie@tod")
            broker = FakeBroker()
            host = HostPinky(registry=registry, broker=broker, fleet_name="mini")

            # The EXACT wire form the mesh_send endpoint emits: canonical from_
            # (ferry://<fleet>/<agent>), NOT the at-form. The canonical form must
            # match the at-form ACL selector (review finding #1 regression).
            env = build_envelope(
                from_="ferry://tod/onesie",
                to="barsik@mini",
                body={"kind": "msg", "text": "ferry smoke"},
            )
            rebuilt = envelope_from_wire(json.loads(envelope_to_wire(env)))
            result = await host.deliver(rebuilt)

            assert result.status == "delivered"
            assert len(broker.received) == 1
            agent_name, bm = broker.received[0]
            assert agent_name == "barsik"
            assert bm.content == "ferry smoke"
            assert bm.platform == "ferry"
        finally:
            registry.close()
            os.unlink(path)

    async def test_acl_denied_blocks_unlisted_peer(self):
        from pinky_daemon.agent_registry import AgentRegistry

        fd, path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        try:
            registry = AgentRegistry(db_path=path)
            registry.register("barsik", model="opus")
            # No ACL entry → default-deny
            broker = FakeBroker()
            host = HostPinky(registry=registry, broker=broker, fleet_name="mini")
            env = build_envelope(
                from_="onesie@tod", to="barsik@mini", body={"kind": "msg", "text": "x"}
            )
            result = await host.deliver(envelope_from_wire(json.loads(envelope_to_wire(env))))
            assert result.status == "rejected"
            assert result.reason == "acl_denied"
            assert broker.received == []
        finally:
            registry.close()
            os.unlink(path)


# -- ACL address-form matching matrix (review finding #1) ---------------------


@pytest.mark.asyncio
class TestAclAddressFormMatching:
    """The wire ``from_`` (canonical) and the stored selector ``agent_id``
    (at-form) must match regardless of which address form each uses.
    """

    async def _deliver(self, *, selector_kwargs, from_):
        registry = FakeRegistry(
            {"barsik": [AgentCardSelector(**selector_kwargs)]}
        )
        broker = FakeBroker()
        host = HostPinky(registry=registry, broker=broker, fleet_name="mini")
        env = build_envelope(from_=from_, to="barsik@mini", body={"kind": "msg", "text": "hi"})
        return (await host.deliver(env)).status

    async def test_canonical_from_matches_atform_selector(self):
        # mesh_send emits canonical; operators store at-form. Must match.
        status = await self._deliver(
            selector_kwargs={"fleet": "tod", "agent_id": "onesie@tod"},
            from_="ferry://tod/onesie",
        )
        assert status == "delivered"

    async def test_atform_from_matches_ferryform_selector(self):
        status = await self._deliver(
            selector_kwargs={"fleet": "tod", "agent_id": "ferry://tod/onesie"},
            from_="onesie@tod",
        )
        assert status == "delivered"

    async def test_fleet_only_selector_matches_canonical(self):
        status = await self._deliver(
            selector_kwargs={"fleet": "tod", "agent_id": "*"},
            from_="ferry://tod/onesie",
        )
        assert status == "delivered"

    async def test_wrong_agent_still_denied(self):
        status = await self._deliver(
            selector_kwargs={"fleet": "tod", "agent_id": "someone-else@tod"},
            from_="ferry://tod/onesie",
        )
        assert status == "rejected"

    async def test_wrong_fleet_still_denied(self):
        status = await self._deliver(
            selector_kwargs={"fleet": "sigil", "agent_id": "onesie@sigil"},
            from_="ferry://tod/onesie",
        )
        assert status == "rejected"
