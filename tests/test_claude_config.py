"""Tests for shared Claude Code config-dir helpers."""

from __future__ import annotations

import json
from pathlib import Path

from pinky_daemon.claude_config import (
    claude_project_slug,
    migrate_claude_project_history,
    resolve_agent_claude_config_dir,
    resolve_claude_config_path,
    resolve_claude_settings_path,
    seed_claude_settings_file,
    seed_claude_trust_file,
)


class _Agent:
    def __init__(self, working_dir: str, isolation_mode: str = "local") -> None:
        self.working_dir = working_dir
        self.isolation_mode = isolation_mode


def _env(**overrides: str) -> dict[str, str]:
    env = {
        "PINKY_PER_AGENT_CLAUDE_CONFIG": "1",
        "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test",
    }
    env.update(overrides)
    return env


def test_resolve_agent_claude_config_dir_default_off(tmp_path: Path) -> None:
    agent = _Agent(str(tmp_path / "agent"))
    assert resolve_agent_claude_config_dir(
        agent,
        env={"CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-oat01-test"},
    ) is None


def test_resolve_agent_claude_config_dir_local_flag_on(tmp_path: Path) -> None:
    work_dir = tmp_path / "agent"
    agent = _Agent(str(work_dir))

    assert resolve_agent_claude_config_dir(agent, env=_env()) == (
        work_dir / ".claude-config"
    )


def test_resolve_agent_claude_config_dir_requires_static_token(
    tmp_path: Path,
) -> None:
    agent = _Agent(str(tmp_path / "agent"))

    assert resolve_agent_claude_config_dir(
        agent,
        env={"PINKY_PER_AGENT_CLAUDE_CONFIG": "1"},
    ) is None


def test_resolve_agent_claude_config_dir_skips_nonlocal_modes(
    tmp_path: Path,
) -> None:
    for mode in ("container", "unix_user"):
        agent = _Agent(str(tmp_path / mode), isolation_mode=mode)
        assert resolve_agent_claude_config_dir(agent, env=_env()) is None


def test_migrate_claude_project_history_copy_if_missing(
    monkeypatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    project_dir = tmp_path / "agents" / "kuzya"
    project_dir.mkdir(parents=True)
    slug = claude_project_slug(project_dir)
    src = Path.home() / ".claude" / "projects" / slug
    src.mkdir(parents=True)
    (src / "old.jsonl").write_text("history", encoding="utf-8")

    cfg = project_dir / ".claude-config"
    assert migrate_claude_project_history(project_dir, cfg) is True
    assert (cfg / "projects" / slug / "old.jsonl").read_text() == "history"

    (src / "old.jsonl").write_text("new-history", encoding="utf-8")
    assert migrate_claude_project_history(project_dir, cfg) is False
    assert (cfg / "projects" / slug / "old.jsonl").read_text() == "history"


def test_seed_trust_and_settings_for_config_dir(tmp_path: Path) -> None:
    cfg_dir = tmp_path / ".claude-config"
    env = {"CLAUDE_CONFIG_DIR": str(cfg_dir), "HOME": str(tmp_path / "home")}
    project_dir = tmp_path / "agent"
    project_dir.mkdir()

    assert seed_claude_trust_file(resolve_claude_config_path(env), project_dir) is True
    assert seed_claude_settings_file(resolve_claude_settings_path(env)) is True

    claude_json = json.loads((cfg_dir / ".claude.json").read_text())
    assert claude_json["hasCompletedOnboarding"] is True
    assert claude_json["bypassPermissionsModeAccepted"] is True
    assert claude_json["projects"][str(project_dir.resolve())][
        "hasCompletedProjectOnboarding"
    ] is True

    settings = json.loads((cfg_dir / "settings.json").read_text())
    assert settings["skipDangerousModePermissionPrompt"] is True
