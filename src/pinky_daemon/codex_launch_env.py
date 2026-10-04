"""Shared Codex launch ownership, scoped candidates and inherited-name removal."""

from __future__ import annotations

import json
import os
import shlex
from dataclasses import replace

from pinky_daemon import isolated_launch_env, launch_env_authority, tmux_launch_env
from pinky_daemon.codex_home import codex_home_for, per_agent_codex_home_enabled
from pinky_daemon.codex_mcp_env import with_mcp_header_env
from pinky_daemon.tmux_launch_env_loader import is_base_name

BUILDER_OWNED = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_BASE_URL",
        "CODEX_HOME",
        "PINKY_AGENT_NAME",
        "PINKY_AGENT_KEY",
        "PINKY_DAEMON_URL",
        "PINKY_TOOL_POLICY",
    }
)


def capture_policy(*, agent_name, registry, log):
    return isolated_launch_env.capture_policy(
        agent_name=agent_name,
        registry=registry,
        status_lookup=lambda: isolated_launch_env.isolation_status(registry, agent_name),
        log=log,
        minimum_shadow=True,
    )


def build_env(
    *,
    agent_name,
    config,
    api_key,
    policy,
    log,
    servers=None,
    prepared_home=None,
    daemon_url=None,
    report=True,
):
    """Keep ambient compatibility while making every owned output explicit."""
    ambient = tmux_launch_env.ambient_env(os.environ.items(), log)
    explicit = {"PINKY_AGENT_NAME": agent_name}
    if prepared_home is not None or policy.clean or per_agent_codex_home_enabled():
        explicit["CODEX_HOME"] = str(
            prepared_home if prepared_home is not None else codex_home_for(config)
        )
    elif "CODEX_HOME" in ambient:
        # Preserve an explicit shared-home input, including deliberate empty values.
        explicit["CODEX_HOME"] = ambient["CODEX_HOME"]
    if policy.agent_key:
        explicit["PINKY_AGENT_KEY"] = policy.agent_key
    if api_key:
        explicit["OPENAI_API_KEY"] = api_key
    provider_url = (getattr(config, "provider_url", "") or "").strip()
    if provider_url == "codex_cli":
        provider_url = ""
    base_url = provider_url or ambient.get("OPENAI_BASE_URL", "")
    if base_url and base_url != "codex_cli":
        explicit["OPENAI_BASE_URL"] = base_url
    for name in ("PINKY_DAEMON_URL", "PINKY_TOOL_POLICY"):
        if name in ambient:
            explicit[name] = ambient[name]
    if daemon_url is not None:
        explicit["PINKY_DAEMON_URL"] = daemon_url

    inherited = launch_env_authority.filter_env(ambient, BUILDER_OWNED)
    baseline = {
        name: value
        for name, value in inherited.items()
        if is_base_name(name)
    }
    grants = isolated_launch_env.with_grants(replace(policy, mode="enforce"), {})
    grants = launch_env_authority.filter_env(grants, BUILDER_OWNED)
    scoped = with_mcp_header_env({**baseline, **grants, **explicit}, servers or {})
    env = scoped if policy.clean else with_mcp_header_env({**inherited, **explicit}, servers or {})
    if (
        report
        and policy.mode == "shadow"
        and isolated_launch_env.is_isolated(
            policy.status,
            bool(policy.agent_key),
        )
    ):
        dropped = sorted(set(os.environ) - scoped.keys())
        log(
            "isolated_launch_env_shadow "
            + json.dumps(
                {
                    "agent": agent_name,
                    "source": "daemon",
                    "isolation": policy.status,
                    "enforced": False,
                    "would_drop_count": len(dropped),
                    "would_drop_names": dropped,
                    "granted_names": list(policy.grants),
                },
                ensure_ascii=True,
                separators=(",", ":"),
            )
        )
    return env


def wrap_command(command: str, env: dict[str, str]) -> str:
    """Run after the pane shell starts; values stay in the private JSON payload."""
    absent = launch_env_authority.absent_names(env, BUILDER_OWNED)
    args = ["env"]
    for name in sorted(absent):
        args.extend(["-u", name])
    return shlex.join(args) + " " + command
