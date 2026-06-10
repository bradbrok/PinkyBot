"""pinky connect -- write Claude Code MCP config."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path


def run_connect() -> None:
    """Write MCP server config to the project's .mcp.json (read by Claude Code)."""
    project_dir = Path.cwd().resolve()
    mcp_file = project_dir / ".mcp.json"
    memory_db = project_dir / "data" / "memory.db"

    mcp_config = {
        "pinky-memory": {
            "command": sys.executable,
            "args": ["-m", "pinky_memory", "--db", str(memory_db)],
            "env": {
                "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
            },
        },
        "pinky-outreach": {
            "command": sys.executable,
            "args": ["-m", "pinky_outreach"],
            "env": {
                "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            },
        },
    }

    # Read existing config
    config: dict = {}
    if mcp_file.exists():
        try:
            config = json.loads(mcp_file.read_text())
        except json.JSONDecodeError as exc:
            print(f"[pinky] Error: {mcp_file} is not valid JSON: {exc}")
            print("[pinky] Fix or remove the file, then re-run 'pinky connect'.")
            sys.exit(1)

    # Merge MCP servers, preserving existing credentials when the env var is unset
    servers = config.setdefault("mcpServers", {})
    for name, entry in mcp_config.items():
        existing = servers.get(name)
        existing_env = existing.get("env", {}) if isinstance(existing, dict) else {}
        env = {}
        for key, value in entry["env"].items():
            if value:
                env[key] = value
            elif existing_env.get(key):
                env[key] = existing_env[key]
            else:
                print(f"[pinky] Warning: {key} is not set; {name} may not start without it.")
        entry["env"] = env
        servers[name] = entry

    # Write back atomically
    tmp_file = mcp_file.with_name(mcp_file.name + ".tmp")
    tmp_file.write_text(json.dumps(config, indent=2) + "\n")
    os.replace(tmp_file, mcp_file)

    print(f"[pinky] Updated {mcp_file}")
    print("[pinky] MCP servers configured:")
    for name in mcp_config:
        print(f"  - {name}")
    print()
    print(f"Run 'claude' from {project_dir} to start using Pinky with Claude Code.")
