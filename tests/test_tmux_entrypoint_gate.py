"""Public standalone dream launches must observe the startup migration gate."""

import ast
import asyncio
import builtins
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import dream_runner, tmux_dream_runner, tmux_session
from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.dream_runner import DreamRunner
from pinky_daemon.isolated_launch_env import LaunchEnvError
from pinky_daemon.tmux_dream_runner import TmuxDreamRunner
from tests.test_tmux_legacy_socket_reap import Registry
from tests.tmux_isolated_env_support import LaunchProbe
from tests.tmux_server_env_support import CANARY, no_values, seed
from tests.tmux_socket_support import private_labels

pytestmark = pytest.mark.legacy_tmux_reap
REFUSAL = "legacy tmux cleanup has not completed; start the daemon once"


@pytest.mark.parametrize("transport_source", ["environment", "settings"])
@pytest.mark.parametrize("state", ["unmigrated", "marker", "compat", "read_error"])
async def test_public_standalone_dream_observes_gate(tmp_path, monkeypatch, transport_source, state):
    probe = LaunchProbe(tmp_path, monkeypatch, start_server=False)
    registry = AgentRegistry(db_path=str(tmp_path / "agents.sqlite"))
    registry.register("test-agent", working_dir=str(tmp_path), transport="tmux")
    marker = tmux_session._LEGACY_TMUX_COMPLETE
    if state == "marker":
        registry.set_setting(marker, "1")
    reads = []

    def settings(key):
        reads.append(key)
        if key == "PINKY_DREAM_TRANSPORT" and transport_source == "settings":
            return "tmux"
        if key == marker and state == "read_error":
            raise OSError(CANARY)
        return registry.get_setting(key)

    runner = DreamRunner(db_path=str(tmp_path / "dream.sqlite"), setting_provider=settings,
        history_provider=lambda *a: [{"timestamp": 200., "role": "user", "content": "synthetic history"}])
    reaper = AsyncMock(side_effect=AssertionError("standalone entry attempted migration"))
    monkeypatch.setattr(tmux_session, "reap_legacy_tmux_sessions", reaper)
    if transport_source == "environment":
        monkeypatch.setenv("PINKY_DREAM_TRANSPORT", "tmux")
    monkeypatch.setenv("PINKY_CLAUDE_BIN", str(probe.bin / "claude"))
    for name in ("_build_memory_links", "_extract_user_profiles", "_extract_user_relationships",
                 "_extract_proposed_skills", "_extract_kg_from_dream_output", "_extract_kg_triples"):
        monkeypatch.setattr(runner, name, lambda *a, **kw: 0)
    monkeypatch.setattr(runner, "_surface_kg_insights", lambda *a, **kw: "")
    monkeypatch.setattr(runner, "_reflection_ids_for_attempt", lambda *a, **kw: [])
    monkeypatch.setattr(runner, "_record_reflection_outcome", AsyncMock())
    monkeypatch.setattr(TmuxDreamRunner, "_wait_ready", AsyncMock(return_value=True))
    monkeypatch.setattr(TmuxDreamRunner, "_ensure_submitted", AsyncMock())
    monkeypatch.setattr(dream_runner, "_log", probe.logs.append)
    monkeypatch.setattr(tmux_dream_runner, "_log", probe.logs.append)
    gate = tmux_session.require_legacy_tmux_reaped
    gate_errors = []

    def checked(*args, **kwargs):
        try:
            return gate(*args, **kwargs)
        except LaunchEnvError as exc:
            gate_errors.append(exc)
            raise

    monkeypatch.setattr(tmux_dream_runner, "require_legacy_tmux_reaped", checked)
    observed = []
    try:
        with private_labels("default", "test-standalone") as route:
            monkeypatch.setenv("TMUX_TMPDIR", route)
            monkeypatch.setenv("PINKY_TMUX_SOCKET", "" if state == "compat" else "test-standalone")
            env = {**probe.seed, "TMUX_TMPDIR": route}

            def client(label, *args):
                return subprocess.run([probe.tmux, "-f", "/dev/null", "-L", label, *args],
                    env=env, capture_output=True, timeout=5)

            async def result(self, *args, **kwargs):
                for _ in range(300):
                    if probe.names_path.exists():
                        selected = "default" if state == "compat" else "test-standalone"
                        observed.append(client(selected, "has-session", "-t", "=pinky-dream-test-agent").returncode == 0)
                        return "synthetic completed dream"
                    await asyncio.sleep(.01)
                raise AssertionError("inert standalone dream child did not report")

            monkeypatch.setattr(TmuxDreamRunner, "_wait_for_result", result)
            try:
                assert client("default", "new-session", "-d", "-s", "unrelated", "sleep 60").returncode == 0
                assert client("default", "new-session", "-d", "-s", "pinky-dream-test-agent", "sleep 60").returncode == 0
                old_pane = client("default", "display-message", "-p", "-t", "=pinky-dream-test-agent:0.0", "#{pane_id}").stdout
                config = SimpleNamespace(working_dir=str(tmp_path), model="", dream_model="", provider_key="")
                outcome = await runner.run_dream("test-agent", config)
                allowed = state in {"marker", "compat"}
                if allowed:
                    assert outcome == "synthetic completed dream"
                    assert observed == [True]
                    assert not gate_errors
                else:
                    assert not probe.names_path.exists(), "unmigrated standalone entry spawned a managed pane"
                    assert not observed
                    assert len(gate_errors) == 1 and type(gate_errors[0]) is LaunchEnvError
                    assert str(gate_errors[0]) == REFUSAL
                    assert outcome.endswith(REFUSAL)
                    assert client("test-standalone", "has-session", "-t", "=pinky-dream-test-agent").returncode != 0
                if state != "compat":
                    assert client("default", "has-session", "-t", "=pinky-dream-test-agent").returncode == 0
                    assert client("default", "display-message", "-p", "-t", "=pinky-dream-test-agent:0.0", "#{pane_id}").stdout == old_pane
                assert client("default", "has-session", "-t", "=unrelated").returncode == 0
                reaper.assert_not_awaited()
                assert bool(registry.get_setting(marker)) is (state == "marker")
                if state == "marker":
                    assert marker in reads, "standalone caller settings were not consulted"
                no_values(outcome, probe.logs)
            finally:
                client("default", "kill-server")
                client("test-standalone", "kill-server")
    finally:
        runner._db.close()
        registry._db.close()
        await probe.close()


async def test_startup_import_failure_rearms_previously_open_gate(tmp_path, monkeypatch):
    seed(monkeypatch, tmp_path)
    registry = Registry(tmp_path / "settings.sqlite")
    registry.agents = []
    logs = []
    try:
        assert await tmux_session.reap_legacy_tmux_sessions(registry, log=logs.append) == set()
        tmux_session.require_legacy_tmux_reaped("test-agent")
        assert not tmux_session._LEGACY_TMUX_BLOCK_ALL
        registry.set_setting(tmux_session._LEGACY_TMUX_COMPLETE, "")
        original_import = builtins.__import__

        def fail_import(name, *args, **kwargs):
            if name == "pinky_daemon.codex_app_server_tmux":
                raise ImportError(CANARY)
            return original_import(name, *args, **kwargs)

        tree = ast.parse(Path(tmux_session.__file__).with_name("api.py").read_text())
        blocks = [node for node in ast.walk(tree) if isinstance(node, ast.Try) and any(
            isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await)
            and isinstance(stmt.value.value, ast.Call)
            and isinstance(stmt.value.value.func, ast.Name)
            and stmt.value.value.func.id == "reap_legacy_tmux_sessions" for stmt in node.body)]
        assert len(blocks) == 1
        wrapper = ast.parse("async def startup_reap():\n    pass\n")
        wrapper.body[0].body = [blocks[0]]
        namespace = {"asyncio": asyncio, "agents": registry, "_log": logs.append}
        exec(compile(ast.fix_missing_locations(wrapper), "<startup-import-test>", "exec"), namespace)
        with monkeypatch.context() as imports:
            imports.setattr(builtins, "__import__", fail_import)
            await namespace["startup_reap"]()
        assert logs == ["ERROR legacy tmux startup cleanup failed; tmux launches blocked"]
        with pytest.raises(LaunchEnvError) as caught:
            tmux_session.require_legacy_tmux_reaped("test-agent")
        assert type(caught.value) is LaunchEnvError
        assert str(caught.value) == REFUSAL
        assert tmux_session._LEGACY_TMUX_BLOCK_ALL
        no_values(logs, str(caught.value))
    finally:
        registry.db.close()
