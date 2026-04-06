"""Run the Pinky Web MCP server standalone.

Usage:
    python -m pinky_web                  # Default stdio transport
    python -m pinky_web --headless       # Force headless (default)
    python -m pinky_web --headed         # Run with visible browser
"""

from __future__ import annotations

import argparse
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky Web MCP Server (Camoufox)")
    parser.add_argument(
        "--transport", default="stdio", choices=["stdio", "sse"],
        help="MCP transport (default: stdio)",
    )
    parser.add_argument("--host", default="127.0.0.1", help="Host for SSE transport")
    parser.add_argument("--port", type=int, default=8105, help="Port for SSE transport")
    parser.add_argument(
        "--headed", action="store_true",
        help="Run browser in headed mode (visible window)",
    )
    parser.add_argument(
        "--timeout", type=int, default=30000,
        help="Default page navigation timeout in ms (default: 30000)",
    )
    args = parser.parse_args()

    from pinky_web.server import create_server

    headless = not args.headed
    print(
        f"[pinky-web] transport={args.transport} headless={headless} "
        f"timeout={args.timeout}ms",
        file=sys.stderr, flush=True,
    )

    server = create_server(
        headless=headless,
        default_timeout=args.timeout,
        host=args.host,
        port=args.port,
    )

    if args.transport == "stdio":
        server.run(transport="stdio")
    else:
        server.run(transport="sse")


if __name__ == "__main__":
    main()
