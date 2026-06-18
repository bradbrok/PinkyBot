"""Shared Claude Code config-dir helpers.

This module is deliberately dependency-light so tmux, SDK streaming, dreams,
and API validation can all use the same path resolver without import cycles.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import threading
from collections.abc import Mapping
from pathlib import Path

PER_AGENT_CLAUDE_CONFIG_ENV = "PINKY_PER_AGENT_CLAUDE_CONFIG"
PER_AGENT_CLAUDE_CONFIG_DIRNAME = ".claude-config"

# Serializes read-modify-write of ``.claude.json`` across concurrent local
# launches so two simultaneous seeds cannot drop each other's project entry.
_CLAUDE_JSON_SEED_LOCK = threading.Lock()


def env_flag_enabled(
    name: str,
    env: Mapping[str, str] | None = None,
    *,
    default: str = "0",
) -> bool:
    e = env if env is not None else os.environ
    return str(e.get(name, default)).strip().lower() in ("1", "true", "yes", "on")


def claude_project_slug(project_dir: str | Path) -> str:
    """Return Claude Code's ``projects/`` slug for an absolute cwd."""
    cwd = Path(project_dir).resolve()
    return re.sub(r"[^a-zA-Z0-9]", "-", str(cwd))


def resolve_claude_config_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve Claude Code's global ``.claude.json`` path.

    Mirrors the CLI: ``$CLAUDE_CONFIG_DIR/.claude.json`` when set, otherwise
    ``$HOME/.claude.json``.
    """
    e = env if env is not None else os.environ
    cfg_dir = (e.get("CLAUDE_CONFIG_DIR") or "").strip()
    base = Path(cfg_dir) if cfg_dir else Path(e.get("HOME") or Path.home())
    return base / ".claude.json"


def resolve_claude_settings_path(env: Mapping[str, str] | None = None) -> Path:
    """Resolve the settings file that carries first-run prompt suppressors."""
    e = env if env is not None else os.environ
    cfg_dir = (e.get("CLAUDE_CONFIG_DIR") or "").strip()
    if cfg_dir:
        return Path(cfg_dir) / "settings.json"
    home = Path(e.get("HOME") or Path.home())
    return home / ".claude" / "settings.json"


def resolve_agent_claude_config_dir(
    agent: object,
    *,
    working_dir: str | Path | None = None,
    env: Mapping[str, str] | None = None,
) -> Path | None:
    """Return this local agent's per-agent ``CLAUDE_CONFIG_DIR`` or ``None``.

    Guardrails:
    - flag-gated and default-off;
    - local-runtime agents only (container/unix_user provisioning owns its own
      config dir);
    - static-token fail-safe: without ``CLAUDE_CODE_OAUTH_TOKEN`` present at
      resolve time, skip the per-agent dir rather than booting into a login wall.
    """
    e = env if env is not None else os.environ
    if not env_flag_enabled(PER_AGENT_CLAUDE_CONFIG_ENV, e):
        return None
    if agent is None:
        return None
    if getattr(agent, "isolation_mode", "local") not in ("", "local"):
        return None
    if not (e.get("CLAUDE_CODE_OAUTH_TOKEN") or "").strip():
        return None

    raw_wd = working_dir if working_dir is not None else getattr(agent, "working_dir", "")
    wd = str(raw_wd or "").strip()
    if not wd:
        return None
    return Path(wd).resolve() / PER_AGENT_CLAUDE_CONFIG_DIRNAME


def migrate_claude_project_history(project_dir: str | Path, config_dir: Path) -> bool:
    """Copy the existing global transcript project dir into ``config_dir``.

    Idempotent and non-clobbering: if the destination project dir already
    exists, leave it alone. The slug remains derived from the project cwd.
    """
    slug = claude_project_slug(project_dir)
    src = Path.home() / ".claude" / "projects" / slug
    dst = config_dir / "projects" / slug
    if not src.exists() or dst.exists():
        return False
    dst.parent.mkdir(parents=True, exist_ok=True)
    tmp = dst.parent / f".{dst.name}.pinky-copy.{os.getpid()}.tmp"
    if tmp.exists():
        shutil.rmtree(tmp)
    try:
        shutil.copytree(src, tmp)
        if dst.exists():
            shutil.rmtree(tmp, ignore_errors=True)
            return False
        os.replace(tmp, dst)
    except Exception:
        shutil.rmtree(tmp, ignore_errors=True)
        raise
    return True


def seed_claude_trust_file(config_path: Path, project_dir: str | Path) -> bool:
    """Idempotently pre-seed Claude Code first-run trust/onboarding flags."""
    proj_key = str(Path(project_dir).resolve())
    with _CLAUDE_JSON_SEED_LOCK:
        data: dict = {}
        if config_path.exists():
            with config_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(
                    f"{config_path} root is not a JSON object "
                    f"(got {type(data).__name__}) -- refusing to overwrite"
                )

        changed = False
        for flag in ("bypassPermissionsModeAccepted", "hasCompletedOnboarding"):
            if data.get(flag) is not True:
                data[flag] = True
                changed = True

        projects = data.setdefault("projects", {})
        if not isinstance(projects, dict):
            raise ValueError(f"{config_path} 'projects' is not an object")
        proj = projects.setdefault(proj_key, {})
        if not isinstance(proj, dict):
            raise ValueError(f"{config_path} projects[{proj_key!r}] is not an object")
        for flag in ("hasTrustDialogAccepted", "hasCompletedProjectOnboarding"):
            if proj.get(flag) is not True:
                proj[flag] = True
                changed = True

        if changed:
            config_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = config_path.parent / f".claude.json.pinky-seed.{os.getpid()}.tmp"
            with tmp.open("w", encoding="utf-8") as f:
                json.dump(data, f, indent=2)
            os.replace(tmp, config_path)
        return changed


def seed_claude_settings_file(settings_path: Path) -> bool:
    """Seed the settings flag that suppresses the dangerous-mode prompt."""
    with _CLAUDE_JSON_SEED_LOCK:
        data: dict = {}
        if settings_path.exists():
            with settings_path.open("r", encoding="utf-8") as f:
                data = json.load(f)
            if not isinstance(data, dict):
                raise ValueError(
                    f"{settings_path} root is not a JSON object "
                    f"(got {type(data).__name__}) -- refusing to overwrite"
                )
        if data.get("skipDangerousModePermissionPrompt") is True:
            return False
        data["skipDangerousModePermissionPrompt"] = True
        settings_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = settings_path.parent / f".claude-settings.pinky-seed.{os.getpid()}.tmp"
        with tmp.open("w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, settings_path)
        return True
