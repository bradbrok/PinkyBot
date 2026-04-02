"""pinky serve -- start MCP servers."""

from __future__ import annotations

import os


def run_serve(server: str = "all") -> None:
    """Start one or more MCP servers.

    Args:
        server: Which server to start: memory, outreach, or all.
    """
    config_path = os.environ.get("PINKY_CONFIG", "pinky.yaml")

    print("[pinky] Starting MCP servers...")
    print(f"[pinky] Config: {config_path}")

    if server in ("memory", "all"):
        from pinky_memory.__main__ import main as memory_main
        print("[pinky] Starting memory server on stdio...")
        memory_main()
    elif server == "outreach":
        from pinky_outreach.__main__ import main as outreach_main
        print("[pinky] Starting outreach server on stdio...")
        outreach_main()
    else:
        print(f"[pinky] Unknown server: {server}. Options: memory, outreach, all")
