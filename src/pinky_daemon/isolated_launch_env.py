"""Names-only shadow policy for isolated launch environments.

This observes the current daemon environment, not a tmux server's historical
environment. It never changes a launch payload or enforces an inheritance policy.
"""

from __future__ import annotations

import json
import os
from collections.abc import Callable, Iterable

SHADOW_ENV = "PINKY_ISOLATED_ENV_SHADOW"
BASE_ALLOWLIST = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "TERM", "LANG", "TZ", "TMPDIR",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    "SSL_CERT_FILE", "SSL_CERT_DIR", "NODE_EXTRA_CA_CERTS", "REQUESTS_CA_BUNDLE",
})
DAEMON_ONLY = frozenset({"PINKY_SESSION_SECRET", "PINKYBOT_FERRY_SHARED_SECRET"})


def shadow_enabled() -> bool:
    return os.environ.get(SHADOW_ENV) == "1"


def isolation_status(registry, agent_name: str) -> str:
    """Preserve the launch gate's tri-state lookup and non-local mode coupling."""
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


def report_shadow(
    *, agent_name: str, status: str, has_agent_key: bool,
    explicit_names: Iterable[str], log: Callable[[str], None],
) -> None:
    """Predict dropped inherited names; explicit daemon-only names still lose."""
    if not shadow_enabled():
        return
    if not (status == "isolated" or (status == "unknown" and has_agent_key)):
        return
    explicit = set(explicit_names)
    dropped = sorted(
        name for name in os.environ
        if name in DAEMON_ONLY or (
            name not in BASE_ALLOWLIST and not name.startswith(("LC_", "XDG_"))
            and name not in explicit
        )
    )
    # JSON escapes names and agent identity, including control characters. No
    # environment values are inspected or interpolated into the diagnostic.
    log("isolated_launch_env_shadow " + json.dumps({
        "agent": agent_name, "source": "daemon", "isolation": status,
        "enforced": False, "would_drop_count": len(dropped), "would_drop_names": dropped,
    }, ensure_ascii=True, separators=(",", ":")))


def report_codex_shadow(
    *, agent_name: str, registry, status_lookup: Callable[[], str],
    env: dict[str, str], log: Callable[[str], None],
) -> None:
    """Model the scoped payload, not today's full ambient Codex payload."""
    if not shadow_enabled():
        return
    has_key = False
    if registry and agent_name:
        try:
            has_key = bool((registry.get_signing_key(agent_name) or "").strip())
        except Exception:
            pass
    # These names are configured by the Codex builders/home resolver. A future
    # grant supplies additional explicit names; ambient parity is not a grant.
    explicit = {"PINKY_AGENT_NAME", "OPENAI_API_KEY", "CODEX_HOME"} & env.keys()
    if has_key:
        explicit.add("PINKY_AGENT_KEY")
    report_shadow(
        agent_name=agent_name, status=status_lookup(), has_agent_key=has_key,
        explicit_names=explicit, log=log,
    )
