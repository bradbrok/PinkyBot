"""pinky serve -- start MCP servers."""

from __future__ import annotations

import os
import subprocess
import sys


def run_serve(server: str = "all") -> None:
    """Start one or more MCP servers.

    Args:
        server: Which server to start: memory, outreach, or all.
    """
    config_path = os.environ.get("PINKY_CONFIG", "pinky.yaml")

    print("[pinky] Starting MCP servers...")
    print(f"[pinky] Config: {config_path}")

    if server == "memory":
        from pinky_memory.__main__ import main as memory_main
        print("[pinky] Starting memory server on stdio...")
        # Override sys.argv so the server's own arg parser doesn't see ours
        sys.argv = ["pinky-memory"]
        memory_main()
    elif server == "outreach":
        from pinky_outreach.__main__ import main as outreach_main
        print("[pinky] Starting outreach server on stdio...")
        sys.argv = ["pinky-outreach"]
        outreach_main()
    elif server == "all":
        print("[pinky] Starting memory and outreach servers...")
        procs = [
            subprocess.Popen([sys.executable, "-m", "pinky_memory"]),
            subprocess.Popen([sys.executable, "-m", "pinky_outreach"]),
        ]
        try:
            for proc in procs:
                proc.wait()
        except KeyboardInterrupt:
            for proc in procs:
                proc.terminate()
            for proc in procs:
                proc.wait()
    else:
        print(f"[pinky] Unknown server: {server}. Options: memory, outreach, all")
