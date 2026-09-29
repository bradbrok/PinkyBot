"""Codex launch authority is scoped independently of ambient compatibility mode."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon import codex_session, codex_tmux_session, tmux_session
from pinky_daemon.auth import resolve_request_signing_secret
from pinky_daemon.codex_app_server_tmux import CodexAppServerSupervisor
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.shared_mcp import derive_mcp_bearer
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_launch_env_loader import BASE_ALLOWLIST, DAEMON_ONLY

KINDS = ("repl", "exec", "app_server", "tmux_app_server")
SENTINEL = "codex-env-private-" + "q" * 48
HEADER = "PINKY_MCP_HDR_TEST_AUTHORIZATION"


def scan_outputs(*outputs):
    protected = (SENTINEL, derive_mcp_bearer(SENTINEL + "own"))
    leaked = any(value in output for output in outputs for value in protected)
    assert not leaked, "protected test value found in output"


class Registry:
    def __init__(self, isolated=True, key=SENTINEL + "own"):
        self.isolated = isolated
        self.key = key
        self.mode = "local"

    def get(self, name):
        return SimpleNamespace(
            isolated=self.isolated,
            isolation_mode=self.mode,
            tool_policy_enabled=False,
            dedicated_config_dir=False,
        )

    def get_signing_key(self, name):
        return self.key


class Harness:
    def __init__(self, root, patch):
        self.root, self.patch = root, patch
        self.logs = []
        self.registry = Registry()
        self.supervisors = []

    def make(self, kind, *, provider_key=SENTINEL + "provider", provider_url="codex_cli"):
        config = StreamingSessionConfig(
            agent_name="test-agent",
            working_dir=str(self.root),
            provider_key=provider_key,
            provider_url=provider_url,
            mcp_servers={
                "test": {
                    "url": "http://127.0.0.1:1/mcp",
                    "headers": {
                        "Authorization": "Bearer " + derive_mcp_bearer(self.registry.key),
                    },
                }
            },
        )
        if kind == "repl":
            owner = CodexTmuxSession(config, registry=self.registry)
            return owner, owner._build_repl_env
        if kind == "tmux_app_server":
            owner = CodexAppServerSupervisor(
                "test-agent",
                working_dir=str(self.root),
                openai_api_key=provider_key,
                agent_config=config,
                registry=self.registry,
                log=self.logs.append,
            )
            # Keep test-owned Unix socket paths short without using shared live state.
            import tempfile

            if owner._sock_dir_is_tmp:
                Path(owner._sock_dir).rmdir()
            owner._sock_dir = tempfile.mkdtemp(prefix="k806-uds-")
            owner._sock_dir_is_tmp = True
            owner.sock_path = str(Path(owner._sock_dir) / "app.sock")
            # macOS may need the existing /tmp fallback for the production UDS limit.
            if len(owner.sock_path) > 100:
                Path(owner._sock_dir).rmdir()
                owner._sock_dir = tempfile.mkdtemp(prefix="k806-uds-", dir="/tmp")
                owner.sock_path = str(Path(owner._sock_dir) / "app.sock")
            self.supervisors.append(owner)
            return owner, owner._build_env
        self.patch.setenv("PINKY_CODEX_APP_SERVER", "1" if kind == "app_server" else "0")
        owner = CodexSession(config, registry=self.registry)
        return owner, owner._build_codex_env


@pytest.fixture
def harness(tmp_path, monkeypatch, capsys):
    for name in tuple(os.environ):
        if name not in {"PATH", "TMPDIR"}:
            monkeypatch.delenv(name)
    home = tmp_path / "home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PINKY_CODEX_PER_AGENT_HOME", "0")
    monkeypatch.setenv("PINKY_CODEX_APP_SERVER", "0")
    monkeypatch.setenv("PINKY_ISOLATED_ENV", "off")
    grants = tmp_path / "grants.json"
    grants.write_text("{}")
    grants.chmod(0o600)
    monkeypatch.setenv("PINKY_ISOLATED_ENV_GRANTS_FILE", str(grants))
    h = Harness(tmp_path, monkeypatch)
    for module in (codex_session, codex_tmux_session, tmux_session):
        monkeypatch.setattr(module, "_log", h.logs.append)
    yield h
    import shutil

    for owner in h.supervisors:
        shutil.rmtree(owner._sock_dir, ignore_errors=True)
    captured = capsys.readouterr()
    scan_outputs(*h.logs, captured.out, captured.err)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", ("off", "shadow", "enforce"))
def test_payload_excludes_daemon_authority_in_every_mode(harness, kind, mode):
    h = harness
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    for name in DAEMON_ONLY:
        h.patch.setenv(name, SENTINEL)
    _, build = h.make(kind)
    absent = not DAEMON_ONLY.intersection(build())
    assert absent, "daemon authority entered Codex payload"


@pytest.mark.parametrize("kind", KINDS)
def test_isolated_off_reports_exact_candidate_drops_without_enforcing(harness, kind):
    h = harness
    for name in (*DAEMON_ONLY, "ORDINARY_TOOL_CONFIG", "PINKY_MCP_HDR_FOREIGN_AUTHORIZATION"):
        h.patch.setenv(name, SENTINEL)
    h.patch.setenv("LC_TIME", "C")
    h.patch.setenv("MULTILINE_TOOL_CONFIG", SENTINEL + "\nsecond-line")
    h.patch.setenv("TMUX", "synthetic-socket,1,0")
    h.patch.setenv("TMUX_PANE", "%999999")
    h.patch.setenv("XDG_CONFIG_HOME", str(h.root / "xdg"))
    _, build = h.make(kind)
    before = set(os.environ)
    env = build()
    retained = "ORDINARY_TOOL_CONFIG" in env
    assert retained, "observation mode removed ordinary tool configuration"
    prefix = "isolated_launch_env_shadow "
    reports = [json.loads(line[len(prefix) :]) for line in h.logs if line.startswith(prefix)]
    assert len(reports) == 1, "isolated off launch did not emit one shadow report"
    explicit = {"OPENAI_API_KEY", "CODEX_HOME", "PINKY_AGENT_NAME", "PINKY_AGENT_KEY"}
    expected = sorted(
        name
        for name in before
        if name not in BASE_ALLOWLIST | explicit and not name.startswith(("LC_", "XDG_"))
    )
    exact = reports[0]["would_drop_names"] == expected
    assert exact, "shadow report does not match the exact scoped candidate"
    assert reports[0]["would_drop_count"] == len(expected)
    assert reports[0]["enforced"] is False
    assert reports[0]["source"] == "daemon"


@pytest.mark.parametrize("kind", KINDS)
def test_nonisolated_off_retains_ordinary_config_without_report(harness, kind):
    h = harness
    h.registry.isolated = False
    h.patch.setenv("ORDINARY_TOOL_CONFIG", SENTINEL)
    _, build = h.make(kind)
    matches = build().get("ORDINARY_TOOL_CONFIG") == SENTINEL
    assert matches
    assert not any(line.startswith("isolated_launch_env_shadow ") for line in h.logs)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("mode", ("off", "shadow", "enforce", "invalid-mode"))
def test_nonisolated_launch_keeps_current_env_signing_identity(harness, kind, mode):
    from pinky_daemon.auth import build_internal_auth_headers, verify_internal_request

    h = harness
    h.registry.isolated = False
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    h.patch.setenv("PINKY_AGENT_KEY", SENTINEL + "foreign")
    for name in DAEMON_ONLY:
        h.patch.setenv(name, SENTINEL)
    _, build = h.make(kind)
    env = build()
    current_identity = env.get("PINKY_AGENT_KEY") == h.registry.key
    authority_absent = not DAEMON_ONLY.intersection(env)
    signed = build_internal_auth_headers(
        env.get("PINKY_AGENT_KEY", ""),
        agent_name="test-agent",
        method="GET",
        path="/agents/me",
    )
    verified = verify_internal_request(
        "",
        agent_name="test-agent",
        method="GET",
        path="/agents/me",
        timestamp=signed.get("x-pinky-timestamp", ""),
        signature=signed.get("x-pinky-signature", ""),
        agent_key=h.registry.key,
        allow_global_secret=False,
    )
    assert current_identity and verified, "launch lost its available scoped signing identity"
    assert authority_absent, "signing compatibility restored global authority"


@pytest.mark.parametrize("kind", KINDS)
def test_invalid_mode_still_refuses_isolated_launch(harness, kind):
    from pinky_daemon.isolated_launch_env import LaunchConfigError

    harness.patch.setenv("PINKY_ISOLATED_ENV", "invalid-mode")
    _, build = harness.make(kind)
    with pytest.raises(LaunchConfigError, match="mode configuration refused"):
        build()


@pytest.mark.parametrize("mode", ("off", "shadow", "invalid-mode"))
async def test_repl_captures_one_policy_per_launch_despite_key_rotation(harness, mode):
    from unittest.mock import AsyncMock

    from pinky_daemon import isolated_launch_env
    from pinky_daemon.tmux_session import TmuxCommandResult

    h = harness
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    h.registry.isolated = mode != "invalid-mode"
    owner, _ = h.make("repl")
    snapshots, wrapped_policies, delivered = [], [], []
    real_capture = isolated_launch_env.capture_policy
    real_wrap = owner._wrap_launch_command

    def capture_then_rotate(**kwargs):
        policy = real_capture(**kwargs)
        snapshots.append(policy)
        h.registry.key = SENTINEL + "rotated-" + str(len(snapshots))
        return policy

    def record_wrap(command, env, policy):
        wrapped_policies.append(policy)
        return real_wrap(command, env, policy)

    async def record_spawn(**kwargs):
        delivered.append(kwargs["env"])
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    h.patch.setattr(isolated_launch_env, "capture_policy", capture_then_rotate)
    h.patch.setattr(owner, "_wrap_launch_command", record_wrap)
    h.patch.setattr(owner._tmux, "new_session", record_spawn)
    h.patch.setattr(owner._tmux, "has_session", AsyncMock(side_effect=[False, True] * 2))
    for name in (
        "_ensure_container_started",
        "_reap_retained_spawn_cleanup_debt",
        "_seed_container_trust",
        "_seed_container_home_creds",
        "_start_tailer",
        "_stop_tailer",
        "_codex_dismiss_nux_and_ready",
    ):
        h.patch.setattr(owner, name, AsyncMock())
    h.patch.setattr(owner, "_container_agent", lambda **kwargs: None)
    h.patch.setattr(owner, "_select_command_runner", lambda *args: owner._tmux._runner)
    h.patch.setattr(owner, "_prepare_tmux_spawn", lambda: None)
    h.patch.setattr(owner, "_has_prior_transcript", lambda: False)
    h.patch.setattr(owner, "_spawn_cleanup_state_dir", lambda: h.root / "home")
    h.patch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0)
    h.patch.setattr(tmux_session, "_seed_claude_trust_file", lambda *args: False)

    for index in range(2):
        expected_key = h.registry.key
        await owner._spawn_tmux_repl()
        assert len(snapshots) == index + 1, "REPL recaptured policy during the same launch"
        same_snapshot = wrapped_policies[index] is snapshots[index]
        same_identity = delivered[index].get("PINKY_AGENT_KEY") == expected_key
        assert same_snapshot and same_identity, "launch environment diverged from captured policy"
    if mode != "invalid-mode":
        reports = [line for line in h.logs if line.startswith("isolated_launch_env_shadow ")]
        assert len(reports) == 2, "preflight duplicated or lost the final launch report"


@pytest.mark.parametrize("kind", KINDS)
def test_current_scoped_identity_replaces_foreign_ambient_key(harness, kind):
    h = harness
    h.patch.setenv("PINKY_AGENT_KEY", SENTINEL + "foreign")
    h.patch.setenv("PINKY_AGENT_NAME", "foreign-agent")
    _, build = h.make(kind)
    env = build()
    own_key = env.get("PINKY_AGENT_KEY") == h.registry.key
    own_name = env.get("PINKY_AGENT_NAME") == "test-agent"
    assert own_key and own_name, "foreign identity was not replaced by explicit current identity"


@pytest.mark.parametrize("kind", KINDS)
def test_withheld_key_cannot_return_from_ambient(harness, kind):
    h = harness
    h.registry.key = ""
    h.patch.setenv("PINKY_AGENT_KEY", SENTINEL)
    _, build = h.make(kind)
    absent = "PINKY_AGENT_KEY" not in build()
    assert absent, "withheld scoped key was filled by ambient"


@pytest.mark.parametrize("kind", KINDS)
def test_runtime_marker_is_never_a_provider_endpoint(harness, kind):
    h = harness
    h.patch.setenv("PINKY_ISOLATED_ENV", "enforce")
    _, build = h.make(kind, provider_url="codex_cli")
    valid = build().get("OPENAI_BASE_URL") != "codex_cli"
    assert valid, "runtime marker entered provider routing"


@pytest.mark.parametrize("kind", KINDS)
def test_isolated_enforce_drops_ungranted_ambient(harness, kind):
    h = harness
    h.patch.setenv("PINKY_ISOLATED_ENV", "enforce")
    h.patch.setenv("ORDINARY_TOOL_CONFIG", SENTINEL)
    _, build = h.make(kind)
    absent = "ORDINARY_TOOL_CONFIG" not in build()
    assert absent, "enforced launch retained ungranted ambient configuration"


@pytest.mark.parametrize("kind", KINDS)
def test_mcp_namespace_contains_only_current_launch(harness, kind):
    h = harness
    h.patch.setenv("PINKY_MCP_HDR_FOREIGN_AUTHORIZATION", SENTINEL)
    _, build = h.make(kind)
    env = build()
    headers = {name for name in env if name.startswith("PINKY_MCP_HDR_")}
    expected = {HEADER} if kind in {"repl", "exec"} else set()
    exact = headers == expected
    assert exact, "foreign MCP header namespace survived"
    if expected:
        matches = env[HEADER] == "Bearer " + derive_mcp_bearer(h.registry.key)
        assert matches


@pytest.mark.parametrize("kind", KINDS)
def test_path_and_explicit_api_key_control(harness, kind):
    h = harness
    h.patch.setenv("PATH", "/synthetic-tools/bin" + os.pathsep + os.defpath)
    h.patch.setenv("OPENAI_API_KEY", SENTINEL + "ambient")
    _, build = h.make(kind)
    env = build()
    path_matches = env.get("PATH") == os.environ["PATH"]
    key_matches = env.get("OPENAI_API_KEY") == SENTINEL + "provider"
    assert path_matches and key_matches


def test_scoped_stdio_auth_works_without_global_and_keeps_rotation_rule(harness):
    h = harness
    h.patch.setenv("PINKY_AGENT_KEY", SENTINEL + "stale")
    for name in DAEMON_ONLY:
        h.patch.delenv(name, raising=False)
    current = resolve_request_signing_secret("test-agent", lambda _: h.registry.key)
    valid = current == h.registry.key
    assert valid
    no_stale_fallback = resolve_request_signing_secret("test-agent", lambda _: None) == ""
    assert no_stale_fallback


def test_output_scanner_detects_planted_value():
    with pytest.raises(AssertionError, match="protected test value"):
        scan_outputs("diagnostic " + SENTINEL)


@pytest.mark.parametrize("kind", ("repl", "tmux_app_server"))
@pytest.mark.parametrize("server_only", (False, True))
@pytest.mark.parametrize("withheld", (False, True))
async def test_real_private_child_cannot_inherit_daemon_authority(
    harness, kind, server_only, withheld
):
    import asyncio
    import shlex
    import shutil
    import subprocess
    import sys
    from unittest.mock import AsyncMock

    from pinky_daemon.tmux_session import _TmuxControl

    h = harness
    binary = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if not binary:
        pytest.skip("tmux unavailable")
    socket = str(h.root / "tmux.sock")
    base = [binary, "-S", socket, "-f", "/dev/null"]
    bindir = h.root / "bin"
    bindir.mkdir()
    report = h.root / "child-names.json"
    script = bindir / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys,time\n"
        f"pathlib.Path({str(report)!r}).write_text(json.dumps(sorted(os.environ)))\n"
        "if 'app-server' in sys.argv:\n"
        " for line in sys.stdin:\n"
        "  frame=json.loads(line)\n"
        "  if 'id' in frame: print(json.dumps({'id':frame['id'],'result':{}}),flush=True)\n"
        "else: time.sleep(60)\n"
    )
    script.chmod(0o700)
    h.patch.setenv("PATH", str(bindir) + os.pathsep + os.defpath)
    h.patch.setenv("PYTHONPATH", str(Path(tmux_session.__file__).resolve().parents[1]))
    seed = {
        "HOME": os.environ["HOME"],
        "PATH": os.environ["PATH"],
        "SHELL": "/bin/sh",
        "TERM": "xterm",
        "PYTHONPATH": os.environ["PYTHONPATH"],
        **{name: SENTINEL for name in DAEMON_ONLY},
        "PINKY_MCP_HDR_FOREIGN_AUTHORIZATION": SENTINEL,
    }
    withheld_names = {
        "PINKY_AGENT_KEY",
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_HOME",
        "PINKY_DAEMON_URL",
        "PINKY_TOOL_POLICY",
    }
    if withheld:
        h.registry.key = ""
        seed.update({name: SENTINEL for name in withheld_names})
    for name in DAEMON_ONLY:
        if server_only:
            h.patch.delenv(name, raising=False)
        else:
            h.patch.setenv(name, SENTINEL)
    control = _TmuxControl("probe", tmux_binary=binary, socket_path=socket)
    h.patch.setattr(control, "_base_cmd", lambda: base)
    commands, outputs = [], []
    real_run = control._run

    async def recorded(*args, **kwargs):
        commands.append(shlex.join(args))
        result = await real_run(*args, **kwargs)
        outputs.extend((result.stdout, result.stderr))
        return result

    h.patch.setattr(control, "_run", recorded)
    client = None
    started = False
    try:
        result = subprocess.run(
            [*base, "new-session", "-d", "-s", "seed", "/bin/sleep 60"],
            env=seed,
            capture_output=True,
            timeout=5,
        )
        assert result.returncode == 0, "private server setup failed"
        started = True
        # A direct inert pane proves server globals exist without ever printing values.
        positive = h.root / "positive-names.json"
        positive_command = shlex.join(
            [
                sys.executable,
                "-c",
                "import json,os,pathlib;pathlib.Path("
                + repr(str(positive))
                + ").write_text(json.dumps(sorted(os.environ)))",
            ]
        )
        planted = await control._run("new-window", "-t", "=seed", positive_command)
        assert planted.ok
        for _ in range(200):
            if positive.exists():
                break
            await asyncio.sleep(0.01)
        assert positive.exists(), "server-global positive control did not report"
        present = DAEMON_ONLY.issubset(json.loads(positive.read_text()))
        assert present, "private server did not carry planted authority names"
        owner, _ = h.make(kind, provider_key="" if withheld else SENTINEL + "provider")
        owner._tmux = control
        if kind == "repl":
            for name in (
                "_ensure_container_started",
                "_reap_retained_spawn_cleanup_debt",
                "_seed_container_trust",
                "_seed_container_home_creds",
                "_stop_tailer",
                "_start_tailer",
            ):
                h.patch.setattr(owner, name, AsyncMock())
            h.patch.setattr(owner, "_container_agent", lambda **kwargs: None)
            h.patch.setattr(owner, "_select_command_runner", lambda *args: control._runner)
            h.patch.setattr(owner, "_prepare_tmux_spawn", lambda: None)
            h.patch.setattr(owner, "_has_prior_transcript", lambda: False)
            h.patch.setattr(owner, "_spawn_cleanup_state_dir", lambda: h.root / "home")
            h.patch.setattr(owner, "_codex_dismiss_nux_and_ready", AsyncMock())
            h.patch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0.01)
            await owner._spawn_tmux_repl()
        else:
            client, _ = await owner.start()
        for _ in range(200):
            if report.exists():
                break
            await asyncio.sleep(0.01)
        assert report.exists(), "inert child did not report its environment names"
        names = set(json.loads(report.read_text()))
        absent = not DAEMON_ONLY.intersection(names)
        assert absent, "real child inherited daemon authority"
        foreign_header_absent = "PINKY_MCP_HDR_FOREIGN_AUTHORIZATION" not in names
        assert foreign_header_absent, "real child inherited a foreign MCP header"
        if withheld:
            absent_owned = not withheld_names.intersection(names)
            assert absent_owned, "server filled a withheld builder-owned name"
    finally:
        if client is not None:
            await client.close()
        if started:
            end = subprocess.run([*base, "kill-server"], env=seed, capture_output=True, timeout=5)
            outputs.extend(
                (end.stdout.decode(errors="replace"), end.stderr.decode(errors="replace"))
            )
        scan_outputs(*commands, *outputs)


@pytest.mark.parametrize("kind", KINDS)
def test_enforce_grants_cannot_fill_withheld_owned_names(harness, kind):
    h = harness
    h.patch.setenv("PINKY_ISOLATED_ENV", "enforce")
    h.registry.key = ""
    for name in ("OPENAI_API_KEY", "PINKY_AGENT_KEY", "ORDINARY_TOOL_CONFIG"):
        h.patch.setenv(name, SENTINEL)
    Path(os.environ["PINKY_ISOLATED_ENV_GRANTS_FILE"]).write_text(
        json.dumps(
            {
                "test-agent": ["OPENAI_API_KEY", "PINKY_AGENT_KEY", "ORDINARY_TOOL_CONFIG"],
            }
        )
    )
    owner, build = h.make(kind, provider_key="")
    owner._openai_api_key = ""
    env = build()
    withheld = not {"OPENAI_API_KEY", "PINKY_AGENT_KEY"}.intersection(env)
    assert withheld, "grant filled a withheld builder-owned value"
    granted = env.get("ORDINARY_TOOL_CONFIG") == SENTINEL
    assert granted, "valid exact grant was lost"


@pytest.mark.parametrize("kind", KINDS)
def test_late_ambient_api_key_cannot_fill_absent_resolved_key(harness, kind):
    h = harness
    owner, build = h.make(kind, provider_key="")
    h.patch.setenv("OPENAI_API_KEY", SENTINEL)
    absent = "OPENAI_API_KEY" not in build()
    assert absent, "late ambient value filled an omitted builder output"


@pytest.mark.parametrize("kind", KINDS)
def test_policy_is_refreshed_for_replacement(harness, kind):
    h = harness
    h.registry.isolated = False
    h.patch.setenv("ORDINARY_TOOL_CONFIG", SENTINEL)
    _, build = h.make(kind)
    assert "ORDINARY_TOOL_CONFIG" in build()
    assert not any(line.startswith("isolated_launch_env_shadow ") for line in h.logs)
    h.registry.isolated = True
    build()
    assert sum(line.startswith("isolated_launch_env_shadow ") for line in h.logs) == 1
    h.patch.setenv("PINKY_ISOLATED_ENV", "enforce")
    dropped = "ORDINARY_TOOL_CONFIG" not in build()
    assert dropped, "replacement used a stale observation policy"


@pytest.mark.parametrize("kind", ("exec", "app_server"))
@pytest.mark.parametrize("mode", ("off", "shadow", "enforce"))
async def test_real_direct_child_has_filtered_environment(harness, kind, mode):
    import asyncio
    import sys

    h = harness
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    for name in (*DAEMON_ONLY, "ORDINARY_TOOL_CONFIG", "PINKY_MCP_HDR_FOREIGN_AUTHORIZATION"):
        h.patch.setenv(name, SENTINEL)
    bindir = h.root / "bin"
    bindir.mkdir()
    report = h.root / "direct-names.json"
    script = bindir / "codex"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json,os,pathlib,sys\n"
        f"pathlib.Path({str(report)!r}).write_text(json.dumps(sorted(os.environ)))\n"
        "if 'app-server' in sys.argv:\n"
        " for line in sys.stdin:\n"
        "  frame=json.loads(line)\n"
        "  if 'id' in frame: print(json.dumps({'id':frame['id'],'result':{}}),flush=True)\n"
        "else:\n"
        " sys.stdin.read()\n"
        " print(json.dumps({'type':'turn.completed','usage':{'input_tokens':1,'output_tokens':1}}))\n"
    )
    script.chmod(0o700)
    h.patch.setenv("PATH", str(bindir) + os.pathsep + os.defpath)
    owner, _ = h.make(kind)
    try:
        if kind == "exec":
            await owner._exec_codex("synthetic prompt")
        else:
            assert await owner._ensure_app_server()
        for _ in range(200):
            if report.exists():
                break
            await asyncio.sleep(0.01)
        assert report.exists(), "actual direct child did not report names"
        names = set(json.loads(report.read_text()))
        absent = not DAEMON_ONLY.intersection(names)
        assert absent, "actual direct child received daemon authority"
        assert ("ORDINARY_TOOL_CONFIG" in names) is (mode != "enforce")
        assert "PINKY_MCP_HDR_FOREIGN_AUTHORIZATION" not in names
        assert "PINKY_AGENT_KEY" in names and "PINKY_AGENT_NAME" in names
    finally:
        if kind == "app_server":
            await owner._teardown_app_server()


@pytest.mark.real_transport
@pytest.mark.parametrize("mode", ("off", "enforce"))
async def test_real_codex_loopback_attach_uses_scoped_header(harness, mode):
    import asyncio
    import shutil

    from aiohttp import web

    from pinky_daemon.codex_app_server import CodexAppServerClient
    from pinky_daemon.codex_mcp_env import mcp_cli_config

    h = harness
    binary = shutil.which("codex")
    if not binary:
        pytest.skip("installed Codex CLI unavailable")
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    for name in DAEMON_ONLY:
        h.patch.setenv(name, SENTINEL)
    observed = []
    expected = "Bearer " + derive_mcp_bearer(h.registry.key)

    async def refuse(request):
        observed.append(request.headers.get("Authorization") == expected)
        return web.Response(status=503, text="test gateway unavailable")

    app = web.Application()
    app.router.add_route("*", "/{path:.*}", refuse)
    server = web.AppRunner(app)
    await server.setup()
    proc = client = None
    try:
        site = web.TCPSite(server, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        owner, build = h.make("repl", provider_url=f"http://127.0.0.1:{port}/v1")
        owner._codex_mcp_servers["test"]["url"] = f"http://127.0.0.1:{port}/mcp"
        args, _ = mcp_cli_config(owner._codex_mcp_servers)
        env = build()
        if mode == "enforce":
            # The existing clean-mode contract supplies an explicit pre-provisioned home.
            Path(env["CODEX_HOME"]).mkdir(mode=0o700)
        absent = not DAEMON_ONLY.intersection(env)
        assert absent
        proc = await asyncio.create_subprocess_exec(
            binary,
            *args,
            "app-server",
            cwd=h.root,
            env=env,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        client = CodexAppServerClient(
            proc.stdout, proc.stdin, stderr=proc.stderr, log=h.logs.append
        )
        client.start()
        await client.initialize(name="environment-test", version="1")
        await client.notify("initialized")
        result = await client.request("mcpServerStatus/list", {}, timeout=30)
        rejected = any(
            row.get("name") == "test" and row.get("toolsError") for row in result.get("data", [])
        )
        assert rejected, "loopback refusal was not observed by the real client"
        assert observed and all(observed), "real client did not use the configured scoped header"
    finally:
        if client is not None:
            await client.close()
        if proc is not None and proc.returncode is None:
            proc.terminate()
            await asyncio.wait_for(proc.wait(), timeout=10)
        await server.cleanup()
        diagnostics = "\n".join(h.logs)
        print(
            json.dumps(
                {
                    "mentions_codex_home": "CODEX_HOME" in diagnostics,
                    "mentions_missing_path": (
                        "No such file" in diagnostics or "does not exist" in diagnostics
                    ),
                    "mentions_unexpected_argument": "unexpected argument" in diagnostics,
                }
            )
        )


@pytest.mark.parametrize("kind", KINDS)
def test_shared_default_home_is_not_forced_when_absent(harness, kind):
    _, build = harness.make(kind)
    absent = "CODEX_HOME" not in build()
    assert absent, "unset default home became an explicit path override"


def test_suite_scrub_removes_daemon_namespace_before_diagnostics(monkeypatch):
    from tests.conftest import _scrub_test_env

    monkeypatch.setenv("PINKYBOT_FERRY_SHARED_SECRET", SENTINEL)
    _scrub_test_env()
    assert "PINKYBOT_FERRY_SHARED_SECRET" not in os.environ


def test_parent_secret_never_reaches_environment_equality_diagnostic(tmp_path):
    import subprocess
    import sys

    probe = tmp_path / "test_environment_diff.py"
    probe.write_text(
        "import os\nfrom tests.conftest import _scrub_test_env\n"
        "_scrub_test_env()\n"
        "def test_deliberate_difference():\n"
        "    before = dict(os.environ)\n"
        "    after = dict(before, DELIBERATE_DIFFERENCE='present')\n"
        "    assert before == after\n"
    )
    private_tmp = tmp_path / "pytest-tmp"
    private_tmp.mkdir(mode=0o700)
    repo = Path(__file__).resolve().parents[1]
    env = dict(
        os.environ,
        PINKYBOT_FERRY_SHARED_SECRET=SENTINEL,
        TMPDIR=str(private_tmp),
        PYTHONPATH=str(repo / "src") + os.pathsep + str(repo),
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", str(probe), "-vv", "--tb=short"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )
    diagnostic = result.stdout + result.stderr
    assert result.returncode == 1 and "AssertionError" in diagnostic
    assert "DELIBERATE_DIFFERENCE" in diagnostic
    scan_outputs(diagnostic)


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("value", ("", "configured-home"))
def test_explicit_shared_home_keeps_empty_and_nonempty_values(harness, kind, value):
    h = harness
    h.patch.setenv("CODEX_HOME", value)
    _, build = h.make(kind)
    env = build()
    matches = "CODEX_HOME" in env and env["CODEX_HOME"] == value
    assert matches, "explicit shared home override changed"


@pytest.mark.parametrize("mode", ("off", "shadow"))
@pytest.mark.parametrize("configured", (False, True))
def test_container_shadow_resolves_target_daemon_url(harness, mode, configured):
    h = harness
    owner, build = h.make("repl")
    h.registry.mode = "container"
    h.patch.setenv("PINKY_CONTAINER_RUNTIME", "podman")
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    h.patch.setenv("PINKY_DAEMON_URL", "http://127.0.0.1:8888")
    expected = "http://container-gateway.invalid:8888"
    if configured:
        h.patch.setenv("PINKY_CONTAINER_DAEMON_URL", expected)
    else:
        expected = "http://host.containers.internal:8888"
    assert owner._container_agent() is not None
    matches = build().get("PINKY_DAEMON_URL") == expected
    assert matches, "shadow container payload retained host-loopback daemon routing"
    reports = [line for line in h.logs if line.startswith("isolated_launch_env_shadow ")]
    assert len(reports) == 1


@pytest.mark.parametrize("kind", KINDS)
@pytest.mark.parametrize("credential", ("current", "rotated", "wrong-agent"))
async def test_launch_identity_authenticates_without_global_authority(harness, kind, credential):
    from mcp import types
    from mcp.server.fastmcp import FastMCP
    from mcp.server.lowlevel.server import request_ctx
    from mcp.shared.context import RequestContext
    from starlette.requests import Request

    from pinky_daemon.auth import build_internal_auth_headers, verify_internal_request
    from pinky_daemon.shared_mcp import AgentNameMiddleware, _current_agent
    from pinky_daemon.shared_mcp_policy import install_tool_policy

    h = harness
    h.patch.setenv("PINKY_ISOLATED_POLICY_MODE", "enforce")
    for name in DAEMON_ONLY:
        h.patch.setenv(name, SENTINEL)
    owner, build = h.make(kind)
    env = build()
    authority_absent = not DAEMON_ONLY.intersection(env)
    assert authority_absent, "daemon authority reached the authentication control"
    signed = build_internal_auth_headers(
        env.get("PINKY_AGENT_KEY", ""),
        agent_name="test-agent",
        method="GET",
        path="/agents/me",
    )
    verified = verify_internal_request(
        "",
        agent_name="test-agent",
        method="GET",
        path="/agents/me",
        timestamp=signed.get("x-pinky-timestamp", ""),
        signature=signed.get("x-pinky-signature", ""),
        agent_key=h.registry.key,
        allow_global_secret=False,
    )
    assert verified, "scoped launch key did not sign a valid request"
    config = owner._agent_config if kind == "tmux_app_server" else owner._config
    header = env.get(HEADER, config.mcp_servers["test"]["headers"]["Authorization"])
    keys = {"test-agent": h.registry.key, "other-agent": SENTINEL + "other"}
    if credential == "rotated":
        keys["test-agent"] = SENTINEL + "rotated"
    caller = "other-agent" if credential == "wrong-agent" else "test-agent"
    entries, replies = [], []
    server = FastMCP("launch-scope")

    @server.tool(name="reflect")
    async def self_only() -> str:
        entries.append(_current_agent.get())
        return "recorded"

    @server.tool(name="reflect_for")
    async def cross_agent() -> str:
        entries.append("forbidden-cross-agent")
        return "recorded"

    install_tool_policy(server, "memory", h.registry, None, keys.get)

    async def dispatch(scope, receive, send):
        token = request_ctx.set(
            RequestContext(
                request_id="launch-test",
                meta=None,
                session=None,
                lifespan_context={},
                request=Request(scope),
            )
        )
        try:
            for name in ("reflect", "reflect_for"):
                result = await server._mcp_server.request_handlers[types.CallToolRequest](
                    types.CallToolRequest(
                        method="tools/call",
                        params=types.CallToolRequestParams(name=name, arguments={}),
                    )
                )
                assert result.root.isError is (name == "reflect_for")
        finally:
            request_ctx.reset(token)

    async def send(message):
        if message["type"] == "http.response.start":
            replies.append(message["status"])

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/mcp/memory/http",
        "query_string": b"",
        "headers": [(b"x-agent-name", caller.encode()), (b"authorization", header.encode())],
        "server": ("127.0.0.1", 8000),
        "client": ("127.0.0.1", 12345),
    }
    middleware = AgentNameMiddleware(dispatch, signing_key_resolver=keys.get, require_auth=True)
    await middleware(scope, None, send)
    assert entries == (["test-agent"] if credential == "current" else [])
    assert replies == ([] if credential == "current" else [401])
