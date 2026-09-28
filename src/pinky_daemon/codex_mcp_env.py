"""Codex MCP configuration with header values confined to process environments."""

from __future__ import annotations

import json
import re

_HEADER_ENV_PREFIX = "PINKY_MCP_HDR_"


def _toml_key(name: str) -> str:
    return name if re.fullmatch(r"[A-Za-z0-9_-]+", name) else json.dumps(name)


def mcp_cli_config(servers: dict) -> tuple[list[str], dict[str, str]]:
    """Return CLI overrides referencing names, and their private header values.

    Server/header punctuation and case can normalize to the same environment
    name, including across the separator. Refuse the entire configuration on
    collision, even when the values match; diagnostics never include values.
    """
    args: list[str] = []
    env: dict[str, str] = {}
    for server, config in servers.items():
        if not isinstance(config, dict) or not config.get("url"):
            continue
        if not isinstance(server, str) or re.fullmatch(r"[A-Za-z0-9_-]+", server) is None:
            raise ValueError("unsupported MCP server name for CLI override")
        prefix = f"mcp_servers.{server}"
        args.extend(["-c", f"{prefix}.url={json.dumps(config['url'])}"])
        header_refs = []
        for header, value in (config.get("headers") or {}).items():
            env_name = _HEADER_ENV_PREFIX + re.sub(r"[^A-Z0-9]", "_", f"{server}_{header}".upper())
            if env_name in env:
                raise ValueError(f"MCP header environment name collision: {env_name}")
            env[env_name] = str(value)
            header_refs.append(f"{_toml_key(header)}={json.dumps(env_name)}")
        if header_refs:
            # CLI dotted paths do not honor quoted segments. Keep header names in a TOML map.
            args.extend(["-c", f"{prefix}.env_http_headers={{" + ", ".join(header_refs) + "}"])
    return args, env


def with_mcp_header_env(env: dict[str, str], servers: dict) -> dict[str, str]:
    """Replace the reserved header namespace with only this launch's configuration."""
    _, headers = mcp_cli_config(servers)
    return {**{k: v for k, v in env.items() if not k.startswith(_HEADER_ENV_PREFIX)}, **headers}
