"""pinky connect -- write Claude Code MCP config."""

from __future__ import annotations

import json
import os
from pathlib import Path


def run_connect() -> None:
    """Write MCP server config to Claude Code settings."""
    home = Path.home()
    settings_dir = home / ".claude"
    settings_file = settings_dir / "settings.json"

    # Build MCP server configs
    project_dir = Path.cwd().resolve()
    memory_db = project_dir / "data" / "memory.db"

    mcp_config = {
        "pinky-memory": {
            "command": "python",
            "args": ["-m", "pinky_memory", "--db", str(memory_db)],
            "env": {
                "OPENAI_API_KEY": os.environ.get("OPENAI_API_KEY", ""),
            },
        },
        "pinky-outreach": {
            "command": "python",
            "args": ["-m", "pinky_outreach"],
            "env": {
                "TELEGRAM_BOT_TOKEN": os.environ.get("TELEGRAM_BOT_TOKEN", ""),
            },
        },
    }

    # Read existing settings
    settings = {}
    if settings_file.exists():
        settings = json.loads(settings_file.read_text())

    # Merge MCP servers
    if "mcpServers" not in settings:
        settings["mcpServers"] = {}

    settings["mcpServers"].update(mcp_config)

    # Write back
    settings_dir.mkdir(parents=True, exist_ok=True)
    settings_file.write_text(json.dumps(settings, indent=2) + "\n")

    print(f"[pinky] Updated {settings_file}")
    print("[pinky] MCP servers configured:")
    for name in mcp_config:
        print(f"  - {name}")
    print()
    print("Run 'claude' to start using Pinky with Claude Code.")
