"""Container-isolation wiring in TmuxSession (gated runner injection + cold-start
ensure_started), plus the engaged-path gaps closed for #638: in-workdir
CLAUDE_CONFIG_DIR for the host-side tailer, PINKY_DAEMON_URL for in-container
hooks, the isolation_mode-implies-isolated secret gate, dead-container stderr
detection, host-side creds seeding, and the image-contract probe. All gated by
PINKY_CONTAINER_RUNTIME and isolation_mode; local agents and the gate-off
default must behave exactly as before. No real podman — the provisioner is
faked, runner selection is asserted via wrap() shapes."""

from __future__ import annotations

import re
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

import pinky_daemon.provisioning as provisioning
from pinky_daemon.command_runner import (
    CommandResult,
    ContainerCommandRunner,
    LocalCommandRunner,
)
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_session import (
    TmuxCommandResult,
    TmuxSession,
    _container_start_timeout_sec,
    _is_dead_runtime_stderr,
    _TmuxControl,
)


class _FakeAgent:
    def __init__(self, name, isolation_mode="local", working_dir="", isolated=False):
        self.name = name
        self.isolation_mode = isolation_mode
        self.working_dir = working_dir
        self.isolated = isolated


class _RecordingInner:
    """Inner CommandRunner double — records wrapped argvs, never spawns."""

    def __init__(self):
        self.calls: list[list[str]] = []

    async def run(self, argv, *, timeout=None, stdin=None):
        from pinky_daemon.command_runner import CommandResult

        self.calls.append(list(argv))
        return CommandResult(returncode=0, stdout=b"", stderr=b"")


class _StubInner:
    """Inner CommandRunner double with a scripted result — never spawns."""

    def __init__(self, *, stdout=b"", returncode=0, exc=None):
        self.stdout = stdout
        self.returncode = returncode
        self.exc = exc
        self.calls: list[list[str]] = []

    async def run(self, argv, *, timeout=None, stdin=None):
        self.calls.append(list(argv))
        if self.exc is not None:
            raise self.exc
        return CommandResult(returncode=self.returncode, stdout=self.stdout, stderr=b"")


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


def _session(agent_name="dymok", registry=None, working_dir="/tmp/x"):
    cfg = StreamingSessionConfig(agent_name=agent_name, working_dir=working_dir)
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
        # #638: the SESSION's working_dir ("/tmp/x" in the fixture) is the
        # canonical cwd — it is what tmux new-session -c gets, so podman exec
        # -w must agree with it (the registry row may hold a symlinked or
        # stale variant).
        assert runner.wrap(["tmux", "ls"]) == [
            "podman", "exec", "-w", "/tmp/x", "--", "pinky-dymok", "tmux", "ls",
        ]

    def test_container_runner_passes_in_container_cwd(self, monkeypatch):
        # The session's working_dir (bind-mounted at the same path) becomes the
        # `podman exec -w` so tmux + claude run in the project dir in-container.
        # The agent ROW's working_dir is only the fallback when the session
        # config carries none (#638 raw-vs-resolved divergence fix).
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(
            registry=_FakeRegistry(
                _FakeAgent("dymok", "container", working_dir="/srv/agents/dymok")
            ),
            working_dir="/srv/agents/dymok",
        )
        runner = ss._select_command_runner()
        assert isinstance(runner, ContainerCommandRunner)
        assert runner.wrap(["tmux", "ls"]) == [
            "podman", "exec", "-w", "/srv/agents/dymok", "--",
            "pinky-dymok", "tmux", "ls",
        ]

    def test_container_runner_falls_back_to_agent_row_workdir(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(
            registry=_FakeRegistry(
                _FakeAgent("dymok", "container", working_dir="/srv/agents/dymok")
            ),
            working_dir="",
        )
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
        # Stub the post-start image probe — otherwise it would exec a REAL
        # `podman exec ... sh -c` subprocess on the test host (passing only
        # because the probe tolerates errors).
        probe_calls = []

        async def _fake_probe():
            probe_calls.append(True)

        monkeypatch.setattr(ss, "_check_container_image_contract", _fake_probe)
        await ss._ensure_container_started()
        assert seen["agent"] == "dymok"
        assert probe_calls == [True]  # contract probe runs after start


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
        # #638 Pi live-validation: the GLOBAL onboarding flag must be seeded or
        # a fresh container config dir wedges on the first-run wizard's OAuth
        # sign-in step even with valid credentials present.
        assert "d['hasCompletedOnboarding']=True" in seed
        assert "CLAUDE_CONFIG_DIR" in seed  # resolves the in-container config dir
        # #735 lera rollout: both first-spawn wedges must be pre-cleared — the
        # project-MCP approval prompt and the bypass-permissions accept dialog
        # (whose default is "No, exit"; the queued-message paste's Enter kills
        # the REPL and strands the transport).
        assert "pr[proj]['enableAllProjectMcpServers']=True" in seed
        assert "skipDangerousModePermissionPrompt" in seed

    async def test_seed_script_writes_both_files(self, monkeypatch, tmp_path):
        """Execute the embedded seed script for real against a temp config dir:
        it must produce a valid .claude.json (trust + MCP approval) AND a
        settings.json with skipDangerousModePermissionPrompt, preserving any
        pre-existing settings keys."""
        import json
        import subprocess

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
        cmd = inner.calls[0]
        seed = cmd[cmd.index("-c") + 1]

        cfg = tmp_path / "cfgdir"
        cfg.mkdir()
        # Pre-existing settings must survive the merge.
        (cfg / "settings.json").write_text(json.dumps({"theme": "dark"}))
        subprocess.run(
            ["python3", "-c", seed, str(tmp_path / "proj")],
            check=True, env={"CLAUDE_CONFIG_DIR": str(cfg), "HOME": str(tmp_path)},
        )

        claude_json = json.loads((cfg / ".claude.json").read_text())
        assert claude_json["bypassPermissionsModeAccepted"] is True
        proj = claude_json["projects"][str(tmp_path / "proj")]
        assert proj["hasTrustDialogAccepted"] is True
        assert proj["enableAllProjectMcpServers"] is True

        settings = json.loads((cfg / "settings.json").read_text())
        assert settings["skipDangerousModePermissionPrompt"] is True
        assert settings["theme"] == "dark"  # merged, not clobbered

    async def test_seed_script_settings_path_without_config_dir(
        self, monkeypatch, tmp_path
    ):
        """Without CLAUDE_CONFIG_DIR the settings seed must land in
        ~/.claude/settings.json (claude's home-level settings path), not
        ~/settings.json."""
        import json
        import subprocess

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
        seed = inner.calls[0][inner.calls[0].index("-c") + 1]

        subprocess.run(
            ["python3", "-c", seed, str(tmp_path / "proj")],
            check=True, env={"HOME": str(tmp_path)},
        )
        settings = json.loads(
            (tmp_path / ".claude" / "settings.json").read_text()
        )
        assert settings["skipDangerousModePermissionPrompt"] is True
        assert not (tmp_path / "settings.json").exists()


@pytest.mark.asyncio
class TestSeedContainerHomeCreds:
    """#638 live-validation fix: claude reads OAuth creds from $HOME/.claude
    (the home VOLUME), not CLAUDE_CONFIG_DIR — the in-container seed copies
    the host-seeded config-dir creds into the volume, idempotently."""

    async def test_noop_for_local_agent(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "local")))
        await ss._seed_container_home_creds()  # must not raise / not exec

    async def test_execs_idempotent_copy_in_container(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(
            _FakeAgent("dymok", "container", working_dir="/srv/agents/dymok")
        ))
        inner = _RecordingInner()
        runner = ContainerCommandRunner(
            "pinky-dymok", workdir="/srv/agents/dymok", inner=inner
        )
        monkeypatch.setattr(ss, "_select_command_runner", lambda: runner)
        await ss._seed_container_home_creds()
        assert len(inner.calls) == 1
        cmd = inner.calls[0]
        assert cmd[:2] == ["podman", "exec"]
        assert "pinky-dymok" in cmd and "sh" in cmd and "-c" in cmd
        seed = cmd[cmd.index("-c") + 1]
        # Skip-if-present FIRST (an agent's own claude login is never
        # clobbered), then copy config-dir creds into $HOME/.claude at 0600.
        assert seed.startswith('test -f "$HOME/.claude/.credentials.json" ||')
        assert '"$CLAUDE_CONFIG_DIR/.credentials.json"' in seed
        assert 'chmod 600 "$HOME/.claude/.credentials.json"' in seed

    async def test_runner_failure_is_nonfatal(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(
            _FakeAgent("dymok", "container", working_dir="/srv/agents/dymok")
        ))
        inner = _StubInner(exc=RuntimeError("podman gone"))
        runner = ContainerCommandRunner(
            "pinky-dymok", workdir="/srv/agents/dymok", inner=inner
        )
        monkeypatch.setattr(ss, "_select_command_runner", lambda: runner)
        await ss._seed_container_home_creds()  # swallowed, never blocks spawn


def _claude_slug(path: str) -> str:
    """Claude Code's project-dir encoder: every non-alphanumeric char of the
    RESOLVED cwd becomes '-' (mirrors _project_dir's re.sub)."""
    return re.sub(r"[^a-zA-Z0-9]", "-", str(Path(path).resolve()))


class TestProjectDir:
    """#638 response pipeline: a container agent's claude runs with
    CLAUDE_CONFIG_DIR=<working_dir>/.claude-container inside the same-path bind
    mount, so the host-side tailer must look for transcripts there — and local
    agents must keep the unchanged ~/.claude/projects/<slug> path."""

    def test_container_agent_projects_under_in_workdir_config_dir(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        wd = str(tmp_path / "proj")
        ss = _session(
            registry=_FakeRegistry(_FakeAgent("dymok", "container", working_dir=wd)),
            working_dir=wd,
        )
        expected = Path(wd) / ".claude-container" / "projects" / _claude_slug(wd)
        assert ss._project_dir() == expected

    def test_local_agent_unchanged(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        wd = str(tmp_path / "proj")
        ss = _session(
            registry=_FakeRegistry(_FakeAgent("dymok", "local", working_dir=wd)),
            working_dir=wd,
        )
        assert ss._project_dir() == Path.home() / ".claude" / "projects" / _claude_slug(wd)


class TestBuildReplEnv:
    """#638 hooks: inside the container netns, localhost is the container, so
    container sessions must point the hook fleet at the host gateway via
    PINKY_DAEMON_URL (operator override: PINKY_CONTAINER_DAEMON_URL)."""

    def test_container_agent_gets_host_gateway_daemon_url(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        monkeypatch.delenv("PINKY_CONTAINER_DAEMON_URL", raising=False)
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "container")))
        env = ss._build_repl_env()
        assert env["PINKY_DAEMON_URL"] == "http://host.containers.internal:8888"

    def test_container_daemon_url_env_override_wins(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        monkeypatch.setenv("PINKY_CONTAINER_DAEMON_URL", "http://10.0.2.2:9999")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "container")))
        assert ss._build_repl_env()["PINKY_DAEMON_URL"] == "http://10.0.2.2:9999"

    def test_local_agent_has_no_daemon_url(self, monkeypatch):
        # Local hooks keep their localhost:8888 default — no key at all.
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "local")))
        assert "PINKY_DAEMON_URL" not in ss._build_repl_env()


class TestIsolationStatus:
    """#638 secret-gate hardening: a non-local isolation_mode IS isolation,
    regardless of the `isolated` bool — legacy rows / direct DB writes must
    never hand a container tenant the forgeable fleet-wide secret."""

    def test_container_mode_isolated_despite_false_flag(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        monkeypatch.setenv("PINKY_SESSION_SECRET", "fleet-secret")
        ss = _session(registry=_FakeRegistry(
            _FakeAgent("dymok", "container", isolated=False)
        ))
        assert ss._isolation_status() == "isolated"
        # And the env builder withholds the global secret accordingly.
        assert "PINKY_SESSION_SECRET" not in ss._build_repl_env()

    def test_local_mode_not_isolated_gets_secret(self, monkeypatch):
        monkeypatch.setenv("PINKY_SESSION_SECRET", "fleet-secret")
        ss = _session(registry=_FakeRegistry(
            _FakeAgent("dymok", "local", isolated=False)
        ))
        assert ss._isolation_status() == "not_isolated"
        assert ss._build_repl_env()["PINKY_SESSION_SECRET"] == "fleet-secret"


class TestIsDeadRuntimeStderr:
    """#638: a stopped/removed container is the same terminal condition as a
    vanished tmux pane — the worker must schedule disconnect, not silently eat
    every subsequent paste against a zombie."""

    @pytest.mark.parametrize(
        "stderr",
        [
            "can't find pane",
            "Error: can only create exec sessions on running containers: "
            "container state improper",
            "Error: no such container pinky-x",
            "container is not running",
            "Container Is Not Running",  # match is case-insensitive
        ],
    )
    def test_dead_runtime_detected(self, stderr):
        assert _is_dead_runtime_stderr(stderr) is True

    @pytest.mark.parametrize("stderr", ["", "some other error"])
    def test_other_stderr_not_dead(self, stderr):
        assert _is_dead_runtime_stderr(stderr) is False


class TestSeedContainerClaudeCreds:
    """#638 creds story: one-time host-side bootstrap of the daemon user's
    Claude OAuth creds into the container agent's host-visible
    <working_dir>/.claude-container, so the in-container claude starts
    authenticated. Best-effort, idempotent, opt-out via env."""

    def _creds_session(self, monkeypatch, tmp_path, *, host_creds=b'{"oauth": "tok"}'):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        monkeypatch.delenv("PINKY_CONTAINER_SEED_CREDS", raising=False)
        host_cfg = tmp_path / "host-claude"
        host_cfg.mkdir()
        if host_creds is not None:
            (host_cfg / ".credentials.json").write_bytes(host_creds)
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(host_cfg))
        wd = tmp_path / "agent-wd"
        wd.mkdir()
        ss = _session(
            registry=_FakeRegistry(_FakeAgent("dymok", "container", working_dir=str(wd))),
            working_dir=str(wd),
        )
        return ss, host_cfg, wd

    def test_copies_host_creds_with_0600(self, monkeypatch, tmp_path):
        ss, _host_cfg, wd = self._creds_session(monkeypatch, tmp_path)
        ss._seed_container_claude_creds()
        dst = wd / ".claude-container" / ".credentials.json"
        assert dst.read_bytes() == b'{"oauth": "tok"}'
        assert dst.stat().st_mode & 0o777 == 0o600

    def test_second_call_noops_when_dst_exists(self, monkeypatch, tmp_path):
        # The in-workdir copy is the durable one (a later in-container login /
        # token refresh writes there) — a re-seed must never clobber it.
        ss, host_cfg, wd = self._creds_session(monkeypatch, tmp_path)
        ss._seed_container_claude_creds()
        (host_cfg / ".credentials.json").write_bytes(b'{"oauth": "rotated"}')
        ss._seed_container_claude_creds()
        dst = wd / ".claude-container" / ".credentials.json"
        assert dst.read_bytes() == b'{"oauth": "tok"}'

    def test_disabled_via_env(self, monkeypatch, tmp_path):
        ss, _host_cfg, wd = self._creds_session(monkeypatch, tmp_path)
        monkeypatch.setenv("PINKY_CONTAINER_SEED_CREDS", "0")
        ss._seed_container_claude_creds()
        assert not (wd / ".claude-container" / ".credentials.json").exists()

    def test_missing_host_creds_nonfatal(self, monkeypatch, tmp_path):
        ss, _host_cfg, wd = self._creds_session(monkeypatch, tmp_path, host_creds=None)
        ss._seed_container_claude_creds()  # must not raise
        assert not (wd / ".claude-container" / ".credentials.json").exists()

    def test_skipped_when_static_token_forwarded(self, monkeypatch, tmp_path):
        # #780: with the static token forwarded, claude authenticates via
        # CLAUDE_CODE_OAUTH_TOKEN (no refresh) — the refresh-prone creds must
        # NOT be seeded, so there's nothing for concurrent agents to race on.
        ss, _host_cfg, wd = self._creds_session(monkeypatch, tmp_path)
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-deadbeef")
        ss._seed_container_claude_creds()
        assert not (wd / ".claude-container" / ".credentials.json").exists()

    def test_seeded_when_forward_flag_off(self, monkeypatch, tmp_path):
        # Token present but flag OFF (default) → forwarding inactive → seed as
        # before. Proves the skip is gated on the flag, not the token's presence.
        ss, _host_cfg, wd = self._creds_session(monkeypatch, tmp_path)
        monkeypatch.delenv("PINKY_FORWARD_OAUTH_TOKEN", raising=False)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-deadbeef")
        ss._seed_container_claude_creds()
        assert (wd / ".claude-container" / ".credentials.json").exists()

    def test_seed_skipped_when_flag_on_but_token_missing(self, monkeypatch, tmp_path):
        # #781 P2: flag ON but no token in env → fail CLOSED. Do NOT fall back to
        # seeding the refresh-prone creds (operator opted into static-token auth;
        # a misconfig must surface as a loud login wall, not a silent race).
        ss, _host_cfg, wd = self._creds_session(monkeypatch, tmp_path)
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)
        ss._seed_container_claude_creds()
        assert not (wd / ".claude-container" / ".credentials.json").exists()


class TestStaticOAuthTokenForward:
    """#780: forward a long-lived CLAUDE_CODE_OAUTH_TOKEN into the session env so
    claude authenticates with a never-refreshed token instead of the single-use
    OAuth refresh token — killing the shared-creds de-auth race that #777
    serialization only narrows. Flag-gated (PINKY_FORWARD_OAUTH_TOKEN, default
    OFF), withheld for custom-provider agents, and reaches container agents
    (whose isolated env can't inherit the daemon env otherwise)."""

    def _clear(self, monkeypatch):
        monkeypatch.delenv("PINKY_FORWARD_OAUTH_TOKEN", raising=False)
        monkeypatch.delenv("CLAUDE_CODE_OAUTH_TOKEN", raising=False)

    def test_default_off_not_forwarded(self, monkeypatch):
        # Token present in daemon env but flag unset → not injected (soak default).
        self._clear(monkeypatch)
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-x")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "local")))
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in ss._build_repl_env()

    def test_forwarded_when_flag_on(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-tok")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "local")))
        assert ss._build_repl_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-tok"

    def test_forwarded_to_container_agent(self, monkeypatch):
        # The whole point: a container agent's isolated env can't inherit the
        # daemon env, so the explicit -e is the only way the token reaches it.
        self._clear(monkeypatch)
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "on")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-c")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "container")))
        assert ss._build_repl_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat01-c"

    def test_withheld_for_custom_provider(self, monkeypatch):
        # Gateway-routed agents inject ANTHROPIC_* (higher precedence) — a
        # subscription token must never be mixed in alongside them.
        self._clear(monkeypatch)
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-tok")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "local")))
        ss._config.provider_key = "gw-secret"
        env = ss._build_repl_env()
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env["ANTHROPIC_API_KEY"] == "gw-secret"

    def test_withheld_for_custom_provider_url_without_key(self, monkeypatch):
        # #781 P1: a custom base URL with an EMPTY key is still a custom-provider
        # signal — a first-party subscription token must never reach a gateway.
        self._clear(monkeypatch)
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "sk-ant-oat01-tok")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "local")))
        ss._config.provider_url = "https://gateway.example/v1"
        env = ss._build_repl_env()
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in env
        assert env["ANTHROPIC_BASE_URL"] == "https://gateway.example/v1"

    def test_flag_on_but_no_token_noop(self, monkeypatch):
        self._clear(monkeypatch)
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")  # no token in env
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "local")))
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in ss._build_repl_env()


@pytest.mark.asyncio
class TestCheckContainerImageContract:
    """#638 fail-fast: the bring-your-own image must provide tmux, claude, and
    python3 — a missing binary raises a clear RuntimeError instead of an opaque
    tmux-spawn stderr minutes later. Probe errors themselves are tolerated."""

    def _contract_session(self, monkeypatch, inner):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(_FakeAgent("dymok", "container")))
        runner = ContainerCommandRunner("pinky-dymok", inner=inner)
        monkeypatch.setattr(ss, "_select_command_runner", lambda: runner)
        return ss

    async def test_missing_binary_raises_naming_it(self, monkeypatch):
        ss = self._contract_session(monkeypatch, _StubInner(stdout=b"claude\n"))
        with pytest.raises(RuntimeError, match="claude"):
            await ss._check_container_image_contract()

    async def test_all_binaries_present_no_raise(self, monkeypatch):
        inner = _StubInner(stdout=b"")
        ss = self._contract_session(monkeypatch, inner)
        await ss._check_container_image_contract()
        assert len(inner.calls) == 1  # exactly one sh -c probe, exec'd in-container
        assert inner.calls[0][:2] == ["podman", "exec"]

    async def test_probe_error_tolerated(self, monkeypatch):
        ss = self._contract_session(
            monkeypatch, _StubInner(exc=RuntimeError("podman exploded"))
        )
        await ss._check_container_image_contract()  # must not raise (logged only)


class TestContainerAgentStrict:
    """#638 review fix: ``strict=True`` (the SPAWN path) fails CLOSED on a
    registry lookup failure — silently falling back to a LocalCommandRunner
    would launch a container-labeled agent UNISOLATED on the host. The default
    (read-side) fail-safe keeps returning None for the same failure."""

    def test_strict_raises_refusing_to_spawn_when_registry_fails(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(raises=True))
        with pytest.raises(RuntimeError, match="refusing to spawn"):
            ss._container_agent(strict=True)

    def test_non_strict_returns_none_for_same_failing_registry(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        ss = _session(registry=_FakeRegistry(raises=True))
        assert ss._container_agent() is None  # read-side consumers stay fail-safe

    def test_strict_gate_off_short_circuits_before_registry(self, monkeypatch):
        # The gate check precedes the lookup, so even strict mode never raises
        # (and never consults the broken registry) when the runtime is off.
        monkeypatch.delenv("PINKY_CONTAINER_RUNTIME", raising=False)
        ss = _session(registry=_FakeRegistry(raises=True))
        assert ss._container_agent(strict=True) is None


@pytest.mark.asyncio
class TestTmuxControlSetCommandRunner:
    """#638: set_command_runner swaps the execution seam on a LIVE _TmuxControl
    — the runner is re-selected at every spawn, so a session that survives an
    isolation_mode flip must start exec'ing through the new runner."""

    async def test_swap_wraps_subsequent_commands_in_podman_exec(self):
        inner = _RecordingInner()
        # The recording inner stands in for the LocalCommandRunner: argv runs
        # verbatim (no wrap) until the seam is swapped.
        control = _TmuxControl("pinky-dymok-main", command_runner=inner)
        await control.has_session()
        assert inner.calls[-1] == ["tmux", "has-session", "-t", "pinky-dymok-main"]

        control.set_command_runner(
            ContainerCommandRunner(
                "pinky-dymok", workdir="/srv/agents/dymok", inner=inner
            )
        )
        await control.has_session()
        assert inner.calls[-1] == [
            "podman", "exec", "-w", "/srv/agents/dymok", "--", "pinky-dymok",
            "tmux", "has-session", "-t", "pinky-dymok-main",
        ]

    async def test_swap_back_to_local_unwraps(self):
        # The reverse flip (container -> local) must stop podman-wrapping.
        inner = _RecordingInner()
        control = _TmuxControl(
            "pinky-dymok-main",
            command_runner=ContainerCommandRunner("pinky-dymok", inner=inner),
        )
        await control.has_session()
        assert inner.calls[-1][:2] == ["podman", "exec"]

        control.set_command_runner(inner)
        await control.has_session()
        assert inner.calls[-1] == ["tmux", "has-session", "-t", "pinky-dymok-main"]


class TestContainerStartTimeoutSec:
    """#638: the provision+start budget at spawn — env-overridable, with a
    600s default (a legitimate podman pull can take minutes) and a 1s floor."""

    def test_default_600(self, monkeypatch):
        monkeypatch.delenv("PINKY_CONTAINER_START_TIMEOUT_SEC", raising=False)
        assert _container_start_timeout_sec() == 600.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_START_TIMEOUT_SEC", "42")
        assert _container_start_timeout_sec() == 42.0

    def test_garbage_value_falls_back_to_default(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_START_TIMEOUT_SEC", "soon-ish")
        assert _container_start_timeout_sec() == 600.0

    def test_floor_at_one_second(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_START_TIMEOUT_SEC", "0.05")
        assert _container_start_timeout_sec() == 1.0

    def test_blank_value_is_default(self, monkeypatch):
        monkeypatch.setenv("PINKY_CONTAINER_START_TIMEOUT_SEC", "   ")
        assert _container_start_timeout_sec() == 600.0


@pytest.mark.asyncio
class TestSpawnRebindsCommandRunner:
    """#638 (review-confirmed critical): _spawn_tmux_repl re-selects the
    execution seam from a fresh strict registry snapshot on EVERY spawn.
    Session objects survive isolation_mode flips (PUT /agents tears nothing
    down), so a runner fixed at construction would silently launch a
    flipped-to-container agent on the HOST."""

    def _spawnable_session(self, monkeypatch, tmp_path, agent):
        registry = _FakeRegistry(agent)
        ss = _session(registry=registry, working_dir=str(tmp_path))
        # Stub every spawn collaborator with side effects beyond the seam.
        seen = {"ensure_started": []}

        async def fake_ensure(agent=None):
            seen["ensure_started"].append(agent)

        async def fake_seed_trust(project_dir):
            return None

        async def fake_start_tailer():
            return None

        monkeypatch.setattr(ss, "_ensure_container_started", fake_ensure)
        monkeypatch.setattr(ss, "_seed_container_claude_creds", lambda: None)
        monkeypatch.setattr(ss, "_seed_container_trust", fake_seed_trust)
        monkeypatch.setattr(ss, "_build_claude_cmd", lambda: "claude")
        monkeypatch.setattr(ss, "_start_tailer", fake_start_tailer)
        ss._tmux.has_session = AsyncMock(return_value=False)
        ss._tmux.kill_session = AsyncMock()
        ss._tmux.new_session = AsyncMock(
            return_value=TmuxCommandResult(returncode=0, stdout="", stderr="")
        )
        return ss, seen

    async def test_spawn_after_container_flip_binds_container_runner(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        # The row starts LOCAL — the session was constructed for a local agent.
        agent = _FakeAgent("dymok", "local", working_dir=str(tmp_path))
        ss, seen = self._spawnable_session(monkeypatch, tmp_path, agent)
        # PUT /agents flips the registry row with no session teardown.
        agent.isolation_mode = "container"

        await ss._spawn_tmux_repl()

        ss._tmux.set_command_runner.assert_called_once()
        runner = ss._tmux.set_command_runner.call_args[0][0]
        assert isinstance(runner, ContainerCommandRunner)
        # Bound to THIS agent's container, exec'ing in the session's cwd.
        assert runner.wrap(["tmux"]) == [
            "podman", "exec", "-w", str(tmp_path), "--", "pinky-dymok", "tmux",
        ]
        # And the same strict snapshot drove the container start.
        assert seen["ensure_started"] == [agent]

    async def test_spawn_for_local_row_binds_local_runner(self, monkeypatch, tmp_path):
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        agent = _FakeAgent("dymok", "local", working_dir=str(tmp_path))
        ss, seen = self._spawnable_session(monkeypatch, tmp_path, agent)

        await ss._spawn_tmux_repl()

        runner = ss._tmux.set_command_runner.call_args[0][0]
        assert isinstance(runner, LocalCommandRunner)
        assert seen["ensure_started"] == [None]  # no container agent snapshot

    async def test_spawn_fails_closed_when_registry_breaks(self, monkeypatch, tmp_path):
        # A registry failure at spawn raises (-> BOOT_FAILED) instead of
        # quietly re-binding a LocalCommandRunner.
        monkeypatch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
        agent = _FakeAgent("dymok", "container", working_dir=str(tmp_path))
        ss, _seen = self._spawnable_session(monkeypatch, tmp_path, agent)
        ss._registry = _FakeRegistry(raises=True)

        with pytest.raises(RuntimeError, match="refusing to spawn"):
            await ss._spawn_tmux_repl()
        ss._tmux.set_command_runner.assert_not_called()
        ss._tmux.new_session.assert_not_called()
