"""Run pinky-outreach MCP server standalone."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky Outreach MCP Server")
    parser.add_argument(
        "--token",
        default=os.environ.get("TELEGRAM_BOT_TOKEN", ""),
        help="Telegram Bot API token (or set TELEGRAM_BOT_TOKEN env var)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8101)
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
        help="MCP transport (default: stdio)",
    )
    args = parser.parse_args()

    if not args.token:
        print(
            "Warning: No Telegram token provided. "
            "Set TELEGRAM_BOT_TOKEN or pass --token.",
            file=sys.stderr,
        )

    from pinky_outreach.server import create_server

    server = create_server(
        telegram_token=args.token,
        host=args.host,
        port=args.port,
    )

    print(f"[pinky-outreach] Starting on {args.transport}...", file=sys.stderr)

    if args.transport == "sse":
        server.run(transport="sse")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
