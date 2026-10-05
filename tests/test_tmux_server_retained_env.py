"""G2 contracts: retained base values and value-free server warnings."""

import asyncio
import json
import subprocess
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.command_runner import (
    CommandResult,
    ContainerCommandRunner,
    RunuserCommandRunner,
)
from pinky_daemon.tmux_dream_runner import TmuxDreamConfig, TmuxDreamRunner
from tests.test_tmux_server_env_integration import managed_probe
from tests.tmux_isolated_env_support import Registry
from tests.tmux_server_env_support import FAMILIES, control, owner, seed


async def launch_dream(probe, monkeypatch):
    dream = TmuxDreamRunner(TmuxDreamConfig(working_dir=str(probe.root),
        claude_binary=str(probe.bin / "claude"), poll_interval_s=0.01), agent_name="test-agent")
    probe.control.session_name = dream.session_name
    dream._control = probe.control
    monkeypatch.setattr(dream, "_wait_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(dream, "_ensure_submitted", AsyncMock())

    async def result(*args, **kwargs):
        for _ in range(300):
            if probe.names_path.exists():
                return "completed"
            await asyncio.sleep(0.01)
        raise AssertionError("inert dream child did not report")

    monkeypatch.setattr(dream, "_wait_for_result", result)
    assert (await dream.run("synthetic prompt")).ok


@pytest.mark.parametrize("kind,clean", [
    ("claude", False), ("claude", True), ("codex", False), ("codex", True),
    ("app_server", False), ("app_server", True), ("dream", False),
])
@pytest.mark.parametrize("retained", [False, True])
async def test_pane_base_and_trust_follow_constructed_client(tmp_path, monkeypatch, kind, clean, retained):
    async with managed_probe(tmp_path, monkeypatch) as probe:
        expected_path = str(probe.bin) + ":/usr/bin:/bin"
        path_report = tmp_path / "child-path-booleans.json"
        for executable in (probe.bin / "claude", probe.bin / "codex"):
            lines = executable.read_text().splitlines(keepends=True)
            lines.insert(1, "import json,os,pathlib; "
                f"pathlib.Path({str(path_report)!r}).write_text(json.dumps({{"
                f"'path_matches':os.environ.get('PATH')=={expected_path!r}}}))\n")
            executable.write_text("".join(lines))
        old_home = tmp_path / "old-home"
        old_home.mkdir(mode=0o700)
        base = [probe.tmux, "-f", "/dev/null", "-S", str(probe.socket)]
        if retained:
            old_env = {**probe.control.server_config.client_env,
                       "HOME": str(old_home), "PATH": "/usr/bin:/bin"}
            subprocess.run(base + ["new-session", "-d", "-s", "keeper", "sleep 60"],
                           env=old_env, check=True, capture_output=True, timeout=5)
            before = subprocess.run(base + ["show-environment", "-g"],
                env=probe.seed, check=True, capture_output=True, timeout=5).stdout
        if clean:
            grants = tmp_path / "grants.json"
            grants.write_text("{}")
            grants.chmod(0o600)
            monkeypatch.setenv("PINKY_ISOLATED_ENV", "enforce")
            monkeypatch.setenv("PINKY_ISOLATED_ENV_GRANTS_FILE", str(grants))
        if kind == "dream":
            await launch_dream(probe, monkeypatch)
        else:
            await probe.launch(kind, registry=Registry("isolated" if clean else "not_isolated"))
        if kind in {"claude", "codex", "dream"}:
            trust = json.loads((probe.home / ".claude.json").read_text())
            assert trust["projects"][str(tmp_path.resolve())]["hasTrustDialogAccepted"]
            assert not (old_home / ".claude.json").exists()
        assert json.loads(probe.base_path.read_text())["home_matches"], "pane HOME differs from trust preseed HOME"
        assert json.loads(path_report.read_text())["path_matches"], "pane PATH kept the retained server value"
        if retained:
            after = subprocess.run(base + ["show-environment", "-g"],
                env=probe.seed, check=True, capture_output=True, timeout=5).stdout
            assert before == after, "launch must not repair retained server globals"
            assert subprocess.run(base + ["has-session", "-t", "=keeper"],
                env=probe.seed, capture_output=True, timeout=5).returncode == 0


@pytest.mark.parametrize("kind", FAMILIES)
@pytest.mark.parametrize("runner_type", [RunuserCommandRunner, ContainerCommandRunner])
@pytest.mark.parametrize("clean", [False, True])
async def test_wrapped_pane_home_and_path_are_not_replaced(tmp_path, monkeypatch, kind, runner_type, clean):
    seed(monkeypatch, tmp_path)
    monkeypatch.setenv("PINKY_TMUX_PANE_PATH", "/synthetic/local-only")
    staged = []

    class Recorder:
        async def run(self, argv, **kwargs):
            assert "env" not in kwargs
            assert "show-environment" not in argv
            request = json.loads(kwargs["stdin_data"]) if kwargs.get("stdin_data") else {}
            if request.get("action") == "stage":
                staged.append(request["env"])
                path = (tmp_path / ".local/state/pinkybot/tmux-launch-env" /
                        request["scope"] / f"env-{request['nonce']}.json")
                return CommandResult(0, json.dumps({"path": str(path)}).encode(), b"")
            return CommandResult(0, b"", b"")

    ctrl = control(owner(kind, tmp_path))
    ctrl.set_command_runner(runner_type("test-namespace", inner=Recorder()))
    pane_env = {"HOME": "/synthetic/tenant-home", "PATH": "/synthetic/tenant-bin"}
    result = await ctrl.new_session(cwd=str(tmp_path), command="true", env=pane_env,
                                    inherit="none" if clean else "all")
    try:
        assert result.ok
        assert staged == [pane_env]
    finally:
        await tmux_session._cleanup_launch_env(result.launch_env)


async def test_multiline_allowed_value_warning_never_prints_fragment(tmp_path, monkeypatch):
    async with managed_probe(tmp_path, monkeypatch) as probe:
        fragment = "SYNTHETIC_" + "PROTECTED_VALUE_809"
        value = "legitimate\n" + fragment + "=tail"
        server_env = {**probe.control.server_config.client_env, "XDG_TEST_VALUE": value}
        subprocess.run([probe.tmux, "-f", "/dev/null", "-S", str(probe.socket),
                        "new-session", "-d", "-s", "keeper", "sleep 60"],
                       env=server_env, check=True, capture_output=True, timeout=5)
        for _ in range(2):
            await probe.launch("claude", registry=Registry("not_isolated"))
        warnings = [line for line in probe.logs if line.startswith("WARNING")]
        leaked = any(fragment in line or value in line for line in warnings)
        assert not leaked, "warning exposed a fragment of a multiline server value"
        assert len(warnings) == 1
        assert "unexpected names: 1" in warnings[0]
        assert "tmux -L test-managed show-environment -g" in warnings[0]
