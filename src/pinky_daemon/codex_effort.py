"""Resolve explicit Codex reasoning effort without changing native levels."""

from __future__ import annotations

from pinky_daemon.effort import resolve_cli_effort


def resolve_codex_effort(level: str | None) -> str | None:
    """Omit adaptive settings and resolve aliases; Codex validates capabilities."""
    if level in (None, "", "auto"):
        return None
    return resolve_cli_effort(level)
