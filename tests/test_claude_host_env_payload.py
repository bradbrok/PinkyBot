"""Host launch provenance uses disposable sockets and names-only child reports."""

import asyncio
import json
import os
import shlex
import shutil
import subprocess
import sys
import uuid
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.command_runner import LocalCommandRunner
from pinky_daemon.isolated_launch_env import LaunchPolicy
from pinky_daemon.streaming_session import StreamingSessionConfig
from pinky_daemon.tmux_dream_runner import TmuxDreamConfig, TmuxDreamRunner
from pinky_daemon.tmux_session import TmuxSession, _TmuxControl

SENTINEL = "private-launch-" + "q" * 48
# Independent contract inventory: do not import the implementation's unset list.
AUTH_NAMES = """
ANTHROPIC_API_KEY ANTHROPIC_AUTH_TOKEN ANTHROPIC_BASE_URL ANTHROPIC_CUSTOM_HEADERS
CLAUDE_CONFIG_DIR CLAUDE_CODE_OAUTH_TOKEN CLAUDE_CODE_OAUTH_REFRESH_TOKEN
CLAUDE_CODE_OAUTH_SCOPES CLAUDE_CODE_USE_BEDROCK CLAUDE_CODE_USE_VERTEX
CLAUDE_CODE_USE_FOUNDRY CLAUDE_CODE_USE_MANTLE CLAUDE_CODE_USE_ANTHROPIC_AWS
CLAUDE_CODE_SKIP_BEDROCK_AUTH CLAUDE_CODE_SKIP_VERTEX_AUTH CLAUDE_CODE_SKIP_FOUNDRY_AUTH
CLAUDE_CODE_SKIP_MANTLE_AUTH CLAUDE_CODE_SKIP_ANTHROPIC_AWS_AUTH
CLAUDE_CODE_PROVIDER_MANAGED_BY_HOST ANTHROPIC_BEDROCK_BASE_URL
ANTHROPIC_BEDROCK_MANTLE_BASE_URL ANTHROPIC_FOUNDRY_API_KEY ANTHROPIC_FOUNDRY_AUTH_TOKEN
ANTHROPIC_FOUNDRY_BASE_URL ANTHROPIC_FOUNDRY_RESOURCE ANTHROPIC_VERTEX_BASE_URL
ANTHROPIC_VERTEX_PROJECT_ID ANTHROPIC_AWS_API_KEY ANTHROPIC_AWS_BASE_URL
ANTHROPIC_AWS_WORKSPACE_ID ANTHROPIC_FEDERATION_RULE_ID ANTHROPIC_ORGANIZATION_ID
ANTHROPIC_WORKSPACE_ID ANTHROPIC_PROFILE AWS_ACCESS_KEY_ID AWS_SECRET_ACCESS_KEY
AWS_SESSION_TOKEN AWS_BEARER_TOKEN_BEDROCK AWS_PROFILE AWS_REGION AWS_DEFAULT_REGION
AWS_SHARED_CREDENTIALS_FILE AWS_CONFIG_FILE GOOGLE_APPLICATION_CREDENTIALS
GCLOUD_PROJECT GOOGLE_CLOUD_PROJECT CLOUD_ML_REGION
""".split()


def scan_outputs(*outputs):
    if any(SENTINEL in output for output in outputs):
        raise AssertionError("captured output contained a protected value")


@pytest.fixture
def clean_daemon(tmp_path, monkeypatch, capsys, caplog):
    for name in list(os.environ):
        monkeypatch.delenv(name)
    home = tmp_path / "daemon-home"
    home.mkdir(mode=0o700)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("PATH", os.defpath)
    monkeypatch.setenv("LANG", "C")
    yield home
    output = capsys.readouterr()
    scan_outputs(output.out, output.err, caplog.text)


def test_capture_scanner_positive_control():
    with pytest.raises(AssertionError, match="protected value"):
        scan_outputs(SENTINEL)


class Registry:
    def __init__(self, *, isolated=False, key=""):
        self.isolated, self.key = isolated, key

    def get(self, name):
        return SimpleNamespace(
            isolated=self.isolated,
            isolation_mode="local",
            tool_policy_enabled=False,
            dedicated_config_dir=False,
        )

    def get_signing_key(self, name):
        return self.key


def host_session(root, *, registry=None, control=None, provider_key="", cls=TmuxSession):
    return cls(
        StreamingSessionConfig(
            agent_name="test-agent", working_dir=str(root), provider_key=provider_key
        ),
        registry=registry or Registry(),
        tmux_control=control,
    )


@pytest.mark.parametrize("mode", ["off", "shadow", "enforce"])
def test_host_payload_contains_daemon_tool_configuration(clean_daemon, monkeypatch, mode):
    monkeypatch.setenv("PINKY_ISOLATED_ENV", mode)
    monkeypatch.setenv("CUSTOM_TOOL_TOKEN", SENTINEL)
    monkeypatch.setenv("HTTPS_PROXY", SENTINEL)
    session = host_session(clean_daemon)
    policy = session._launch_env_policy()
    assert policy.mode == mode if mode == "enforce" else not policy.clean
    assert not policy.clean
    env = session._build_repl_env()
    for name in ("CUSTOM_TOOL_TOKEN", "HTTPS_PROXY"):
        matches = env.get(name) == SENTINEL
        assert matches, name


@pytest.mark.parametrize("mode", ["off", "shadow"])
def test_explicit_provider_and_empty_oauth_win(clean_daemon, monkeypatch, mode):
    monkeypatch.setenv("PINKY_ISOLATED_ENV", mode)
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL + "ambient")
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SENTINEL + "ambient")
    session = host_session(clean_daemon, provider_key=SENTINEL + "explicit")
    monkeypatch.setattr(session, "_dedicated_config_dir", lambda: str(clean_daemon / "account"))
    env = session._build_repl_env()
    matches = env.get("ANTHROPIC_API_KEY") == SENTINEL + "explicit"
    assert matches, "ANTHROPIC_API_KEY"
    empty = env.get("CLAUDE_CODE_OAUTH_TOKEN") == ""
    assert empty, "CLAUDE_CODE_OAUTH_TOKEN"


@pytest.mark.parametrize("isolated,key", [(True, ""), (True, "scoped"), (False, "scoped")])
def test_explicit_signing_identity_is_not_replaced(clean_daemon, monkeypatch, isolated, key):
    monkeypatch.setenv("PINKY_SESSION_SECRET", SENTINEL)
    monkeypatch.setenv("PINKY_AGENT_KEY", SENTINEL + "foreign")
    session = host_session(clean_daemon, registry=Registry(isolated=isolated, key=key))
    env = session._build_repl_env()
    assert ("PINKY_SESSION_SECRET" in env) is (not isolated)
    matches = env.get("PINKY_AGENT_KEY", "") == key
    assert matches, "PINKY_AGENT_KEY"


@pytest.mark.parametrize("kind", ["clean", "nonlocal", "local_subclass"])
def test_host_overlay_excludes_clean_and_other_runners(clean_daemon, monkeypatch, kind):
    monkeypatch.setenv("CUSTOM_TOOL_TOKEN", SENTINEL)
    session = host_session(clean_daemon)
    policy = LaunchPolicy("enforce", "isolated", "scoped") if kind == "clean" else LaunchPolicy()
    if kind == "nonlocal":
        session._tmux._runner = object()
    elif kind == "local_subclass":

        class OtherRunner(LocalCommandRunner):
            pass

        session._tmux._runner = OtherRunner()
    env = session._build_repl_env(launch_policy=policy)
    assert "CUSTOM_TOOL_TOKEN" not in env


async def wait_report(path):
    for _ in range(500):
        if path.exists():
            return json.loads(path.read_text())
        await asyncio.sleep(0.01)
    raise AssertionError("inert child did not publish its names-only report")


@asynccontextmanager
async def private_server(root, monkeypatch):
    binary = shutil.which("tmux", path="/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin")
    if not binary:
        pytest.skip("tmux unavailable")
    label = "k809-" + uuid.uuid4().hex[:12]
    base = [binary, "-L", label, "-f", "/dev/null"]
    pane_home = root / "pane-home"
    pane_home.mkdir(mode=0o700)
    pane_bin = root / "pane-bin"
    pane_bin.mkdir()
    # Model the shell startup PATH additions used by host panes.
    shell = root / "pane-shell"
    shell.write_text(
        "#!/bin/sh\n"
        + "export PATH="
        + shlex.quote(str(pane_bin))
        + ':"$PATH"\n'
        + 'exec /bin/sh "$@"\n'
    )
    shell.chmod(0o700)
    report = root / "child-names.json"
    script = pane_bin / "claude"
    code = (
        "import json,os,pathlib,time\n"
        f"names={AUTH_NAMES!r}\n"
        f"marker={SENTINEL!r}\n"
        "result={name:{'present':name in os.environ,'daemon':os.environ.get(name)==marker+'daemon',"
        "'server':os.environ.get(name)==marker+'server','empty':os.environ.get(name)==''} for name in names}\n"
        f"result['PATH']={{'pane':{str(pane_bin)!r} in os.environ.get('PATH','').split(os.pathsep)}}\n"
        f"result['HOME']={{'pane':os.environ.get('HOME')=={str(pane_home)!r}}}\n"
        "result['TERM']={'pane':os.environ.get('TERM')!='dumb'}\n"
        "result['LANG']={'pane':os.environ.get('LANG')=='C'}\n"
        "result['LC_TIME']={'pane':os.environ.get('LC_TIME')=='C'}\n"
        f"result['TMUX']={{'pane':{label!r} in os.environ.get('TMUX','')}}\n"
        "result['TMUX_CUSTOM_MARKER']={'pane':os.environ.get('TMUX_CUSTOM_MARKER')=='server-marker'}\n"
        "result['TMUX_PANE']={'pane':os.environ.get('TMUX_PANE','').startswith('%')}\n"
        "result['PINKYBOT_FERRY_SHARED_SECRET']={'present':'PINKYBOT_FERRY_SHARED_SECRET' in os.environ}\n"
        "result['CUSTOM_TOOL_TOKEN']={'daemon':os.environ.get('CUSTOM_TOOL_TOKEN')==marker+'daemon'}\n"
        f"pathlib.Path({str(report)!r}).write_text(json.dumps(result))\n"
        "time.sleep(60)\n"
    )
    script.write_text(f"#!{sys.executable}\n" + code)
    script.chmod(0o700)
    seed = {
        "HOME": str(pane_home),
        "PATH": str(pane_bin) + os.pathsep + os.defpath,
        "SHELL": str(shell),
        "LANG": "C",
        "LC_TIME": "C",
        "TERM": "xterm",
        "TMUX_CUSTOM_MARKER": "server-marker",
        **{name: SENTINEL + "server" for name in AUTH_NAMES},
    }
    control = _TmuxControl("probe", tmux_binary=binary, socket_name=label)
    monkeypatch.setattr(control, "_base_cmd", lambda: base)
    commands = []
    outputs = []
    real_run = control._run

    async def record(*args, **kwargs):
        commands.append(shlex.join(args))
        result = await real_run(*args, **kwargs)
        outputs.extend((result.stdout, result.stderr))
        return result

    monkeypatch.setattr(control, "_run", record)
    try:
        start = subprocess.run(
            [*base, "new-session", "-d", "-s", "seed", "sleep 120"],
            env=seed,
            capture_output=True,
            timeout=5,
        )
        assert start.returncode == 0, "private tmux seed failed"
        yield SimpleNamespace(control=control, script=script, report=report, commands=commands)
    finally:
        end = subprocess.run(
            [*base, "kill-server"],
            capture_output=True,
            timeout=5,
            env={"HOME": str(pane_home), "PATH": os.defpath},
        )
        scan_outputs(
            *commands,
            *outputs,
            start.stdout.decode(errors="replace"),
            start.stderr.decode(errors="replace"),
            end.stdout.decode(errors="replace"),
            end.stderr.decode(errors="replace"),
        )


async def launch_host(root, probe, monkeypatch, *, cls=TmuxSession, provider_url=""):
    session = host_session(root, control=probe.control, cls=cls)
    session._config.provider_url = provider_url
    for name in (
        "_ensure_container_started",
        "_reap_retained_spawn_cleanup_debt",
        "_seed_container_trust",
        "_seed_container_home_creds",
        "_stop_tailer",
        "_start_tailer",
    ):
        monkeypatch.setattr(session, name, AsyncMock())
    monkeypatch.setattr(session, "_container_agent", lambda **kwargs: None)
    monkeypatch.setattr(session, "_select_command_runner", lambda *args: probe.control._runner)
    monkeypatch.setattr(session, "_prepare_tmux_spawn", lambda: None)
    monkeypatch.setattr(session, "_spawn_cleanup_state_dir", lambda: root)
    monkeypatch.setattr(session, "_build_claude_cmd", lambda: shlex.quote(str(probe.script)))
    monkeypatch.setattr(tmux_session, "_seed_claude_trust_file", lambda *a: False)
    monkeypatch.setattr(tmux_session, "_POST_SPAWN_LIVENESS_DELAY_SEC", 0.01)
    await session._spawn_tmux_repl()
    return await wait_report(probe.report)


async def launch_dream(root, probe, monkeypatch, *, binary=None):
    runner = TmuxDreamRunner(
        TmuxDreamConfig(
            working_dir=str(root),
            claude_binary=binary or str(probe.script),
            poll_interval_s=0.01,
        ),
        agent_name="test-agent",
    )
    probe.control.session_name = runner.session_name
    runner._control = probe.control

    async def private_tmux(*args, **kwargs):
        result = await probe.control._run(*args, **kwargs)
        return result.returncode, result.stdout + result.stderr

    monkeypatch.setattr(runner, "_tmux", private_tmux)
    monkeypatch.setattr(runner, "_seed_trust", lambda *a: False)
    monkeypatch.setattr(runner, "_wait_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(runner, "_ensure_submitted", AsyncMock())
    captured = []

    async def result(*args, **kwargs):
        captured.append(await wait_report(probe.report))
        return "completed"

    monkeypatch.setattr(runner, "_wait_for_result", result)
    outcome = await runner.run("synthetic prompt")
    assert outcome.ok, "dream did not complete with inert child"
    return captured[0]


async def test_private_server_positive_control(clean_daemon, tmp_path, monkeypatch):
    async with private_server(tmp_path, monkeypatch) as probe:
        result = await probe.control._run(
            "new-session",
            "-d",
            "-s",
            "probe",
            str(probe.script),
        )
        assert result.ok
        report = await wait_report(probe.report)
        assert report["ANTHROPIC_API_KEY"]["server"]


@pytest.mark.parametrize("kind", ["host", "dream"])
@pytest.mark.parametrize("auth", ["absent", "configured", "empty"])
async def test_child_auth_is_daemon_owned(clean_daemon, tmp_path, monkeypatch, kind, auth):
    async with private_server(tmp_path, monkeypatch) as probe:
        if auth != "absent":
            for name in AUTH_NAMES:
                monkeypatch.setenv(name, "" if auth == "empty" else SENTINEL + "daemon")
        launch = launch_host if kind == "host" else launch_dream
        report = await launch(tmp_path, probe, monkeypatch)
        for name in AUTH_NAMES:
            withheld = name == "CLAUDE_CODE_OAUTH_TOKEN" or (
                kind == "host" and name == "CLAUDE_CONFIG_DIR"
            )
            assert report[name]["present"] is (auth != "absent" and not withheld), name
            if withheld:
                continue
            if auth == "configured":
                assert report[name]["daemon"], name
            elif auth == "empty":
                assert report[name]["empty"], name


async def test_host_payload_preserves_pane_path_and_terminal(clean_daemon, tmp_path, monkeypatch):
    async with private_server(tmp_path, monkeypatch) as probe:
        monkeypatch.setenv("TERM", "dumb")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_TIME", "en_US.UTF-8")
        monkeypatch.setenv("TMUX", "unrelated-routing-metadata")
        monkeypatch.setenv("TMUX_PANE", "unrelated-pane")
        monkeypatch.setenv("TMUX_CUSTOM_MARKER", "daemon-marker")
        monkeypatch.setenv("CUSTOM_TOOL_TOKEN", SENTINEL + "daemon")
        report = await launch_host(tmp_path, probe, monkeypatch)
        for name in (
            "PATH",
            "HOME",
            "TERM",
            "LANG",
            "LC_TIME",
            "TMUX",
            "TMUX_PANE",
            "TMUX_CUSTOM_MARKER",
        ):
            assert report[name]["pane"], name


def test_shared_auth_inventory_contains_original_names():
    original = {
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_OAUTH_TOKEN",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "ANTHROPIC_CUSTOM_HEADERS",
    }
    assert original <= tmux_session._CLAUDE_AUTH_ENV_NAMES


async def test_dream_loader_keeps_shell_path_and_resolves_binary(
    clean_daemon, tmp_path, monkeypatch
):
    async with private_server(tmp_path, monkeypatch) as probe:
        monkeypatch.setenv("TERM", "dumb")
        monkeypatch.setenv("LANG", "en_US.UTF-8")
        monkeypatch.setenv("LC_TIME", "en_US.UTF-8")
        monkeypatch.setenv("TMUX", "unrelated-routing-metadata")
        monkeypatch.setenv("TMUX_PANE", "unrelated-pane")
        monkeypatch.setenv("TMUX_CUSTOM_MARKER", "daemon-marker")
        monkeypatch.setenv("CUSTOM_TOOL_TOKEN", SENTINEL + "daemon")
        # Only the host shell's added directory contains this inert binary.
        assert shutil.which("claude") is None
        report = await launch_dream(tmp_path, probe, monkeypatch, binary="claude")
        for name in (
            "PATH",
            "HOME",
            "TERM",
            "LANG",
            "LC_TIME",
            "TMUX",
            "TMUX_PANE",
            "TMUX_CUSTOM_MARKER",
        ):
            assert report[name]["pane"], name
        assert report["CUSTOM_TOOL_TOKEN"]["daemon"]


async def test_codex_launch_retains_existing_auth_inheritance(clean_daemon, tmp_path, monkeypatch):
    async with private_server(tmp_path, monkeypatch) as probe:
        report = await launch_host(tmp_path, probe, monkeypatch, cls=CodexTmuxSession)
        assert report["ANTHROPIC_API_KEY"]["server"]


BUILDER_OWNED_NAMES = {
    "ANTHROPIC_API_KEY",
    "ANTHROPIC_AUTH_TOKEN",
    "ANTHROPIC_BASE_URL",
    "CLAUDE_CONFIG_DIR",
    "CLAUDE_CODE_OAUTH_TOKEN",
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


@pytest.mark.parametrize("name", sorted(BUILDER_OWNED_NAMES))
def test_overlay_cannot_fill_an_omitted_builder_name(clean_daemon, monkeypatch, name):
    monkeypatch.setenv(name, SENTINEL)
    env = tmux_session._claude_host_payload({})
    present = name in env
    assert not present, name


@pytest.mark.parametrize("kind", ["host", "dream"])
async def test_ferry_secret_is_not_newly_forwarded(clean_daemon, tmp_path, monkeypatch, kind):
    async with private_server(tmp_path, monkeypatch) as probe:
        monkeypatch.setenv("PINKYBOT_FERRY_SHARED_SECRET", SENTINEL)
        launch = launch_host if kind == "host" else launch_dream
        report = await launch(tmp_path, probe, monkeypatch)
        assert not report["PINKYBOT_FERRY_SHARED_SECRET"]["present"]


@pytest.mark.parametrize("args", [["has-session"], ["-L", "default", "has-session"]])
def test_default_tmux_socket_tripwire(args):
    with pytest.raises(RuntimeError, match="explicit private socket"):
        subprocess.run(["tmux", *args], check=False)


async def test_default_control_socket_tripwire():
    with pytest.raises(RuntimeError, match="explicit private socket"):
        await _TmuxControl("test-agent")._run("has-session")


@pytest.mark.parametrize("condition", ["custom_provider", "forwarding_off", "no_dedicated_config"])
async def test_builder_withheld_auth_is_absent_from_child(
    clean_daemon, tmp_path, monkeypatch, condition
):
    async with private_server(tmp_path, monkeypatch) as probe:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SENTINEL + "daemon")
        monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(clean_daemon / "account"))
        monkeypatch.setenv(
            "PINKY_FORWARD_OAUTH_TOKEN", "1" if condition == "custom_provider" else "0"
        )
        report = await launch_host(
            tmp_path,
            probe,
            monkeypatch,
            provider_url="https://provider.example" if condition == "custom_provider" else "",
        )
        name = (
            "CLAUDE_CONFIG_DIR" if condition == "no_dedicated_config" else "CLAUDE_CODE_OAUTH_TOKEN"
        )
        assert not report[name]["present"], name


async def test_default_provider_keeps_enabled_static_oauth(clean_daemon, tmp_path, monkeypatch):
    async with private_server(tmp_path, monkeypatch) as probe:
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SENTINEL + "daemon")
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
        report = await launch_host(tmp_path, probe, monkeypatch)
        assert report["CLAUDE_CODE_OAUTH_TOKEN"]["daemon"]


def test_explicit_tool_configuration_wins_over_ambient(clean_daemon, monkeypatch):
    monkeypatch.setenv("CUSTOM_TOOL_TOKEN", SENTINEL + "ambient")
    env = tmux_session._claude_host_payload({"CUSTOM_TOOL_TOKEN": SENTINEL + "explicit"})
    matches = env.get("CUSTOM_TOOL_TOKEN") == SENTINEL + "explicit"
    assert matches, "CUSTOM_TOOL_TOKEN"


@pytest.mark.parametrize("kind", ["host", "dream"])
@pytest.mark.parametrize("forward", [False, True])
async def test_static_oauth_intent_controls_api_billing(
    clean_daemon, tmp_path, monkeypatch, kind, forward
):
    async with private_server(tmp_path, monkeypatch) as probe:
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1" if forward else "0")
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.setenv(name, SENTINEL + "daemon")
        launch = launch_host if kind == "host" else launch_dream
        report = await launch(tmp_path, probe, monkeypatch)
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            assert report[name]["present"] is (not forward), name
            if not forward:
                assert report[name]["daemon"], name
        assert report["CLAUDE_CODE_OAUTH_TOKEN"]["present"] is forward
        if forward:
            assert report["CLAUDE_CODE_OAUTH_TOKEN"]["daemon"]


@pytest.mark.parametrize("kind", ["host", "dream"])
@pytest.mark.parametrize(
    "selector",
    [
        "ANTHROPIC_BASE_URL",
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    ],
)
async def test_daemon_provider_route_withholds_subscription_token(
    clean_daemon, tmp_path, monkeypatch, kind, selector
):
    async with private_server(tmp_path, monkeypatch) as probe:
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
        monkeypatch.setenv(
            selector, "https://provider.example" if selector == "ANTHROPIC_BASE_URL" else "1"
        )
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "CLAUDE_CODE_OAUTH_TOKEN"):
            monkeypatch.setenv(name, SENTINEL + "daemon")
        launch = launch_host if kind == "host" else launch_dream
        report = await launch(tmp_path, probe, monkeypatch)
        assert not report["CLAUDE_CODE_OAUTH_TOKEN"]["present"]
        for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
            assert report[name]["daemon"], name


def test_false_provider_switches_keep_subscription_intent(clean_daemon, monkeypatch):
    monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
    monkeypatch.setenv("ANTHROPIC_API_KEY", SENTINEL)
    monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", SENTINEL)
    for name in (
        "CLAUDE_CODE_USE_BEDROCK",
        "CLAUDE_CODE_USE_VERTEX",
        "CLAUDE_CODE_USE_FOUNDRY",
        "CLAUDE_CODE_USE_MANTLE",
        "CLAUDE_CODE_USE_ANTHROPIC_AWS",
    ):
        monkeypatch.setenv(name, "0")
    env = tmux_session._claude_host_auth_env()
    key_present = "ANTHROPIC_API_KEY" in env
    token_matches = env.get("CLAUDE_CODE_OAUTH_TOKEN") == SENTINEL
    assert not key_present
    assert token_matches


async def test_empty_static_token_is_withheld_by_host_builder(clean_daemon, tmp_path, monkeypatch):
    async with private_server(tmp_path, monkeypatch) as probe:
        monkeypatch.setenv("PINKY_FORWARD_OAUTH_TOKEN", "1")
        monkeypatch.setenv("CLAUDE_CODE_OAUTH_TOKEN", "")
        report = await launch_host(tmp_path, probe, monkeypatch)
        assert not report["CLAUDE_CODE_OAUTH_TOKEN"]["present"]
