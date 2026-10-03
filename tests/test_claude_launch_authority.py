"""Claude authority controls use synthetic inputs and names/boolean reports only."""

import json
import os
import shlex
import sys
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import isolated_launch_env, tmux_session
from pinky_daemon.auth import build_internal_auth_headers, verify_internal_request
from pinky_daemon.tmux_launch_env_loader import BASE_ALLOWLIST, DAEMON_ONLY
from pinky_daemon.tmux_session import TmuxCommandResult
from tests import test_claude_host_env_payload as support
from tests.test_claude_host_env_payload import AUTH_NAMES, SENTINEL, host_session, scan_outputs
from tests.test_claude_host_env_payload import clean_daemon as clean_daemon

HEADER = "PINKY_MCP_HDR_FOREIGN_AUTHORIZATION"
OWNED = set(AUTH_NAMES) | {
    "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION",
    "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
    "CLAUDE_CODE_AUTO_COMPACT_WINDOW",
    "PINKY_AGENT_NAME",
    "PINKY_AGENT_KEY",
    "PINKY_DAEMON_URL",
    "PINKY_CONTAINER_DAEMON_URL",
    "PINKY_EXPECTED_EFFORT",
    "PINKY_STRICT_EFFORT",
    "PINKY_TOOL_POLICY",
    "PINKY_SESSION_SECRET",
    "PINKY_TMUX_TRANSCRIPT_BIND",
}
WITHHELD = {"PINKY_AGENT_KEY", "PINKY_STRICT_EFFORT", "CLAUDE_CODE_AUTO_COMPACT_WINDOW"}


class Registry(support.Registry):
    def __init__(self, status="not_isolated", key=SENTINEL + "own", policy_enabled=True):
        super().__init__(isolated=status == "isolated", key=key)
        self.status = status
        self.policy_enabled = policy_enabled

    def get(self, name):
        if self.status == "unknown":
            raise LookupError("synthetic registry unavailable")
        agent = super().get(name)
        agent.tool_policy_enabled = self.policy_enabled
        return agent


@pytest.fixture
def authority(clean_daemon, monkeypatch):
    logs = []
    monkeypatch.setattr(tmux_session, "_log", logs.append)
    h = SimpleNamespace(root=clean_daemon, patch=monkeypatch, registry=Registry(), logs=logs)
    yield h
    scan_outputs(*logs)


def build(h, kind):
    if kind == "dream":
        return tmux_session._claude_host_payload(tmux_session._claude_host_auth_env())
    return host_session(h.root, registry=h.registry)._build_repl_env()


@pytest.mark.parametrize("kind", ("host", "dream"))
@pytest.mark.parametrize("mode", ("off", "shadow", "enforce"))
def test_daemon_authority_is_absent_from_every_nonclean_payload(authority, kind, mode):
    h = authority
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    for name in DAEMON_ONLY:
        h.patch.setenv(name, SENTINEL)
    absent = not DAEMON_ONLY.intersection(build(h, kind))
    assert absent, "daemon authority entered non-clean Claude payload"


@pytest.mark.parametrize("kind", ("host", "dream"))
def test_foreign_header_is_not_forwarded_from_daemon(authority, kind):
    authority.patch.setenv(HEADER, SENTINEL)
    absent = HEADER not in build(authority, kind)
    assert absent, "foreign MCP header entered Claude payload"


@pytest.mark.parametrize("status", ("isolated", "not_isolated", "unknown"))
@pytest.mark.parametrize("has_key", (False, True))
def test_missing_or_uncertain_identity_never_borrows_global_authority(authority, status, has_key):
    h = authority
    h.registry.status = status
    h.registry.isolated = status == "isolated"
    h.registry.key = SENTINEL + "own" if has_key else ""
    for name in DAEMON_ONLY | {"PINKY_AGENT_KEY"}:
        h.patch.setenv(name, SENTINEL + "foreign")
    env = build(h, "host")
    authority_absent = not DAEMON_ONLY.intersection(env)
    scoped_identity = (
        env.get("PINKY_AGENT_KEY") == h.registry.key if has_key else "PINKY_AGENT_KEY" not in env
    )
    assert authority_absent and scoped_identity, "uncertain identity borrowed ambient authority"


@pytest.mark.parametrize("name", sorted(DAEMON_ONLY | {HEADER}))
def test_explicit_candidate_cannot_reintroduce_forbidden_authority(authority, name):
    env = tmux_session._claude_host_payload({name: SENTINEL})
    absent = name not in env
    assert absent, "explicit candidate reintroduced forbidden authority"


@pytest.mark.parametrize("kind", ("host", "dream"))
@pytest.mark.parametrize("name", ("PINKY_DAEMON_URL", "PINKY_TOOL_POLICY"))
@pytest.mark.parametrize("empty", (False, True))
def test_daemon_owned_controls_remain_explicit(authority, kind, name, empty):
    h = authority
    value = "" if empty else SENTINEL + "daemon"
    h.patch.setenv(name, value)
    env = build(h, kind)
    matches = name in env and env[name] == value
    assert matches, "daemon-owned route or policy was discarded"


def test_agent_policy_override_still_wins_over_daemon_input(authority):
    h = authority
    h.registry.policy_enabled = False
    h.patch.setenv("PINKY_TOOL_POLICY", "enforce")
    disabled = build(h, "host").get("PINKY_TOOL_POLICY") == "off"
    assert disabled, "daemon forwarding overrode the agent policy decision"


@pytest.mark.parametrize("name", ("PINKY_DAEMON_URL", "PINKY_TOOL_POLICY"))
def test_resolved_control_beats_ambient_candidate(authority, name):
    authority.patch.setenv(name, SENTINEL + "daemon")
    env = tmux_session._claude_host_payload({name: ""})
    empty_preserved = name in env and env[name] == ""
    assert empty_preserved, "intentional explicit empty control was replaced"


@pytest.mark.parametrize("present", (False, True))
def test_names_only_wrapper_removes_all_withheld_builder_names(authority, present):
    env = {name: "" for name in OWNED - DAEMON_ONLY} if present else {}
    # Daemon-only authority is removed even if a candidate mistakenly includes it.
    env.update({name: SENTINEL for name in DAEMON_ONLY})
    command = tmux_session._claude_host_command("/bin/true", env)
    argv = shlex.split(command)
    unset = {argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "-u"}
    expected = DAEMON_ONLY | (OWNED - env.keys())
    exact = unset == expected
    safe = SENTINEL not in command
    assert exact, "names-only wrapper missed or removed an owned output name"
    assert safe, "wrapper contains a value"


@asynccontextmanager
async def probe_at(h, root, source, names):
    if source == "daemon":
        for name in names:
            h.patch.setenv(name, SENTINEL + "daemon")
    if source == "server":
        original_run = support.subprocess.run

        def seeded(command, *args, **kwargs):
            if "new-session" in command and "seed" in command:
                kwargs["env"] = {
                    **kwargs["env"],
                    **{name: SENTINEL + "server" for name in names},
                }
            return original_run(command, *args, **kwargs)

        h.patch.setattr(support.subprocess, "run", seeded)
    async with support.private_server(root, h.patch) as probe:
        if source == "shell":
            shell = root / "pane-shell"
            insertion = "".join(
                "export " + name + "=" + shlex.quote(SENTINEL + "shell") + "\n"
                for name in sorted(names)
            )
            shell.write_text(
                shell.read_text().replace('exec /bin/sh "$@"', insertion + 'exec /bin/sh "$@"')
            )
        probe.script.write_text(
            f"#!{sys.executable}\n"
            "import json,os,pathlib,time\n"
            f"names={sorted(names)!r}\nmarker={SENTINEL!r}\n"
            "result={name:{'present':name in os.environ,"
            "'daemon':os.environ.get(name)==marker+'daemon',"
            "'server':os.environ.get(name)==marker+'server',"
            "'shell':os.environ.get(name)==marker+'shell',"
            "'empty':os.environ.get(name)==''} for name in names}\n"
            f"target=pathlib.Path({str(probe.report)!r})\n"
            "staged=target.with_suffix('.pending')\n"
            "staged.write_text(json.dumps(result))\nstaged.replace(target)\n"
            "time.sleep(60)\n"
        )
        if source in {"server", "shell"}:
            positive = await probe.control._run(
                "new-window", "-t", "=seed", shlex.quote(str(probe.script))
            )
            assert positive.ok
            report = await support.wait_report(probe.report)
            planted = all(report[name][source] for name in names)
            assert planted, "private inheritance positive control was not planted"
            probe.report.unlink()
        yield probe


async def launch(h, root, probe, kind):
    if kind == "host":
        h.patch.setattr(support, "Registry", lambda: h.registry)
        return await support.launch_host(root, probe, h.patch)
    return await support.launch_dream(root, probe, h.patch)


@pytest.mark.parametrize("kind", ("host", "dream"))
@pytest.mark.parametrize("source", ("daemon", "server", "shell"))
@pytest.mark.parametrize("family", ("daemon_only", "foreign_header", "withheld_owned"))
async def test_real_child_drops_unowned_authority(authority, tmp_path, kind, source, family):
    h = authority
    h.registry.key = ""
    names = {
        "daemon_only": DAEMON_ONLY,
        "foreign_header": {HEADER},
        "withheld_owned": WITHHELD,
    }[family]
    async with probe_at(h, tmp_path, source, names) as probe:
        report = await launch(h, tmp_path, probe, kind)
        absent = all(not report[name]["present"] for name in names)
        assert absent, "inert Claude child retained forbidden or withheld authority"


@pytest.mark.parametrize("kind", ("host", "dream"))
@pytest.mark.parametrize("name", ("PINKY_DAEMON_URL", "PINKY_TOOL_POLICY"))
@pytest.mark.parametrize("daemon", (False, True))
async def test_real_child_control_provenance(authority, tmp_path, kind, name, daemon):
    h = authority
    async with probe_at(h, tmp_path, "server", {name}) as probe:
        if daemon:
            h.patch.setenv(name, SENTINEL + "daemon")
        report = await launch(h, tmp_path, probe, kind)
        if daemon:
            assert report[name]["daemon"], "daemon control was lost or replaced by server state"
        elif kind == "host" and name == "PINKY_TOOL_POLICY":
            assert not report[name]["server"], "stale server policy overrode host policy decision"
        else:
            assert not report[name]["present"], (
                "server-only control survived without explicit ownership"
            )


@pytest.mark.parametrize("mode", (None, "off", "shadow"))
@pytest.mark.parametrize("status", ("isolated", "not_isolated", "unknown"))
@pytest.mark.parametrize("has_key", (False, True))
def test_host_minimum_shadow_reports_exact_candidate(authority, mode, status, has_key):
    h = authority
    h.registry.status = status
    h.registry.isolated = status == "isolated"
    h.registry.key = SENTINEL + "own" if has_key else ""
    if mode is not None:
        h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    for name in (*DAEMON_ONLY, HEADER, "ORDINARY_TOOL_CONFIG", "TMUX"):
        h.patch.setenv(name, SENTINEL)
    h.patch.setenv("MULTILINE_TOOL_CONFIG", SENTINEL + "\nsecond line")
    env = build(h, "host")
    retained = env.get("ORDINARY_TOOL_CONFIG") == SENTINEL
    assert retained, "minimum shadow enforced ordinary credential removal"
    prefix = "isolated_launch_env_shadow "
    reports = [json.loads(line[len(prefix) :]) for line in h.logs if line.startswith(prefix)]
    isolated = status == "isolated" or (status == "unknown" and has_key)
    assert len(reports) == int(isolated), (
        "isolated host did not emit exactly one minimum-shadow report"
    )
    if reports:
        explicit = {
            "PINKY_AGENT_NAME",
            "PINKY_AGENT_KEY",
            "PINKY_TOOL_POLICY",
            "PINKY_EXPECTED_EFFORT",
            "PINKY_TMUX_TRANSCRIPT_BIND",
            "CLAUDE_CODE_MAX_CONCURRENT_SUBAGENTS",
            "CLAUDE_CODE_ENABLE_PROMPT_SUGGESTION",
        }
        expected = sorted(
            name
            for name in os.environ
            if name not in BASE_ALLOWLIST | explicit and not name.startswith(("LC_", "XDG_"))
        )
        exact = reports[0]["would_drop_names"] == expected
        assert exact, "minimum-shadow names do not match the scoped daemon candidate"
        assert reports[0]["would_drop_count"] == len(expected)
        assert reports[0]["enforced"] is False
        assert reports[0]["source"] == "daemon"


@pytest.mark.parametrize("mode", ("off", "shadow", "enforce", "invalid-mode"))
def test_host_scoped_signing_remains_available_without_global_authority(authority, mode):
    h = authority
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    for name in DAEMON_ONLY:
        h.patch.setenv(name, SENTINEL)
    env = build(h, "host")
    signed = build_internal_auth_headers(
        env.get("PINKY_AGENT_KEY", ""), agent_name="test-agent", method="GET", path="/agents/me"
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
    authority_absent = not DAEMON_ONLY.intersection(env)
    assert verified, "scoped host key no longer authenticates"
    assert authority_absent, "scoped signing still exposes global authority"


@pytest.mark.parametrize("mode", ("off", "shadow"))
async def test_host_captures_policy_once_per_launch(authority, mode):
    h = authority
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    h.registry.status = "isolated"
    h.registry.isolated = True
    owner = host_session(h.root, registry=h.registry)
    snapshots, delivered = [], []
    real_capture = isolated_launch_env.capture_policy

    def capture_then_rotate(**kwargs):
        policy = real_capture(**kwargs)
        snapshots.append(policy)
        h.registry.key = SENTINEL + "rotated-" + str(len(snapshots))
        return policy

    async def spawn(**kwargs):
        delivered.append(kwargs["env"])
        return TmuxCommandResult(returncode=0, stdout="", stderr="")

    h.patch.setattr(isolated_launch_env, "capture_policy", capture_then_rotate)
    h.patch.setattr(owner._tmux, "new_session", spawn)
    h.patch.setattr(owner._tmux, "has_session", AsyncMock(side_effect=[False, True] * 2))
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
    h.patch.setattr(owner, "_select_command_runner", lambda *args: owner._tmux._runner)
    h.patch.setattr(owner, "_prepare_tmux_spawn", lambda: None)
    h.patch.setattr(owner, "_build_claude_cmd", lambda: "/bin/true")
    h.patch.setattr(tmux_session, "_seed_claude_trust_file", lambda *args: False)
    h.patch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0)
    for index in range(2):
        expected_key = h.registry.key
        await owner._spawn_tmux_repl()
        assert len(snapshots) == index + 1, "host recaptured policy during one launch"
        same_key = delivered[index].get("PINKY_AGENT_KEY") == expected_key
        assert same_key, "host payload identity diverged from its launch snapshot"
    reports = [line for line in h.logs if line.startswith("isolated_launch_env_shadow ")]
    assert len(reports) == 2, "host preflight duplicated or lost the final shadow report"


@pytest.mark.parametrize("mode", ("off", "shadow", "enforce"))
async def test_dream_does_not_gain_policy_wiring_or_identity(authority, tmp_path, mode):
    h = authority
    h.patch.setenv("PINKY_ISOLATED_ENV", mode)
    h.patch.setenv("PINKY_ISOLATED_ENV_GRANTS_FILE", "/synthetic-missing-grants")

    def unexpected_policy(**kwargs):
        raise AssertionError("dream unexpectedly captured an isolated launch policy")

    h.patch.setattr(isolated_launch_env, "capture_policy", unexpected_policy)
    async with probe_at(h, tmp_path, "daemon", {"PINKY_AGENT_KEY"}) as probe:
        report = await launch(h, tmp_path, probe, "dream")
        assert not report["PINKY_AGENT_KEY"]["present"], "dream gained a parent signing identity"
    assert not any(line.startswith("isolated_launch_env_shadow ") for line in h.logs)


@pytest.mark.parametrize("kind", ("host", "dream"))
async def test_empty_daemon_route_survives_without_foreign_headers(authority, tmp_path, kind):
    h = authority
    async with probe_at(h, tmp_path, "server", {"PINKY_DAEMON_URL", HEADER}) as probe:
        h.patch.setenv("PINKY_DAEMON_URL", "")
        report = await launch(h, tmp_path, probe, kind)
        assert report["PINKY_DAEMON_URL"]["empty"], "empty daemon route was filled by the server"
        assert not report[HEADER]["present"], "foreign header survived an empty explicit route"
