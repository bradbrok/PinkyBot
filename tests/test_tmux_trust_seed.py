"""Tests for the Claude Code first-run trust pre-seed (#112).

Covers the two pure helpers wired into ``_spawn_tmux_repl``:
- ``_resolve_claude_config_path`` — CLI-matching config-path resolution.
- ``_seed_claude_trust_file`` — idempotent, atomic, non-clobbering seed of
  the trust/bypass flags that ``--dangerously-skip-permissions`` does NOT
  auto-accept (the wedge a fresh tmux agent hit on a cold box).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pinky_daemon.tmux_session import (
    _resolve_claude_config_path,
    _seed_claude_trust_file,
)

# ── _resolve_claude_config_path ──────────────────────────────────────────


def test_resolve_honors_claude_config_dir():
    p = _resolve_claude_config_path({"CLAUDE_CONFIG_DIR": "/tmp/cfg", "HOME": "/home/x"})
    assert p == Path("/tmp/cfg/.claude.json")


def test_resolve_falls_back_to_home_when_no_config_dir():
    p = _resolve_claude_config_path({"HOME": "/home/x"})
    assert p == Path("/home/x/.claude.json")


def test_resolve_blank_config_dir_falls_back_to_home():
    p = _resolve_claude_config_path({"CLAUDE_CONFIG_DIR": "  ", "HOME": "/home/x"})
    assert p == Path("/home/x/.claude.json")


# ── _seed_claude_trust_file ──────────────────────────────────────────────


def test_seed_creates_missing_file(tmp_path: Path):
    cfg = tmp_path / ".claude.json"
    proj = tmp_path / "agents" / "angel"
    proj.mkdir(parents=True)

    wrote = _seed_claude_trust_file(cfg, str(proj))

    assert wrote is True
    assert cfg.exists()
    data = json.loads(cfg.read_text())
    assert data["bypassPermissionsModeAccepted"] is True
    assert data["hasCompletedOnboarding"] is True
    entry = data["projects"][str(proj.resolve())]
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["hasCompletedProjectOnboarding"] is True


def test_seed_is_idempotent(tmp_path: Path):
    cfg = tmp_path / ".claude.json"
    proj = tmp_path / "proj"
    proj.mkdir()

    assert _seed_claude_trust_file(cfg, str(proj)) is True
    # Second call: every flag already set → no write, returns False.
    assert _seed_claude_trust_file(cfg, str(proj)) is False


def test_seed_preserves_existing_keys_and_other_projects(tmp_path: Path):
    cfg = tmp_path / ".claude.json"
    other = "/some/other/project"
    cfg.write_text(
        json.dumps(
            {
                "oauthAccount": {"token": "SECRET-DO-NOT-LOSE"},
                "numStartups": 7,
                "projects": {
                    other: {"hasTrustDialogAccepted": True, "lastCost": 0.42},
                },
            }
        )
    )
    proj = tmp_path / "proj"
    proj.mkdir()

    wrote = _seed_claude_trust_file(cfg, str(proj))

    assert wrote is True
    data = json.loads(cfg.read_text())
    # Untouched pre-existing data survives.
    assert data["oauthAccount"] == {"token": "SECRET-DO-NOT-LOSE"}
    assert data["numStartups"] == 7
    assert data["projects"][other] == {"hasTrustDialogAccepted": True, "lastCost": 0.42}
    # New project seeded alongside.
    assert data["bypassPermissionsModeAccepted"] is True
    assert data["projects"][str(proj.resolve())]["hasTrustDialogAccepted"] is True


def test_seed_completes_partial_flags(tmp_path: Path):
    cfg = tmp_path / ".claude.json"
    proj = tmp_path / "proj"
    proj.mkdir()
    # Top-level bypass already accepted, but global onboarding and this
    # project trust were missing.
    cfg.write_text(json.dumps({"bypassPermissionsModeAccepted": True, "projects": {}}))

    wrote = _seed_claude_trust_file(cfg, str(proj))

    assert wrote is True  # project flags were missing
    data = json.loads(cfg.read_text())
    assert data["hasCompletedOnboarding"] is True
    entry = data["projects"][str(proj.resolve())]
    assert entry["hasTrustDialogAccepted"] is True
    assert entry["hasCompletedProjectOnboarding"] is True


def test_seed_corrupt_file_raises_and_does_not_overwrite(tmp_path: Path):
    cfg = tmp_path / ".claude.json"
    cfg.write_text("{ this is not valid json ")

    with pytest.raises(json.JSONDecodeError):
        _seed_claude_trust_file(cfg, str(tmp_path / "proj"))

    # The corrupt file is left exactly as-is — we never clobber a file
    # that might hold (unparseable but recoverable) oauth creds.
    assert cfg.read_text() == "{ this is not valid json "


def test_seed_non_object_root_raises(tmp_path: Path):
    cfg = tmp_path / ".claude.json"
    cfg.write_text(json.dumps(["unexpected", "array"]))

    with pytest.raises(ValueError):
        _seed_claude_trust_file(cfg, str(tmp_path / "proj"))


def test_seed_no_temp_file_left_behind(tmp_path: Path):
    cfg = tmp_path / ".claude.json"
    proj = tmp_path / "proj"
    proj.mkdir()

    _seed_claude_trust_file(cfg, str(proj))

    leftovers = list(tmp_path.glob(".claude.json.pinky-seed*"))
    assert leftovers == []
