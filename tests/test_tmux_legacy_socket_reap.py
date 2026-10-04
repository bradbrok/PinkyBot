"""Startup migration contract. Missing reaper API is reported explicitly.

The proposed narrow seam is reap_legacy_tmux_sessions(registry, log=...) ->
set of blocked agent names. These tests prescribe behavior, not a background
worker: startup runs one pass and the persistent marker disables future passes.
"""

import ast
import asyncio
import os
import shutil
import sqlite3
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from pinky_daemon import tmux_session
from pinky_daemon.isolated_launch_env import LaunchEnvError, LaunchPolicy
from tests.tmux_server_env_support import CANARY, FAMILIES, control, no_values, owner, seed
from tests.tmux_socket_support import private_labels

pytestmark = pytest.mark.legacy_tmux_reap


class Registry:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)")
        self.agents = [SimpleNamespace(name="test-agent", sleeping=True), SimpleNamespace(name="other-agent", sleeping=False)]
        self.writes = []

    def list(self, **kwargs):
        return self.agents

    def get_setting(self, key, default=""):
        row = self.db.execute("SELECT value FROM system_settings WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def set_setting(self, key, value):
        self.writes.append((key, value))
        self.db.execute("INSERT OR REPLACE INTO system_settings VALUES (?, ?)", (key, value))
        self.db.commit()


def reaper():
    fn = getattr(tmux_session, "reap_legacy_tmux_sessions", None)
    assert callable(fn), "S6 startup legacy reaper API absent on RED base"
    return fn


@pytest.fixture
def rig(tmp_path, monkeypatch):
    seed(monkeypatch, tmp_path)
    root = tmp_path / "routing"
    root.mkdir(mode=0o700)
    monkeypatch.setenv("TMUX_TMPDIR", str(root))
    monkeypatch.setenv("TMUX", "/synthetic/wrong-server,1,1")
    monkeypatch.setenv("PINKY_TMUX_SOCKET", "test-new-fleet")
    registry = Registry(tmp_path / "settings.sqlite")
    state = SimpleNamespace(registry=registry, calls=[], logs=[], live=set(), mode="ok", root=root)
    state.names = {control(owner(kind, tmp_path)).session_name for kind in FAMILIES}

    async def present(ctrl):
        cmd = ctrl._base_cmd()
        state.calls.append(("has", ctrl.session_name, cmd))
        assert "-L" in cmd and cmd[cmd.index("-L") + 1] == "default"
        assert ctrl._local_socket_path() == root / f"tmux-{os.getuid()}" / "default"
        if state.mode == "probe_error" and ctrl.session_name in state.names:
            raise OSError(CANARY)
        return ctrl.session_name in state.live

    async def strict(ctrl, *, agent_name, action):
        state.calls.append(("kill", ctrl.session_name, ctrl._base_cmd()))
        assert ctrl.session_name in state.live, "must not kill an absent target"
        if agent_name == "test-agent":
            if state.mode == "raise":
                raise OSError(CANARY)
            if state.mode == "timeout":
                raise asyncio.TimeoutError(CANARY)
            if state.mode == "cancel":
                raise asyncio.CancelledError
            if state.mode == "failure":
                return CANARY
            if state.mode == "lie":
                return None
        state.live.discard(ctrl.session_name)
        return None

    monkeypatch.setattr(tmux_session._TmuxControl, "has_session", present)
    monkeypatch.setattr(tmux_session, "_strict_owned_tmux_cleanup", strict)
    monkeypatch.setattr(tmux_session, "_log", state.logs.append)
    yield state
    no_values(state.logs)
    registry.db.close()


@pytest.mark.parametrize("kind", FAMILIES)
async def test_only_exact_registered_owned_names_reaped(rig, tmp_path, kind):
    target = control(owner(kind, tmp_path)).session_name
    preserved = {target + "-old", target + "-suffix", "other-session", "login-hold-test-agent", "pinky-unregistered"}
    rig.live = {target, *preserved}
    blocked = await reaper()(rig.registry, log=rig.logs.append)
    assert not blocked
    assert rig.live == preserved
    assert {name for op, name, _ in rig.calls if op == "kill"} == {target}
    assert rig.registry.writes, "full absence proof must persist completion"
    assert not any(token in argv for _, _, argv in rig.calls for token in ("kill-server", "set-environment", "list-sessions"))


@pytest.mark.parametrize("mode", ["failure", "raise", "timeout", "lie", "probe_error"])
async def test_failure_blocks_only_affected_agent_and_never_completes(rig, mode):
    rig.mode = mode
    rig.live = set(rig.names) | {"pinky-other-agent"}
    blocked = await reaper()(rig.registry, log=rig.logs.append)
    assert set(blocked) == {"test-agent"}
    assert "pinky-other-agent" not in rig.live
    assert not rig.registry.writes
    assert rig.names & rig.live


async def test_cancel_does_not_publish_completion(rig):
    rig.mode, rig.live = "cancel", set(rig.names)
    with pytest.raises(asyncio.CancelledError):
        await reaper()(rig.registry, log=rig.logs.append)
    assert not rig.registry.writes


async def test_shared_default_opt_out_never_reaps_or_marks_done(rig, monkeypatch):
    monkeypatch.setenv("PINKY_TMUX_SOCKET", "")
    rig.live = set(rig.names)
    assert not await reaper()(rig.registry, log=rig.logs.append)
    assert not rig.calls and not rig.registry.writes
    assert rig.live == rig.names


async def test_completion_survives_new_registry_and_never_reaps_again(rig, tmp_path):
    await reaper()(rig.registry, log=rig.logs.append)
    assert rig.registry.writes
    rig.calls.clear()
    rig.live = set(rig.names)
    reopened = Registry(tmp_path / "settings.sqlite")
    try:
        assert not await reaper()(reopened, log=rig.logs.append)
        assert not rig.calls
        assert rig.live == rig.names
    finally:
        reopened.db.close()


async def test_failed_pass_can_retry_without_killing_absent_names(rig):
    rig.mode, rig.live = "failure", {"pinky-test-agent"}
    assert await reaper()(rig.registry, log=rig.logs.append) == {"test-agent"}
    rig.mode = "ok"
    assert not await reaper()(rig.registry, log=rig.logs.append)
    assert not rig.live and rig.registry.writes


class SpawnReachedError(RuntimeError):
    pass


REFUSAL = "legacy tmux cleanup has not completed"


def launch_probe(kind, root, monkeypatch, registry, *, agent="test-agent"):
    """Leave launch gating real and stop at the first process creation seam."""
    obj = owner(kind, root, registry=registry, agent=agent)
    ctrl = control(obj)
    attempted = []
    gate_errors = []

    async def spawn(**kwargs):
        attempted.append(True)
        raise SpawnReachedError

    monkeypatch.setattr(ctrl, "has_session", AsyncMock(return_value=False))
    monkeypatch.setattr(ctrl, "new_session", spawn)
    if kind == "dream":
        from pinky_daemon import tmux_dream_runner

        def check(name):
            try:
                tmux_session.require_legacy_tmux_reaped(name)
            except LaunchEnvError as exc:
                gate_errors.append(exc)
                raise

        monkeypatch.setattr(tmux_dream_runner, "require_legacy_tmux_reaped", check)
        monkeypatch.setattr(obj, "_tmux", AsyncMock(return_value=(0, "")))
        monkeypatch.setattr(obj, "_seed_trust", lambda _: False)
        monkeypatch.setattr(obj, "_resolve_binary", lambda: "synthetic-command")
    else:
        monkeypatch.setattr(obj, "_launch_env_policy", lambda: LaunchPolicy())
        if kind == "app_server":
            monkeypatch.setattr(obj, "_kill_tmux_session", AsyncMock())
            monkeypatch.setattr(obj, "_unlink_sock", lambda: None)
            monkeypatch.setattr(obj, "_ensure_sock_dir_secure", lambda: None)
            monkeypatch.setattr(obj, "_build_env", lambda **kw: {})
        else:
            for method in ("_ensure_container_started", "_reap_retained_spawn_cleanup_debt",
                           "_stop_tailer", "_start_tailer", "_seed_container_trust", "_seed_container_home_creds"):
                monkeypatch.setattr(obj, method, AsyncMock())
            monkeypatch.setattr(obj, "_container_agent", lambda **kw: None)
            monkeypatch.setattr(obj, "_select_command_runner", lambda *a: ctrl._runner)
            monkeypatch.setattr(obj, "_prepare_tmux_spawn", lambda: None)
            monkeypatch.setattr(obj, "_build_claude_cmd", lambda: "synthetic-command")
            monkeypatch.setattr(obj, "_build_repl_env", lambda **kw: {})
            monkeypatch.setattr(obj, "_transcript_candidates", lambda: [])
            monkeypatch.setattr(tmux_session, "_seed_claude_trust_file", lambda *a, **kw: False)

    async def assert_launch(*, allowed):
        before = len(attempted)
        if allowed:
            tmux_session.require_legacy_tmux_reaped(agent)
        else:
            with pytest.raises(LaunchEnvError, match="^" + REFUSAL + "$"):
                tmux_session.require_legacy_tmux_reaped(agent)
        if kind == "dream":
            result = await obj.run("synthetic prompt")
            assert not result.ok
            assert result.error == ("tmux new-session failed: SpawnReachedError" if allowed else REFUSAL)
            if not allowed:
                assert type(gate_errors[-1]) is LaunchEnvError
                assert str(gate_errors[-1]) == REFUSAL
        else:
            expected = SpawnReachedError if allowed else LaunchEnvError
            with pytest.raises(expected) as caught:
                await (obj.start() if kind == "app_server" else obj._spawn_tmux_repl())
            assert type(caught.value) is expected
            if not allowed:
                assert str(caught.value) == REFUSAL
        assert len(attempted) - before == int(allowed)

    return assert_launch


@pytest.mark.parametrize("kind", FAMILIES)
async def test_failed_reap_blocks_replacement_launch(rig, tmp_path, monkeypatch, kind):
    rig.mode, rig.live = "failure", set(rig.names)
    assert await reaper()(rig.registry, log=rig.logs.append) == {"test-agent"}
    launch = launch_probe(kind, tmp_path, monkeypatch, rig.registry)
    await launch(allowed=False)
    other = launch_probe(kind, tmp_path, monkeypatch, rig.registry, agent="other-agent")
    await other(allowed=True)
    rig.mode = "ok"
    assert not await reaper()(rig.registry, log=rig.logs.append)
    # The very same app-server fixture must reach spawn after the gate opens.
    await launch(allowed=True)


@pytest.mark.parametrize("kind", FAMILIES)
@pytest.mark.parametrize("state", ["present", "absent", "marker", "compat"])
async def test_completed_reap_unblocks_every_registered_launch(rig, tmp_path, monkeypatch, kind, state):
    if state == "present":
        rig.live = set(rig.names) | {"pinky-other-agent"}
    if state == "marker":
        rig.registry.set_setting(tmux_session._LEGACY_TMUX_COMPLETE, "1")
    if state == "compat":
        monkeypatch.setenv("PINKY_TMUX_SOCKET", "")
    assert not await reaper()(rig.registry, log=rig.logs.append)
    for agent in rig.registry.agents:
        launch = launch_probe(kind, tmp_path, monkeypatch, rig.registry, agent=agent.name)
        await launch(allowed=True)


@pytest.mark.parametrize("kind", FAMILIES)
async def test_unexpected_startup_reap_error_blocks_all_and_logs_no_values(rig, tmp_path, monkeypatch, kind):
    def fail_marker(*args):
        raise OSError(CANARY)

    monkeypatch.setattr(rig.registry, "set_setting", fail_marker)
    # Execute the actual startup error boundary without starting unrelated API services.
    tree = ast.parse(Path(tmux_session.__file__).with_name("api.py").read_text())
    blocks = [node for node in ast.walk(tree) if isinstance(node, ast.Try) and any(
        isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Await)
        and isinstance(stmt.value.value, ast.Call)
        and isinstance(stmt.value.value.func, ast.Name)
        and stmt.value.value.func.id == "reap_legacy_tmux_sessions" for stmt in node.body)]
    assert len(blocks) == 1
    wrapper = ast.parse("async def startup_reap():\n    pass\n")
    wrapper.body[0].body = [blocks[0]]
    namespace = {"asyncio": asyncio, "agents": rig.registry, "_log": rig.logs.append}
    exec(compile(ast.fix_missing_locations(wrapper), "<startup-reap-test>", "exec"), namespace)
    await namespace["startup_reap"]()
    assert rig.logs == ["ERROR legacy tmux startup cleanup failed; tmux launches blocked"]
    assert not rig.registry.writes
    for agent in rig.registry.agents:
        launch = launch_probe(kind, tmp_path, monkeypatch, rig.registry, agent=agent.name)
        await launch(allowed=False)


@pytest.mark.parametrize("kind", FAMILIES)
async def test_real_legacy_pair_preserves_lookalikes_globals_and_new_server(tmp_path, monkeypatch, kind):
    fn = reaper()  # API-absence RED precedes any real-process fixture setup.
    binary = shutil.which("tmux")
    assert binary, "tmux is required for the private legacy integration"
    seed(monkeypatch, tmp_path)
    registry = Registry(tmp_path / "settings.sqlite")
    target = control(owner(kind, tmp_path)).session_name
    preserved = {target + "-old", "login-hold-test-agent", "pinky-unregistered", "unrelated"}
    logs = []
    try:
        with private_labels("default", "test-new-fleet") as root:
            monkeypatch.setenv("TMUX_TMPDIR", root)
            monkeypatch.setenv("PINKY_TMUX_SOCKET", "test-new-fleet")
            monkeypatch.setenv("TMUX", "/synthetic/wrong,1,1")
            env = {"HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "TMUX_TMPDIR": root, "FOREIGN_SERVER_NAME": CANARY}

            def run(label, *args):
                return subprocess.run([binary, "-f", "/dev/null", "-L", label, *args],
                                      env=env, capture_output=True, timeout=5)

            try:
                for name in {target, *preserved}:
                    assert run("default", "new-session", "-d", "-s", name, "sleep 60").returncode == 0
                assert run("test-new-fleet", "new-session", "-d", "-s", "new-unrelated", "sleep 60").returncode == 0
                before = run("default", "show-environment", "-g").stdout
                assert not await fn(registry, log=logs.append)
                names = set(run("default", "list-sessions", "-F", "#{session_name}").stdout.decode().splitlines())
                assert names == preserved
                unchanged = before == run("default", "show-environment", "-g").stdout
                assert unchanged, "legacy globals changed"
                assert run("test-new-fleet", "has-session", "-t", "=new-unrelated").returncode == 0
                assert registry.writes
                no_values(logs)
            finally:
                run("default", "kill-server")
                run("test-new-fleet", "kill-server")
    finally:
        registry.db.close()


def test_startup_wiring_calls_reaper_before_boot_launch():
    # Read code only; never instantiate the API daemon or open a live registry.
    import ast
    source = Path(tmux_session.__file__).with_name("api.py").read_text()
    tree = ast.parse(source)
    calls = [node for node in ast.walk(tree) if isinstance(node, ast.Call)]
    reaps = [n.lineno for n in calls if isinstance(n.func, ast.Name) and n.func.id == "reap_legacy_tmux_sessions"]
    assert reaps, "S6 legacy reap is not wired into startup"
    boots = [n.lineno for n in calls if isinstance(n.func, ast.Name) and n.func.id == "_launch_boot_session"]
    assert boots and min(reaps) < min(boots)


async def test_strict_cleanup_diagnostics_do_not_include_values(tmp_path, monkeypatch):
    logs = []
    ctrl = SimpleNamespace(kill_session=AsyncMock(return_value=SimpleNamespace(ok=False, returncode=1, stderr=CANARY)),
                           has_session=AsyncMock(side_effect=OSError(CANARY)))
    monkeypatch.setattr(tmux_session, "_log", logs.append)
    monkeypatch.setattr(tmux_session, "_SPAWN_ROLLBACK_ATTEMPTS", 1)
    result = await tmux_session._strict_owned_tmux_cleanup(ctrl, agent_name="test-agent", action="legacy reap")
    assert result
    no_values(logs, result)
