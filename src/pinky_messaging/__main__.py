"""Run pinky-messaging MCP server standalone."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky Messaging MCP Server")
    parser.add_argument(
        "--agent",
        default=os.environ.get("PINKY_AGENT_NAME", ""),
        help="Agent name (or set PINKY_AGENT_NAME env var)",
    )
    parser.add_argument(
        "--api-url",
        default=os.environ.get("PINKY_API_URL", "http://localhost:8888"),
        help="PinkyBot API URL (or set PINKY_API_URL env var)",
    )
    args = parser.parse_args()

    if not args.agent:
        print("Error: --agent is required", file=sys.stderr)
        sys.exit(1)

    from pinky_messaging.server import create_server

    server = create_server(agent_name=args.agent, api_url=args.api_url)
    print(f"[pinky-messaging] {args.agent} -> {args.api_url}", file=sys.stderr)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
