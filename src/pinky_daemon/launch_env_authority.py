"""Shared launch ownership rules; transport-specific resolution stays with callers."""

from collections.abc import Iterable, Mapping

from pinky_daemon.tmux_launch_env_loader import DAEMON_ONLY


def permitted(name: str, owned: Iterable[str] = ()) -> bool:
    """Inherited builder outputs and daemon authority are never candidates."""
    return name not in DAEMON_ONLY and name not in owned and not name.startswith("PINKY_MCP_HDR_")


def filter_env(env: Mapping[str, str], owned: Iterable[str] = ()) -> dict[str, str]:
    return {name: value for name, value in env.items() if permitted(name, owned)}


def absent_names(env: Mapping[str, str], owned: Iterable[str]) -> frozenset[str]:
    """Always remove daemon authority, even if a builder accidentally supplies it."""
    return DAEMON_ONLY | (frozenset(owned) - env.keys())
