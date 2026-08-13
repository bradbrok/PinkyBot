"""Per-agent Codex home isolation, auth, and rollout migration tests."""

from __future__ import annotations

import json
import os
import threading
import tomllib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon import codex_home as codex_home_mod
from pinky_daemon.codex_home import (
    MANAGED_CONFIG_SENTINEL,
    PER_AGENT_CODEX_HOME_ENV,
    ROLLOUT_MIGRATION_MARKER,
    codex_home_for,
    move_matching_rollouts,
    prepare_agent_codex_home,
    rollback_agent_rollouts,
)
from pinky_daemon.codex_session import CodexSession
from pinky_daemon.codex_tmux_session import CodexTmuxSession
from pinky_daemon.codex_tmux_transcript import _discover_codex_rollout
from pinky_daemon.streaming_session import StreamingSessionConfig


def _scope(working_dir: Path, *, codex_home: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        agent_name="test-agent",
        working_dir=str(working_dir),
        codex_home=codex_home,
    )


def _auth(shared_home: Path) -> Path:
    shared_home.mkdir(parents=True, exist_ok=True)
    path = shared_home / "auth.json"
    path.write_text('{"test": true}\n', encoding="utf-8")
    path.chmod(0o600)
    return path


def _rollout(home: Path, name: str, cwd: Path) -> Path:
    path = home / "sessions" / "2026" / "08" / "13" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {
                "type": "session_meta",
                "payload": {"id": name, "cwd": str(cwd.resolve())},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    return path


def test_flag_off_preserves_shared_path_and_performs_no_bootstrap(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    override = tmp_path / "override"
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.delenv(PER_AGENT_CODEX_HOME_ENV, raising=False)
    scope = _scope(working_dir, codex_home=str(override))

    assert codex_home_for(scope) == shared_home
    assert prepare_agent_codex_home(scope, log=lambda _message: None) == shared_home
    assert not working_dir.exists()
    assert not override.exists()
    assert not shared_home.exists()


def test_flag_off_home_fallback_is_test_controlled(tmp_path, monkeypatch):
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv(PER_AGENT_CODEX_HOME_ENV, raising=False)
    monkeypatch.setattr("pinky_daemon.codex_home.Path.home", lambda: tmp_path)

    assert codex_home_for(_scope(tmp_path / "agent")) == tmp_path / ".codex"


def test_prepare_generates_minimal_home_auth_link_and_migrates_matches(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    other_dir = tmp_path / "other"
    working_dir.mkdir()
    other_dir.mkdir()
    auth_source = _auth(shared_home)
    matching = _rollout(shared_home, "rollout-match.jsonl", working_dir)
    other = _rollout(shared_home, "rollout-other.jsonl", other_dir)
    logs: list[str] = []

    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    home = prepare_agent_codex_home(_scope(working_dir), log=logs.append)

    assert home == working_dir / ".codex"
    config_text = (home / "config.toml").read_text(encoding="utf-8")
    assert config_text.startswith(MANAGED_CONFIG_SENTINEL)
    config = tomllib.loads(config_text)
    assert config["features"] == {"apps": False, "plugins": False}
    assert config["projects"][str(working_dir.resolve())]["trust_level"] == "trusted"
    assert "mcp_servers" not in config
    assert "plugins" not in config
    assert "marketplaces" not in config
    assert (home / "sessions").is_dir()
    assert (home / "auth.json").is_symlink()
    assert os.path.samefile(home / "auth.json", auth_source)

    migrated = home / "sessions" / matching.relative_to(shared_home / "sessions")
    assert migrated.exists()
    assert not matching.exists()
    assert other.exists()
    assert any("migrated 1 cwd-matched rollout(s)" in message for message in logs)

    before = config_text
    prepare_agent_codex_home(_scope(working_dir), log=logs.append)
    assert (home / "config.toml").read_text(encoding="utf-8") == before


def test_migration_marker_permanently_hides_late_shared_rollout(tmp_path, monkeypatch):
    """Review P1-1: migration is a one-time bridge, not a spawn-time scan."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    first = _rollout(shared_home, "rollout-first.jsonl", working_dir)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    home = prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)
    migrated = home / "sessions" / first.relative_to(shared_home / "sessions")
    assert migrated.exists()
    marker = home / ROLLOUT_MIGRATION_MARKER
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    assert marker_payload["moved_count"] == 1
    assert marker_payload["completed_at"]
    marker_bytes = marker.read_bytes()

    # Make the isolated store intentionally empty, then plant a new ambient
    # rollout with the same cwd. A later spawn must remain fresh.
    migrated.unlink()
    late = _rollout(shared_home, "rollout-planted-after-gate.jsonl", working_dir)
    second_logs: list[str] = []
    prepare_agent_codex_home(_scope(working_dir), log=second_logs.append)

    assert marker.read_bytes() == marker_bytes
    assert late.exists()
    assert not (home / "sessions" / late.relative_to(shared_home / "sessions")).exists()
    assert any("migration marker present" in message for message in second_logs)
    assert any("migrated 0 cwd-matched rollout(s)" in message for message in second_logs)

    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
    )
    session = CodexTmuxSession(config)
    command = session._build_claude_cmd()
    assert "resume --last" not in command
    assert " -C " in command
    assert session._last_launch_used_continue is False


def test_explicit_override_wins_only_when_flag_is_enabled(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    override = tmp_path / "custom-codex"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    scope = _scope(working_dir, codex_home=str(override))
    assert codex_home_for(scope) == override
    assert prepare_agent_codex_home(scope, log=lambda _message: None) == override
    assert (override / "auth.json").is_symlink()


def test_unmanaged_config_is_preserved_with_loud_log(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    manual = "# operator-managed\n[features]\nplugins = true\n"
    (agent_home / "config.toml").write_text(manual, encoding="utf-8")
    logs: list[str] = []
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    prepare_agent_codex_home(_scope(working_dir), log=logs.append)

    assert (agent_home / "config.toml").read_text(encoding="utf-8") == manual
    assert any("preserving unmanaged config" in message for message in logs)


def test_linked_config_never_mutates_shared_config(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    shared_config = shared_home / "config.toml"
    original = f"{MANAGED_CONFIG_SENTINEL}\n# shared\n"
    shared_config.write_text(original, encoding="utf-8")
    (agent_home / "config.toml").symlink_to(shared_config)
    logs: list[str] = []
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    prepare_agent_codex_home(_scope(working_dir), log=logs.append)

    assert shared_config.read_text(encoding="utf-8") == original
    assert any("preserving linked config" in message for message in logs)


def test_dangling_config_symlink_is_preserved_without_write_through(tmp_path, monkeypatch):
    """Review P1-2: exists() must never precede symlink classification."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    missing_shared_target = shared_home / "config.toml"
    config_link = agent_home / "config.toml"
    config_link.symlink_to(missing_shared_target)
    logs: list[str] = []
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    prepare_agent_codex_home(_scope(working_dir), log=logs.append)

    assert config_link.is_symlink()
    assert not missing_shared_target.exists()
    assert any("preserving linked config" in message for message in logs)


def test_partial_migration_final_is_quarantined_and_retry_converges(tmp_path, monkeypatch):
    """Review P1-3: recover an old crash artifact without shadowing source."""
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    _auth(shared_home)
    source = _rollout(shared_home, "rollout-crash.jsonl", working_dir)
    source_bytes = source.read_bytes()
    relative = source.relative_to(shared_home / "sessions")
    final = agent_home / "sessions" / relative
    final.parent.mkdir(parents=True, exist_ok=True)
    partial_bytes = b'\xff{"type":"session_meta"'
    final.write_bytes(partial_bytes)
    abandoned = final.parent / ".pinkybot-rollout-migration-abandoned.tmp"
    abandoned.write_text("partial", encoding="utf-8")
    logs: list[str] = []
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    prepare_agent_codex_home(_scope(working_dir), log=logs.append)

    assert not source.exists()
    assert final.read_bytes() == source_bytes
    assert not abandoned.exists()
    quarantines = list(final.parent.glob(f".pinkybot-rollout-quarantine-{final.name}-*"))
    assert len(quarantines) == 1
    assert quarantines[0].read_bytes() == partial_bytes
    assert any("removed abandoned rollout migration temp" in message for message in logs)
    assert any("quarantined invalid rollout migration final" in message for message in logs)


def test_same_source_migration_loser_converges_and_counts_result(tmp_path, monkeypatch):
    """Review P2: losing the source unlink race is successful convergence."""
    shared_home = tmp_path / "shared"
    destination_home = tmp_path / "agent-home"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    source = _rollout(shared_home, "rollout-race.jsonl", working_dir)
    relative = source.relative_to(shared_home / "sessions")
    destination = destination_home / "sessions" / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(source.read_bytes())
    real_unlink = Path.unlink

    def winner_claimed_then_raised(path: Path, *args, **kwargs):
        if path == source:
            real_unlink(path)
            raise FileNotFoundError(path)
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", winner_claimed_then_raised)
    logs: list[str] = []

    moved = move_matching_rollouts(
        shared_home,
        destination_home,
        working_dir,
        log=logs.append,
    )

    assert moved == 1
    assert not source.exists()
    assert destination.exists()
    assert any("migration converged" in message for message in logs)


def test_concurrent_same_source_migrators_both_converge(tmp_path, monkeypatch):
    """Review P2 exact shape: two pre-lock scans, one atomic final."""
    shared_home = tmp_path / "shared"
    destination_home = tmp_path / "agent-home"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    source = _rollout(shared_home, "rollout-concurrent.jsonl", working_dir)
    relative = source.relative_to(shared_home / "sessions")
    destination = destination_home / "sessions" / relative
    source_sessions = shared_home / "sessions"
    scan_barrier = threading.Barrier(2)
    real_glob = Path.glob

    def synchronized_glob(path: Path, pattern: str):
        found = list(real_glob(path, pattern))
        if path == source_sessions and pattern == "**/rollout-*.jsonl":
            scan_barrier.wait(timeout=5)
        return iter(found)

    monkeypatch.setattr(Path, "glob", synchronized_glob)
    logs: list[str] = []

    def migrate() -> int:
        return move_matching_rollouts(
            shared_home,
            destination_home,
            working_dir,
            log=logs.append,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate), pool.submit(migrate)]
        moved = [future.result(timeout=10) for future in futures]

    assert moved == [1, 1]
    assert not source.exists()
    assert destination.exists()
    assert any("source was already claimed" in message for message in logs)


def test_concurrent_initial_migration_commits_one_accurate_marker(
    tmp_path, monkeypatch
):
    """The one-time marker closes the scan-to-gate race between spawns."""
    shared_home = tmp_path / "shared"
    destination_home = tmp_path / "agent-home"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    source = _rollout(shared_home, "rollout-initial-race.jsonl", working_dir)
    source_sessions = shared_home / "sessions"
    destination_sessions = destination_home / "sessions"
    destination_sessions.mkdir(parents=True)
    scan_barrier = threading.Barrier(2)
    real_glob = Path.glob

    def synchronized_glob(path: Path, pattern: str):
        found = list(real_glob(path, pattern))
        if path == source_sessions and pattern == "**/rollout-*.jsonl":
            scan_barrier.wait(timeout=5)
        return iter(found)

    monkeypatch.setattr(Path, "glob", synchronized_glob)
    logs: list[str] = []

    def migrate() -> int:
        return codex_home_mod._run_initial_rollout_migration(
            shared_home,
            destination_home,
            working_dir,
            log=logs.append,
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(migrate), pool.submit(migrate)]
        moved = [future.result(timeout=10) for future in futures]

    marker = destination_home / ROLLOUT_MIGRATION_MARKER
    marker_payload = json.loads(marker.read_text(encoding="utf-8"))
    destination = destination_sessions / source.relative_to(source_sessions)
    assert moved == [1, 1]
    assert marker_payload["moved_count"] == 1
    assert not source.exists()
    assert destination.exists()
    assert any("marker already committed" in message for message in logs)


def test_auth_absence_refuses_spawn_before_creating_agent_home(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    shared_home.mkdir()
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    with pytest.raises(RuntimeError, match="shared auth file is absent or unreadable"):
        prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)

    assert not (working_dir / ".codex").exists()


def test_existing_non_symlink_auth_refuses_spawn(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    agent_home = working_dir / ".codex"
    working_dir.mkdir()
    agent_home.mkdir()
    _auth(shared_home)
    (agent_home / "auth.json").write_text('{"forked": true}\n', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    with pytest.raises(RuntimeError, match="not the managed auth symlink"):
        prepare_agent_codex_home(_scope(working_dir), log=lambda _message: None)


def test_discovery_uses_agent_home_not_shared_home(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    agent_home = working_dir / ".codex"
    shared_match = _rollout(shared_home, "rollout-shared.jsonl", working_dir)
    agent_match = _rollout(agent_home, "rollout-agent.jsonl", working_dir)
    os.utime(shared_match, (agent_match.stat().st_mtime + 10,) * 2)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")

    assert _discover_codex_rollout(working_dir, agent=_scope(working_dir)) == agent_match


def test_subprocess_transport_overlays_prepared_home(tmp_path, monkeypatch):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    working_dir.mkdir()
    _auth(shared_home)
    monkeypatch.setenv("CODEX_HOME", str(shared_home))
    monkeypatch.setenv(PER_AGENT_CODEX_HOME_ENV, "1")
    config = StreamingSessionConfig(
        agent_name="test-agent",
        working_dir=str(working_dir),
        provider_url="codex_cli",
    )

    env = CodexSession(config)._build_codex_env()

    assert env["CODEX_HOME"] == str(working_dir / ".codex")
    assert (working_dir / ".codex" / "auth.json").is_symlink()


def test_reverse_migration_moves_only_matching_rollouts(tmp_path):
    shared_home = tmp_path / "shared"
    working_dir = tmp_path / "agent"
    other_dir = tmp_path / "other"
    working_dir.mkdir()
    other_dir.mkdir()
    agent_home = working_dir / ".codex"
    matching = _rollout(agent_home, "rollout-match.jsonl", working_dir)
    other = _rollout(agent_home, "rollout-other.jsonl", other_dir)
    logs: list[str] = []

    moved = rollback_agent_rollouts(
        working_dir,
        agent_home,
        shared_home=shared_home,
        log=logs.append,
    )

    restored = shared_home / "sessions" / matching.relative_to(agent_home / "sessions")
    assert moved == 1
    assert restored.exists()
    assert not matching.exists()
    assert other.exists()
    assert any("rolled back 1 cwd-matched rollout(s)" in message for message in logs)
