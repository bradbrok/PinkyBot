"""Container-isolation wiring in TmuxSession (gated runner injection + cold-start
ensure_started). All gated by PINKY_CONTAINER_RUNTIME and isolation_mode; local
agents and the gate-off default must behave exactly as before. No real podman —
the provisioner is faked, runner selection is asserted via wrap() shapes."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

import pinky_daemon.provisioning as provisioning
from pinky_daemon.command_runner import ContainerCommandRunner, LocalCommandRunner
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import TmuxSession


class _FakeAgent:
    def __init__(self, name, isolation_mode="local"):
        self.name = name
        self.isolation_mode = isolation_mode


class _FakeRegistry:
    def __init__(self, agent=None, *, raises=False):
        self._agent = agent
        self._raises = raises

    def get(self, name):
        if self._raises:
            raise RuntimeError("registry boom")
        return self._agent

    def get_or_create_signing_key(self, name):
        return f"key-{name}"


def _session(agent_name="dymok", registry=None):
    cfg = StreamingSessionConfig(agent_name=agent_name, working_dir="/tmp/x")
    # A truthy tmux_control short-circuits the in-__init__ runner selection, so we
    # can set _registry and call the gated methods directly + deterministically.
    ss = TmuxSession(cfg, tmux_control=MagicMock())
    ss._registry = registry
    return ss


class TestSelectCommandRunner:
    def test_local_by_default_gate_off(self, monkeypatch):
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "container")))
        assert isinstance(ss._select_command_runner(), LocalCommandRunner)

    def test_container_runner_when_gated(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "container")))
        runner = ss._select_command_runner()
        assert isinstance(runner, ContainerCommandRunner)
        assert runner.wrap(["tmux", "ls"]) == ["podman", "exec", "--", "pinky-dymok", "tmux", "ls"]

    def test_docker_binary_honored(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "docker")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "container")))
        assert ss._select_command_runner().wrap(["tmux"])[0] == "docker"

    def test_local_for_non_container_even_when_gated(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "local")))
        assert isinstance(ss._select_command_runner(), LocalCommandRunner)

    def test_failsafe_when_registry_missing(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=None)
        assert isinstance(ss._select_command_runner(), LocalCommandRunner)

    def test_failsafe_when_registry_raises(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(raises=True))
        assert isinstance(ss._select_command_runner(), LocalCommandRunner)


@pytest.mark.asyncio
class TestEnsureContainerStarted:
    async def test_noop_for_local_agent(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "local")))
        called = []
        monkeypatch.setattr(
            provisioning, "get_provisioner",
            lambda *a, **k: called.append(1) or MagicMock(),
        )
        await ss._ensure_container_started()
        assert called == []  # provisioner never consulted for a local agent

    async def test_noop_when_gate_off(self, monkeypatch):
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "container")))
        called = []
        monkeypatch.setattr(
            provisioning, "get_provisioner",
            lambda *a, **k: called.append(1) or MagicMock(),
        )
        await ss._ensure_container_started()
        assert called == []  # gate off → never provisions, even for a container agent

    async def test_calls_ensure_started_when_gated_container(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "container")))
        seen = {}

        class _FakeProv:
            def ensure_started(self, agent):
                seen["agent"] = agent.name

        monkeypatch.setattr(provisioning, "get_provisioner", lambda mode, **kw: _FakeProv())
        await ss._ensure_container_started()
        assert seen["agent"] == "dymok"
