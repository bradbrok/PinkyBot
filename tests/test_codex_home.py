"""Per-agent Codex home isolation, auth, and rollout migration tests."""

from __future__ import annotations

import json
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from pinky_daemon.codex_home import (
    MANAGED_CONFIG_SENTINEL,
    PER_AGENT_CODEX_HOME_ENV,
    codex_home_for,
    prepare_agent_codex_home,
    rollback_agent_rollouts,
)
from pinky_daemon.codex_session import CodexSession
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


def test_flag_off_preserves_shared_path_and_performs_no_bootstrap(
    tmp_path, monkeypatch
):
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


def test_prepare_generates_minimal_home_auth_link_and_migrates_matches(
    tmp_path, monkeypatch
):
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
