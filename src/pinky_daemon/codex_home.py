"""Flag-gated per-agent Codex home preparation and rollout migration."""

from __future__ import annotations

import json
import os
import shutil
from collections.abc import Callable
from pathlib import Path

PER_AGENT_CODEX_HOME_ENV = "PINKY_CODEX_PER_AGENT_HOME"
MANAGED_CONFIG_SENTINEL = "# pinkybot-managed-codex-home-v1"

LogFn = Callable[[str], None]


def per_agent_codex_home_enabled() -> bool:
    """Return whether per-agent Codex homes are explicitly enabled."""
    return os.environ.get(PER_AGENT_CODEX_HOME_ENV, "").strip() == "1"


def shared_codex_home() -> Path:
    """Return the user-level Codex home used when isolation is disabled."""
    return Path(os.environ.get("CODEX_HOME") or (Path.home() / ".codex"))


def _agent_working_dir(agent: object | None) -> Path:
    raw = (getattr(agent, "working_dir", "") or "").strip()
    if not raw:
        raise RuntimeError("per-agent Codex home requires an agent working directory")
    return Path(raw).expanduser().resolve()


def codex_home_for(agent: object | None = None) -> Path:
    """Resolve the Codex home shared by every reader for an agent scope.

    The feature is a strict production no-op while disabled: the daemon-level
    ``CODEX_HOME`` (or Codex's normal user-home fallback) is returned exactly as
    before. When enabled, an explicit agent override wins; otherwise the home
    lives inside the resolved agent working directory.
    """
    if not per_agent_codex_home_enabled():
        return shared_codex_home()

    override = (getattr(agent, "codex_home", "") or "").strip()
    if override:
        return Path(override).expanduser().resolve()
    return _agent_working_dir(agent) / ".codex"


def _managed_config(working_dir: Path) -> str:
    project_key = json.dumps(str(working_dir))
    return (
        f"{MANAGED_CONFIG_SENTINEL}\n"
        "# Generated for an isolated agent session.\n\n"
        "[features]\n"
        "apps = false\n"
        "plugins = false\n\n"
        f"[projects.{project_key}]\n"
        'trust_level = "trusted"\n'
    )


def _write_managed_config(codex_home: Path, working_dir: Path, log: LogFn) -> None:
    config_path = codex_home / "config.toml"
    expected = _managed_config(working_dir)
    if config_path.exists():
        if config_path.is_symlink() or config_path.stat().st_nlink > 1:
            log(
                f"codex-home: preserving linked config at {config_path}; "
                "per-agent safety settings were not rewritten"
            )
            return
        existing = config_path.read_text(encoding="utf-8")
        if not existing.startswith(MANAGED_CONFIG_SENTINEL):
            log(
                f"codex-home: preserving unmanaged config at {config_path}; "
                "per-agent safety settings were not rewritten"
            )
            return
        if existing == expected:
            return
    config_path.write_text(expected, encoding="utf-8")
    config_path.chmod(0o600)


def _ensure_auth_link(codex_home: Path, auth_source: Path) -> None:
    auth_path = codex_home / "auth.json"
    if auth_path.is_symlink():
        try:
            if auth_path.resolve(strict=True) == auth_source.resolve(strict=True):
                return
        except OSError:
            pass
        auth_path.unlink()
    elif auth_path.exists():
        raise RuntimeError(
            f"refusing Codex spawn: {auth_path} is not the managed auth symlink"
        )
    auth_path.symlink_to(auth_source.resolve(strict=True))


def _rollout_cwd(path: Path) -> str:
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                try:
                    entry = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if entry.get("type") != "session_meta":
                    continue
                payload = entry.get("payload") or {}
                cwd = payload.get("cwd")
                return cwd if isinstance(cwd, str) else ""
    except OSError:
        return ""
    return ""


def move_matching_rollouts(
    source_home: Path,
    destination_home: Path,
    working_dir: str | Path,
    *,
    log: LogFn,
) -> int:
    """Move rollouts for one resolved working directory between Codex homes."""
    source_sessions = source_home / "sessions"
    destination_sessions = destination_home / "sessions"
    target_cwd = os.path.realpath(str(working_dir))
    if not source_sessions.exists():
        return 0

    moved = 0
    for source in source_sessions.glob("**/rollout-*.jsonl"):
        rollout_cwd = _rollout_cwd(source)
        if not rollout_cwd or os.path.realpath(rollout_cwd) != target_cwd:
            continue
        relative = source.relative_to(source_sessions)
        destination = destination_sessions / relative
        if destination.exists():
            log(
                f"codex-home: rollout migration collision at {destination}; "
                f"leaving {source} in place"
            )
            continue
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source), str(destination))
        moved += 1
    return moved


def prepare_agent_codex_home(agent: object, *, log: LogFn) -> Path:
    """Prepare the isolated home before a Codex process is spawned.

    With the flag disabled this function performs no filesystem work. With it
    enabled, auth absence is a hard error before launch, config generation is
    non-destructive, and cwd-matched rollouts move into the isolated store.
    """
    codex_home = codex_home_for(agent)
    if not per_agent_codex_home_enabled():
        return codex_home

    working_dir = _agent_working_dir(agent)
    shared_home = shared_codex_home().expanduser().resolve()
    resolved_home = codex_home.expanduser().resolve()
    if resolved_home == shared_home:
        raise RuntimeError(
            "refusing Codex spawn: per-agent CODEX_HOME resolves to the shared home"
        )

    auth_source = shared_home / "auth.json"
    if not auth_source.is_file() or not os.access(auth_source, os.R_OK):
        raise RuntimeError(
            f"refusing Codex spawn: shared auth file is absent or unreadable: {auth_source}"
        )

    resolved_home.mkdir(parents=True, exist_ok=True)
    resolved_home.chmod(0o700)
    sessions = resolved_home / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    sessions.chmod(0o700)
    _write_managed_config(resolved_home, working_dir, log)
    _ensure_auth_link(resolved_home, auth_source)

    moved = move_matching_rollouts(
        shared_home,
        resolved_home,
        working_dir,
        log=log,
    )
    log(
        f"codex-home: migrated {moved} cwd-matched rollout(s) into "
        f"{resolved_home / 'sessions'}"
    )
    return resolved_home


def rollback_agent_rollouts(
    working_dir: str | Path,
    agent_home: str | Path,
    *,
    shared_home: str | Path | None = None,
    log: LogFn,
) -> int:
    """Move one agent's rollouts back to the user-level Codex store."""
    source = Path(agent_home).expanduser().resolve()
    destination = (
        Path(shared_home).expanduser().resolve()
        if shared_home is not None
        else shared_codex_home().expanduser().resolve()
    )
    moved = move_matching_rollouts(
        source,
        destination,
        working_dir,
        log=log,
    )
    log(
        f"codex-home: rolled back {moved} cwd-matched rollout(s) into "
        f"{destination / 'sessions'}"
    )
    return moved
