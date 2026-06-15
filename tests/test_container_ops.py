"""Tests for one-click containerize (#204).

Two layers:
  * Unit — :class:`ContainerLifecycle` orchestration driven with fakes, but
    against the REAL :class:`ContainerProvisioner` over a recording
    ``ContainerOps`` double (no podman). These pin Murzik's review invariants:
    no DB mutation before provision+probe succeed, a failed provision leaves the
    prior config intact, and decontainerize keeps the home volume.
  * API — the endpoints over a TestClient: preflight honesty, the
    409-unless-force active-work guard, operator-only authz, and the
    runtime-off 501.
"""

from __future__ import annotations

import asyncio
import dataclasses
import os
import tempfile

import pytest

from pinky_daemon import container_ops as cops
from pinky_daemon.agent_registry import Agent
from pinky_daemon.provisioning import ContainerNames, ContainerProvisioner, ProvisionError


# ── recording ContainerOps double (no real podman) ───────────────────────────
class RecordingContainerOps:
    """Records every podman command + tracks image/volume/secret/container state.

    ``fail_predicate(argv) -> bool`` injects a command failure (e.g. a failed
    pull, or a failed ``exec`` to simulate a missing-binary probe)."""

    def __init__(
        self, *, images=None, volumes=None, secrets=None, containers=None, fail_predicate=None
    ):
        self.commands: list[list[str]] = []
        self._images = set(images or [])
        self._volumes = set(volumes or [])
        self._secrets = set(secrets or [])
        self._containers = set(containers or [])
        self._fail = fail_predicate

    def run(self, argv):
        self.commands.append(list(argv))
        if self._fail and self._fail(list(argv)):
            raise ProvisionError(f"injected failure: {' '.join(argv)}")
        if len(argv) >= 4 and argv[1] == "volume" and argv[2] == "create":
            self._volumes.add(argv[3])
        elif len(argv) >= 3 and argv[1] == "pull":
            self._images.add(argv[2])
        elif len(argv) >= 4 and argv[1] == "create" and argv[2] == "--name":
            self._containers.add(argv[3])
        elif len(argv) >= 4 and argv[1] == "rm" and argv[2] == "-f":
            self._containers.discard(argv[3])
        elif len(argv) >= 4 and argv[1] == "secret" and argv[2] == "rm":
            self._secrets.discard(argv[3])
        elif len(argv) >= 4 and argv[1] == "volume" and argv[2] == "rm":
            self._volumes.discard(argv[3])

    def image_exists(self, ref):
        return ref in self._images

    def volume_exists(self, name):
        return name in self._volumes

    def secret_exists(self, name):
        return name in self._secrets

    def container_exists(self, name):
        return name in self._containers

    def write_secret(self, name, content):
        self._secrets.add(name)


# ── fake registry + session deps ─────────────────────────────────────────────
class FakeRegistry:
    def __init__(self, agent: Agent):
        self._agent = agent
        self.register_calls: list[dict] = []

    def get(self, name):
        return self._agent if (self._agent and self._agent.name == name) else None

    def register(self, name, **kwargs):
        self.register_calls.append(dict(kwargs))
        self._agent = dataclasses.replace(self._agent, **kwargs)
        return self._agent

    def get_or_create_signing_key(self, name):
        return f"key-{name}"


async def _sync_runner(fn, *a, **k):
    """Run blocking provisioner work inline (no thread) for deterministic tests."""
    return fn(*a, **k)


def _make_deps(agent, ops, *, has_live=True, provisioner=True):
    reg = FakeRegistry(agent)
    rec = {"disconnect": [], "start": [], "mcp": []}
    locks: dict[str, asyncio.Lock] = {}

    prov = ContainerProvisioner(
        ops=ops,
        image_provider=lambda a: a.container_image or "myco/agent:1",
        signing_key_provider=lambda n: f"key-{n}",
    )

    async def _disconnect(name):
        rec["disconnect"].append(name)
        return 1

    async def _start(name):
        rec["start"].append(name)

    deps = cops.ContainerLifecycleDeps(
        registry=reg,
        provisioner_for=(lambda: prov) if provisioner else (lambda: None),
        write_mcp_json=lambda n: rec["mcp"].append(n),
        disconnect_sessions=_disconnect,
        start_session=_start,
        has_live_session=lambda n: has_live,
        lifecycle_lock=lambda n: locks.setdefault(n, asyncio.Lock()),
        op_registry=cops.ContainerOpRegistry(),
        runner=_sync_runner,
    )
    return deps, reg, rec


def _agent(**kw):
    base = dict(name="tenant", model="opus", working_dir="/tmp/tenant", isolation_mode="local")
    base.update(kw)
    return Agent(**base)


# ── unit: image validation ───────────────────────────────────────────────────
class TestValidateImage:
    def test_ok(self):
        assert cops.validate_container_image("  registry/img:tag ") == "registry/img:tag"

    @pytest.mark.parametrize("bad", ["", "   ", "img with space", "img\ttab", "img\x00ctl"])
    def test_rejected(self, bad):
        with pytest.raises(ValueError):
            cops.validate_container_image(bad)

    def test_too_long(self):
        with pytest.raises(ValueError):
            cops.validate_container_image("x" * 600)


# ── unit: op registry ────────────────────────────────────────────────────────
class TestOpRegistry:
    def test_in_flight_then_terminal(self):
        r = cops.ContainerOpRegistry()
        r.begin("a", cops.OP_CONTAINERIZE, image="i")
        assert r.in_flight("a") is True
        r.update("a", cops.READY, "done")
        assert r.in_flight("a") is False
        assert r.get("a").status == cops.READY

    def test_unknown_agent_not_in_flight(self):
        assert cops.ContainerOpRegistry().in_flight("nope") is False


# ── unit: containerize / decontainerize orchestration ────────────────────────
class TestContainerizeOrchestration:
    def test_happy_path_provisions_probes_then_persists(self):
        ops = RecordingContainerOps()
        agent = _agent()
        deps, reg, rec = _make_deps(agent, ops, has_live=True)
        lc = cops.ContainerLifecycle(deps)
        deps.op_registry.begin("tenant", cops.OP_CONTAINERIZE, image="myco/agent:1")

        asyncio.run(lc.containerize("tenant", image="myco/agent:1", start=True))

        # Persisted exactly once, with the full coupled config.
        assert len(reg.register_calls) == 1
        kw = reg.register_calls[0]
        assert kw["isolation_mode"] == "container"
        assert kw["container_image"] == "myco/agent:1"
        assert kw["isolated"] is True
        assert kw["transport"] == "tmux"
        # MCP rewritten + session bounced (was live).
        assert rec["mcp"] == ["tenant"]
        assert rec["disconnect"] == ["tenant"]
        assert rec["start"] == ["tenant"]
        # Podman tenant exists.
        n = ContainerNames.for_agent("tenant")
        assert ops.volume_exists(n.volume)
        assert ops.secret_exists(n.secret)
        assert ops.container_exists(n.container)
        # Terminal record.
        r = deps.op_registry.get("tenant")
        assert r.status == cops.READY and r.provisioned is True and r.runtime_ready is True

    def test_no_persist_until_after_probe(self):
        """INVARIANT: a failed runnability probe must NOT mutate the DB and must
        leave the agent local (a bad image can never strand it)."""
        # exec (the verify probe) fails → image missing required tooling.
        ops = RecordingContainerOps(fail_predicate=lambda a: len(a) > 1 and a[1] == "exec")
        agent = _agent()
        deps, reg, rec = _make_deps(agent, ops, has_live=True)
        lc = cops.ContainerLifecycle(deps)
        deps.op_registry.begin("tenant", cops.OP_CONTAINERIZE, image="myco/agent:1")

        asyncio.run(lc.containerize("tenant", image="myco/agent:1", start=True))

        assert reg.register_calls == []  # never persisted
        assert reg.get("tenant").isolation_mode == "local"  # config intact
        assert rec["mcp"] == [] and rec["disconnect"] == [] and rec["start"] == []
        r = deps.op_registry.get("tenant")
        assert r.status == cops.ERROR and "not runnable" in r.message
        # Cleanup kept the home volume but removed the container we created.
        n = ContainerNames.for_agent("tenant")
        assert ops.volume_exists(n.volume) is True
        assert ops.container_exists(n.container) is False

    def test_failed_provision_leaves_config_intact(self):
        """INVARIANT: a failed provision (image pull failure) leaves the prior
        DB config untouched."""
        ops = RecordingContainerOps(fail_predicate=lambda a: len(a) > 1 and a[1] == "pull")
        agent = _agent()
        deps, reg, rec = _make_deps(agent, ops, has_live=True)
        lc = cops.ContainerLifecycle(deps)
        deps.op_registry.begin("tenant", cops.OP_CONTAINERIZE, image="bad/img:1")

        asyncio.run(lc.containerize("tenant", image="bad/img:1", start=True))

        assert reg.register_calls == []
        assert reg.get("tenant").isolation_mode == "local"
        assert deps.op_registry.get("tenant").status == cops.ERROR

    def test_runtime_off_records_error_no_persist(self):
        ops = RecordingContainerOps()
        agent = _agent()
        deps, reg, _ = _make_deps(agent, ops, provisioner=False)
        lc = cops.ContainerLifecycle(deps)
        deps.op_registry.begin("tenant", cops.OP_CONTAINERIZE, image="i")

        asyncio.run(lc.containerize("tenant", image="i", start=True))

        assert reg.register_calls == []
        r = deps.op_registry.get("tenant")
        assert r.status == cops.ERROR and "not enabled" in r.message

    def test_no_restart_when_not_live(self):
        """start=True only restarts a session that was ALREADY live."""
        ops = RecordingContainerOps()
        agent = _agent()
        deps, reg, rec = _make_deps(agent, ops, has_live=False)
        lc = cops.ContainerLifecycle(deps)
        deps.op_registry.begin("tenant", cops.OP_CONTAINERIZE, image="myco/agent:1")

        asyncio.run(lc.containerize("tenant", image="myco/agent:1", start=True))

        assert len(reg.register_calls) == 1  # still persisted
        assert rec["disconnect"] == ["tenant"]  # still stop any stragglers
        assert rec["start"] == []  # but nothing to restart
        assert deps.op_registry.get("tenant").status == cops.READY


class TestDecontainerizeOrchestration:
    def test_keeps_home_volume(self):
        """INVARIANT: decontainerize tears down the container + secret but KEEPS
        the home volume (its CLI logins survive a future re-containerize)."""
        n = ContainerNames.for_agent("tenant")
        ops = RecordingContainerOps(
            volumes={n.volume}, secrets={n.secret}, containers={n.container}
        )
        agent = _agent(isolation_mode="container", container_image="myco/agent:1", isolated=True)
        deps, reg, rec = _make_deps(agent, ops, has_live=True)
        lc = cops.ContainerLifecycle(deps)
        deps.op_registry.begin("tenant", cops.OP_DECONTAINERIZE, image="myco/agent:1")

        asyncio.run(lc.decontainerize("tenant", start=True))

        # Flipped to local, isolation lifted.
        assert len(reg.register_calls) == 1
        kw = reg.register_calls[0]
        assert kw["isolation_mode"] == "local" and kw["isolated"] is False
        # Home volume KEPT; container + secret removed.
        assert ops.volume_exists(n.volume) is True
        assert ops.container_exists(n.container) is False
        assert ops.secret_exists(n.secret) is False
        # Bounced + MCP rewritten.
        assert rec["mcp"] == ["tenant"] and rec["disconnect"] == ["tenant"]
        assert rec["start"] == ["tenant"]
        assert deps.op_registry.get("tenant").status == cops.READY

    def test_unknown_agent_errors(self):
        ops = RecordingContainerOps()
        deps, _reg, _ = _make_deps(_agent(), ops)
        lc = cops.ContainerLifecycle(deps)
        deps.op_registry.begin("ghost", cops.OP_DECONTAINERIZE)
        asyncio.run(lc.decontainerize("ghost", start=True))
        assert deps.op_registry.get("ghost").status == cops.ERROR


# ── API-level: endpoints over a TestClient ───────────────────────────────────
def _client():
    from fastapi.testclient import TestClient

    from pinky_daemon.api import create_api

    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    app = create_api(max_sessions=10, default_working_dir="/tmp", db_path=path)
    return TestClient(app)


def _register(client, name="tenant", working_dir="/tmp/tenant"):
    return client.post(
        "/agents", json={"name": name, "model": "opus", "working_dir": working_dir}
    )


class TestContainerizeEndpoints:
    def test_preflight_runtime_off(self, monkeypatch):
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        monkeypatch.delenv("PINKY_CONTAINER_DEFAULT_IMAGE", raising=False)
        client = _client()
        resp = client.get("/system/container-runtime")
        assert resp.status_code == 200
        data = resp.json()
        assert data["enabled"] is False
        assert data["ready"] is False
        assert data["binary"] is None
        assert data["default_image"] is None

    def test_preflight_runtime_on(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        monkeypatch.setenv("PINKY_CONTAINER_DEFAULT_IMAGE", "myco/agent:1")
        client = _client()
        data = client.get("/system/container-runtime").json()
        assert data["enabled"] is True
        assert data["binary"] == "podman"
        assert data["ready"] is True
        assert data["default_image"] == "myco/agent:1"

    def test_preflight_docker_not_ready(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "docker")
        client = _client()
        data = client.get("/system/container-runtime").json()
        assert data["enabled"] is True
        assert data["binary"] == "docker"
        assert data["ready"] is False  # docker unsupported today

    def test_containerize_501_when_runtime_off(self, monkeypatch):
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        client = _client()
        _register(client)
        resp = client.post("/agents/tenant/containerize", json={"image": "myco/agent:1"})
        assert resp.status_code == 501

    def test_containerize_404_unknown_agent(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        client = _client()
        resp = client.post("/agents/ghost/containerize", json={"image": "myco/agent:1"})
        assert resp.status_code == 404

    def test_operator_only_rejects_internal_caller(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        client = _client()
        _register(client)
        resp = client.post(
            "/agents/tenant/containerize",
            json={"image": "myco/agent:1"},
            headers={"x-pinky-agent": "sometenant"},
        )
        assert resp.status_code == 403

    def test_busy_409_unless_force(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")

        # Keep the background task off real podman: the provisioner gate returns
        # nothing, so a forced op records an error instead of shelling out.
        def _no_provisioner(*_a, **_k):
            raise NotImplementedError("test: no runtime")

        monkeypatch.setattr("pinky_daemon.provisioning.get_provisioner", _no_provisioner)

        client = _client()
        _register(client)
        # Mark the agent actively working (the hook surface the guard reads).
        client.post(
            "/agents/tenant/status",
            json={"status": "working", "tool_name": "Bash", "detail": "x"},
        )
        busy = client.post("/agents/tenant/containerize", json={"image": "myco/agent:1"})
        assert busy.status_code == 409

        forced = client.post(
            "/agents/tenant/containerize", json={"image": "myco/agent:1", "force": True}
        )
        assert forced.status_code == 202
        body = forced.json()
        assert body["operation"] == "containerize"
        assert body["status_url"] == "/agents/tenant/container-status"

    def test_invalid_image_400(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        client = _client()
        _register(client)
        resp = client.post(
            "/agents/tenant/containerize", json={"image": "bad image with spaces"}
        )
        assert resp.status_code == 400

    def test_no_image_no_default_400(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        monkeypatch.delenv("PINKY_CONTAINER_DEFAULT_IMAGE", raising=False)
        client = _client()
        _register(client)
        resp = client.post("/agents/tenant/containerize", json={})
        assert resp.status_code == 400

    def test_container_status_local_agent(self, monkeypatch):
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        client = _client()
        _register(client)
        data = client.get("/agents/tenant/container-status").json()
        assert data["isolation_mode"] == "local"
        assert data["status"] == "idle"
        assert data["provisioned"] is False
        assert data["runtime_ready"] is False
