"""Run pinky-zoho MCP server standalone."""

from __future__ import annotations

import argparse
import os
import sys

from .client import resolve_secret


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky Zoho MCP Server")
    parser.add_argument(
        "--agent",
        default=os.environ.get("PINKY_AGENT_NAME", ""),
        help="Agent name (or set PINKY_AGENT_NAME env var)",
    )
    parser.add_argument(
        "--api-url",
        default="",
        help="Zoho API URL. Falls back to ZOHO_API_URL, then LAN hosts.",
    )
    args = parser.parse_args()

    if not args.agent:
        print("Error: --agent is required", file=sys.stderr)
        sys.exit(1)

    secret = resolve_secret()
    if not secret:
        print("Error: ZOHO_API_SECRET or PINKY_SESSION_SECRET is required", file=sys.stderr)
        sys.exit(1)

    from .server import create_server

    server = create_server(agent_name=args.agent, api_url=args.api_url, secret=secret)
    target = args.api_url or os.environ.get("ZOHO_API_URL") or "LAN fallback"
    print(f"[pinky-zoho] {args.agent} -> {target}", file=sys.stderr)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
