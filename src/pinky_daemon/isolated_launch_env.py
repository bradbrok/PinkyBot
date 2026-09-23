"""Isolated launch policy snapshots, exact-name grants and names-only reports."""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field

from pinky_daemon.tmux_launch_env_loader import (
    BASE_ALLOWLIST as BASE_ALLOWLIST,
)
from pinky_daemon.tmux_launch_env_loader import (
    DAEMON_ONLY as DAEMON_ONLY,
)
from pinky_daemon.tmux_launch_env_loader import (
    _private_regular,
    key_policy,
)

MODE_ENV = "PINKY_ISOLATED_ENV"
GRANTS_FILE_ENV = "PINKY_ISOLATED_ENV_GRANTS_FILE"
_MAX_GRANTS_BYTES = 65536


class LaunchConfigError(ValueError):
    """An isolated launch has invalid operator policy configuration."""


class LaunchEnvError(PermissionError):
    """An explicit launch payload violates the isolated environment policy."""


@dataclass(frozen=True)
class LaunchPolicy:
    mode: str = "off"
    status: str = "unknown"
    agent_key: str = field(default="", repr=False)
    grants: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return self.mode == "enforce" and is_isolated(self.status, bool(self.agent_key))

    def spawn_options(self, env: dict[str, str]) -> dict:
        if not self.clean:
            return {}
        return {"inherit": "none", "granted": tuple(k for k in self.grants if k in env)}


def is_isolated(status: str, has_key: bool) -> bool:
    return status == "isolated" or (status == "unknown" and has_key)


def shadow_enabled() -> bool:
    return os.environ.get(MODE_ENV, "off") == "shadow"


def isolation_status(registry, agent_name: str) -> str:
    """Use the existing tri-state lookup and non-local mode coupling."""
    if not registry or not agent_name:
        return "unknown"
    try:
        agent = registry.get(agent_name)
    except Exception:
        return "unknown"
    if agent is None:
        return "unknown"
    if getattr(agent, "isolation_mode", "local") not in ("", "local"):
        return "isolated"
    return "isolated" if getattr(agent, "isolated", False) else "not_isolated"


def signing_key(registry, agent_name: str) -> str:
    if registry and agent_name:
        try:
            return (registry.get_signing_key(agent_name) or "").strip()
        except Exception:
            pass
    return ""


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate grant entry")
        result[key] = value
    return result


def _load_grants(registry, agent_name: str, log: Callable[[str], None]) -> tuple[str, ...]:
    try:
        path = os.environ.get(GRANTS_FILE_ENV)
        if not path:
            raise ValueError("missing grant file")
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
        try:
            info = os.fstat(fd)
            if not _private_regular(info) or info.st_size > _MAX_GRANTS_BYTES:
                raise ValueError("unsafe grant file")
            with os.fdopen(fd, "rb", closefd=False) as stream:
                raw = stream.read(_MAX_GRANTS_BYTES + 1)
            if len(raw) > _MAX_GRANTS_BYTES:
                raise ValueError("oversized grant file")
        finally:
            os.close(fd)
        config = json.loads(raw, object_pairs_hook=_unique_object)
        if not isinstance(config, dict):
            raise ValueError("invalid grant mapping")
        for agent, names in config.items():
            if not isinstance(agent, str) or not agent or not isinstance(names, list):
                raise ValueError("invalid grant entry")
            if any(char in agent for char in "*?[]"):
                raise ValueError("wildcard grant principal")
            if any(not isinstance(k, str) or key_policy(k) or k in DAEMON_ONLY for k in names):
                raise ValueError("invalid grant name")
            if len(names) != len(set(names)):
                raise ValueError("duplicate grant name")
    except (OSError, ValueError, TypeError, RecursionError):
        log("ERROR isolated launch grant configuration refused")
        raise LaunchConfigError("isolated launch grant configuration refused") from None
    selected = ()
    for agent, names in config.items():
        try:
            known = registry.get(agent) if registry else None
        except Exception:
            known = None
        if known is None:
            log("isolated launch grants ignored for unknown agent " + json.dumps(agent))
        elif agent == agent_name:
            selected = tuple(names)
    return selected


def capture_policy(
    *, agent_name: str, registry, status_lookup: Callable[[], str], log: Callable[[str], None],
) -> LaunchPolicy:
    mode = os.environ.get(MODE_ENV, "off")
    if mode in ("off", "shadow"):
        return LaunchPolicy(mode=mode)
    status = status_lookup()
    key = signing_key(registry, agent_name)
    isolated = is_isolated(status, bool(key))
    if mode != "enforce":
        if isolated:
            log("ERROR isolated launch mode configuration refused")
            raise LaunchConfigError("isolated launch mode configuration refused")
        return LaunchPolicy()
    grants = _load_grants(registry, agent_name, log) if isolated else ()
    return LaunchPolicy(mode=mode, status=status, agent_key=key, grants=grants)


def with_grants(policy: LaunchPolicy, explicit: dict[str, str]) -> dict[str, str]:
    if not policy.clean:
        return explicit
    env = {k: os.environ[k] for k in policy.grants if k in os.environ}
    env.update(explicit)
    # An unavailable scoped key must not be replaced by an ambient grant.
    if not policy.agent_key:
        env.pop("PINKY_AGENT_KEY", None)
    return env


def scoped_codex_env(policy: LaunchPolicy, agent_name: str) -> dict[str, str]:
    env = {"PINKY_AGENT_NAME": agent_name}
    if policy.agent_key:
        env["PINKY_AGENT_KEY"] = policy.agent_key
    for key in ("PINKY_DAEMON_URL", "PINKY_TOOL_POLICY"):
        if key in os.environ:
            env[key] = os.environ[key]
    return env


def report_shadow(
    *, agent_name: str, status: str, has_agent_key: bool,
    explicit_names: Iterable[str], log: Callable[[str], None],
) -> None:
    """Predict name changes from the current daemon environment."""
    if not shadow_enabled():
        return
    if not is_isolated(status, has_agent_key):
        return
    explicit = set(explicit_names)
    dropped = sorted(
        name for name in os.environ
        if name in DAEMON_ONLY or (
            name not in BASE_ALLOWLIST and not name.startswith(("LC_", "XDG_"))
            and name not in explicit
        )
    )
    log("isolated_launch_env_shadow " + json.dumps({
        "agent": agent_name, "source": "daemon", "isolation": status,
        "enforced": False, "would_drop_count": len(dropped), "would_drop_names": dropped,
    }, ensure_ascii=True, separators=(",", ":")))


def report_codex_shadow(
    *, agent_name: str, registry, status_lookup: Callable[[], str],
    env: dict[str, str], log: Callable[[str], None],
) -> None:
    if not shadow_enabled():
        return
    has_key = bool(signing_key(registry, agent_name))
    explicit = {"PINKY_AGENT_NAME", "OPENAI_API_KEY", "CODEX_HOME"} & env.keys()
    if has_key:
        explicit.add("PINKY_AGENT_KEY")
    report_shadow(
        agent_name=agent_name, status=status_lookup(), has_agent_key=has_key,
        explicit_names=explicit, log=log,
    )
