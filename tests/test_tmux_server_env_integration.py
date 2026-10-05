"""Owned real-server contracts. Safety gates fail before exec on the RED base.

Each child is an inert test executable. Values stay in memory; retained reports
contain only names or booleans. Routing is separately exercised through all four
production constructors in test_tmux_server_routes.
"""

import asyncio
import json
import os
import shlex
import subprocess
import sys
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock

import pytest

from pinky_daemon.command_runner import LocalCommandRunner
from pinky_daemon.isolated_launch_env import LaunchEnvError
from pinky_daemon.tmux_dream_runner import TmuxDreamConfig, TmuxDreamRunner
from tests.tmux_isolated_env_support import LaunchProbe, Registry
from tests.tmux_server_env_support import CANARY, allowed, control, no_values, owner
from tests.tmux_socket_support import private_labels


@asynccontextmanager
async def managed_probe(tmp_path, monkeypatch):
    probe = LaunchProbe(tmp_path, monkeypatch, start_server=False)
    original_run = LocalCommandRunner.run
    observed = []
    try:
        monkeypatch.setenv("PINKY_TMUX_SOCKET", "test-managed")
        monkeypatch.setenv("PINKY_TMUX_PANE_PATH", str(probe.bin) + ":/usr/bin:/bin")
        probe.control = control(owner("claude", tmp_path))
        # Pin the fixture-owned -S route. This is containment only, not a fake
        # verifier/base environment; the production manager remains attached.
        probe.control.socket_path = str(probe.socket)
        probe.control.tmux_binary = probe.tmux
        cmd = probe.control._base_cmd()
        assert "-f" in cmd and cmd[cmd.index("-f") + 1] == "/dev/null", "managed cold-start config safety gate absent"

        async def recorded(self, argv, **kwargs):
            if os.path.basename(argv[0]) == "tmux":
                env = kwargs.get("env")
                observed.append(set(env) if isinstance(env, dict) else None)
                no_values(argv)
            return await original_run(self, argv, **kwargs)

        monkeypatch.setattr(LocalCommandRunner, "run", recorded)
        probe.client_names = observed
        yield probe
    finally:
        await probe.close()


def globals_names(probe, *, hidden=False):
    result = subprocess.run([probe.tmux, "-f", "/dev/null", "-S", str(probe.socket),
                             "show-environment", "-g", *(["-h"] if hidden else [])],
                            env=probe.seed, capture_output=True, timeout=5)
    assert result.returncode == 0, "private global environment read failed"
    return {line.split("=", 1)[0].removeprefix("-") for line in result.stdout.decode("utf-8").splitlines() if line}


@pytest.mark.parametrize("kind", ["claude", "codex", "app_server"])
@pytest.mark.parametrize("clean", [True, False])
async def test_daemon_cold_start_globals_and_child_authority(tmp_path, monkeypatch, kind, clean):
    async with managed_probe(tmp_path, monkeypatch) as probe:
        if clean:
            grants = tmp_path / "grants.json"
            grants.write_text("{}")
            grants.chmod(0o600)
            monkeypatch.setenv("PINKY_ISOLATED_ENV", "enforce")
            monkeypatch.setenv("PINKY_ISOLATED_ENV_GRANTS_FILE", str(grants))
        names = await probe.launch(kind, registry=Registry("isolated" if clean else "not_isolated"))
        assert not {"PINKY_SESSION_SECRET", "PINKYBOT_FERRY_SHARED_SECRET"} & names
        assert "EXPLICIT_ALLOWED_NAME" in names and "CLAUDE_CODE_OAUTH_TOKEN" in names
        assert ("HRPOS_PASSWORD" in names) is (not clean)
        assert allowed(globals_names(probe), globals=True)
        assert allowed(globals_names(probe, hidden=True), globals=True)
        assert probe.client_names and all(n is not None and allowed(n) for n in probe.client_names)


async def test_fake_home_tmux_config_is_ignored(tmp_path, monkeypatch):
    async with managed_probe(tmp_path, monkeypatch) as probe:
        (probe.home / ".tmux.conf").write_text("set-environment -g CONFIG_CANARY synthetic\nset-option -g default-shell /nonexistent/shell\n")
        names = await probe.launch("claude", registry=Registry("not_isolated"))
        assert "CONFIG_CANARY" not in names | globals_names(probe)


async def test_claude_trust_seed_uses_pane_home(tmp_path, monkeypatch):
    async with managed_probe(tmp_path, monkeypatch) as probe:
        await probe.launch("claude", registry=Registry("not_isolated"))
        assert json.loads(probe.base_path.read_text())["home_matches"]
        trust = json.loads((probe.home / ".claude.json").read_text())
        assert trust["projects"][str(tmp_path.resolve())]["hasTrustDialogAccepted"]


async def test_dream_cold_start_uses_managed_globals_and_raw_ancillary_route(tmp_path, monkeypatch):
    async with managed_probe(tmp_path, monkeypatch) as probe:
        dream = TmuxDreamRunner(TmuxDreamConfig(working_dir=str(tmp_path),
            claude_binary=str(probe.bin / "claude"), poll_interval_s=0.01), agent_name="test-agent")
        probe.control.session_name = dream.session_name
        dream._control = probe.control
        monkeypatch.setattr(dream, "_wait_ready", AsyncMock(return_value=True))
        monkeypatch.setattr(dream, "_ensure_submitted", AsyncMock())
        captured = []

        async def result(*args, **kwargs):
            for _ in range(300):
                if probe.names_path.exists():
                    captured.append(set(json.loads(probe.names_path.read_text())))
                    captured.append(globals_names(probe) | globals_names(probe, hidden=True))
                    return "completed"
                await asyncio.sleep(0.01)
            raise AssertionError("inert dream child did not report")

        monkeypatch.setattr(dream, "_wait_for_result", result)
        outcome = await dream.run("synthetic prompt")
        assert outcome.ok
        assert not {"PINKY_SESSION_SECRET", "PINKYBOT_FERRY_SHARED_SECRET"} & captured[0]
        assert "HRPOS_PASSWORD" in captured[0]
        assert allowed(captured[1], globals=True)
        assert json.loads(probe.base_path.read_text())["home_matches"]
        trust = json.loads((probe.home / ".claude.json").read_text())
        assert trust["projects"][str(tmp_path.resolve())]["hasTrustDialogAccepted"]


@pytest.mark.parametrize("hidden", [False, True])
async def test_dirty_existing_managed_server_is_never_repaired(tmp_path, monkeypatch, hidden):
    async with managed_probe(tmp_path, monkeypatch) as probe:
        # Fixture-only server setup. Hidden pollution uses a private config
        # file so no synthetic value is sent on any tmux command line.
        config = tmp_path / "synthetic-tmux.conf"
        config.write_text("set-environment -g " + ("-h " if hidden else "") + "FOREIGN_SERVER_NAME " + CANARY + "\n")
        subprocess.run([probe.tmux, "-f", str(config), "-S", str(probe.socket),
                        "new-session", "-d", "-s", "keeper", "sleep 60"],
                       env=probe.seed, capture_output=True, check=True, timeout=5)
        before = globals_names(probe, hidden=hidden)
        assert "FOREIGN_SERVER_NAME" in before
        with pytest.raises(LaunchEnvError):
            await probe.control.new_session(cwd=str(tmp_path), command="true", env={}, inherit="none")
        assert globals_names(probe, hidden=hidden) == before
        assert not probe.names_path.exists()
        assert not list(probe.home.rglob("env-*.json"))


async def test_candidate_path_resolves_fake_tool_after_loader(tmp_path, monkeypatch):
    async with managed_probe(tmp_path, monkeypatch) as probe:
        report = tmp_path / "path-booleans.json"
        expected = str(probe.bin)
        code = ("import json,os,pathlib,shutil; "
                f"pathlib.Path({str(report)!r}).write_text(json.dumps({{"
                f"'candidate_present':{expected!r} in os.environ.get('PATH','').split(':'),"
                f"'fake_resolved':shutil.which('claude')=={str(probe.bin / 'claude')!r}}}))")
        result = await probe.control.new_session(cwd=str(tmp_path),
            command=shlex.join([sys.executable, "-I", "-c", code]), env={}, inherit="none")
        assert result.ok
        for _ in range(300):
            if report.exists():
                break
            await asyncio.sleep(0.01)
        assert json.loads(report.read_text()) == {"candidate_present": True, "fake_resolved": True}


async def test_shared_default_is_untouched_while_compatibility_launches(tmp_path, monkeypatch):
    probe = LaunchProbe(tmp_path, monkeypatch, start_server=False)
    try:
        with private_labels("default") as root:
            monkeypatch.setenv("TMUX_TMPDIR", root)
            monkeypatch.setenv("PINKY_TMUX_SOCKET", "")
            ctrl = control(owner("claude", tmp_path))
            cmd = ctrl._base_cmd()
            assert "-L" in cmd and cmd[cmd.index("-L") + 1] == "default", "shared route must be pinned"
            base = [probe.tmux, "-f", "/dev/null", "-L", "default"]
            env = {**probe.seed, "TMUX_TMPDIR": root, "FOREIGN_SERVER_NAME": CANARY}
            try:
                subprocess.run(base + ["new-session", "-d", "-s", "unrelated", "sleep 60"], env=env, check=True, capture_output=True, timeout=5)
                before = subprocess.run(base + ["show-environment", "-g"], env=env, check=True, capture_output=True, timeout=5).stdout
                result = await ctrl.new_session(cwd=str(tmp_path), command="sleep 30", env={})
                assert result.ok
                after = subprocess.run(base + ["show-environment", "-g"], env=env, check=True, capture_output=True, timeout=5).stdout
                unchanged = before == after
                assert unchanged, "shared globals changed"
                assert subprocess.run(base + ["has-session", "-t", "=unrelated"], env=env, capture_output=True, timeout=5).returncode == 0
                with pytest.raises(LaunchEnvError):
                    await ctrl.new_session(cwd=str(tmp_path), command="true", env={}, inherit="none")
            finally:
                subprocess.run(base + ["kill-server"], env=env, capture_output=True, timeout=5)
    finally:
        await probe.close()
