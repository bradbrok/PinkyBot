"""pinky serve -- start MCP servers."""

from __future__ import annotations

import os
import sys


def run_serve(memory_only: bool = False) -> None:
    # Load config
    config_path = os.environ.get("PINKY_CONFIG", "pinky.yaml")

    print("[pinky] Starting MCP servers...")
    print(f"[pinky] Config: {config_path}")

    # For now, just start the memory server
    # TODO: Start outreach and google servers based on config
    from pinky_memory.__main__ import main as memory_main

    print("[pinky] Starting memory server on stdio...")
    memory_main()
