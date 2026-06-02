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
    def __init__(self, name, isolation_mode="local", working_dir=""):
        self.name = name
        self.isolation_mode = isolation_mode
        self.working_dir = working_dir


class _RecordingInner:
    """Inner CommandRunner double — records wrapped argvs, never spawns."""

    def __init__(self):
        self.calls: list[list[str]] = []

    async def run(self, argv, *, timeout=None, stdin=None):
        from pinky_daemon.command_runner import CommandResult

        self.calls.append(list(argv))
        return CommandResult(returncode=0, stdout=b"", stderr=b"")


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

    def test_container_runner_passes_in_container_cwd(self, monkeypatch):
        # The agent's working_dir (bind-mounted at the same path) becomes the
        # `podman exec -w` so tmux + claude run in the project dir in-container.
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(
            _FakeAgent("dymok", "container", working_dir="/srv/agents/dymok")
        ))
        runner = ss._select_command_runner()
        assert isinstance(runner, ContainerCommandRunner)
        assert runner.wrap(["tmux", "ls"]) == [
            "podman", "exec", "-w", "/srv/agents/dymok", "--",
            "pinky-dymok", "tmux", "ls",
        ]

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


@pytest.mark.asyncio
class TestSeedContainerTrust:
    async def test_noop_for_local_agent(self, monkeypatch):
        # Local agents seed trust on the host path; the in-container seeder is a
        # no-op for them (runner isn't a ContainerCommandRunner) — never execs.
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "local")))
        await ss._seed_container_trust("/tmp/x")  # must not raise / not exec

    async def test_execs_seed_in_container(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(
            _FakeAgent("dymok", "container", working_dir="/srv/agents/dymok")
        ))
        inner = _RecordingInner()
        runner = ContainerCommandRunner(
            "pinky-dymok", workdir="/srv/agents/dymok", inner=inner
        )
        monkeypatch.setattr(ss, "_select_command_runner", lambda: runner)
        await ss._seed_container_trust("/srv/agents/dymok")
        assert len(inner.calls) == 1
        cmd = inner.calls[0]
        # podman exec -w <wd> -- pinky-dymok python3 -c <seed> <project_dir>
        assert cmd[:2] == ["podman", "exec"]
        assert "pinky-dymok" in cmd and "python3" in cmd and "-c" in cmd
        assert cmd[-1] == "/srv/agents/dymok"  # project dir is the script arg
        seed = cmd[cmd.index("-c") + 1]
        assert "bypassPermissionsModeAccepted" in seed
        assert "hasTrustDialogAccepted" in seed
        assert "CLAUDE_CONFIG_DIR" in seed  # resolves the in-container config dir
