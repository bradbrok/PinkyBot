"""Run pinky-calendar MCP server standalone."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky Calendar MCP Server")
    parser.add_argument(
        "--caldav-url",
        default=os.environ.get("CALDAV_URL", ""),
        help="CalDAV server URL",
    )
    parser.add_argument(
        "--caldav-username",
        default=os.environ.get("CALDAV_USERNAME", ""),
        help="CalDAV username",
    )
    parser.add_argument(
        "--caldav-password",
        default=os.environ.get("CALDAV_PASSWORD", ""),
        help="CalDAV password",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8105)
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
    )
    args = parser.parse_args()

    if not args.caldav_url:
        print(
            "Warning: CALDAV_URL not set. Calendar tools will return errors until configured.",
            file=sys.stderr,
        )

    from pinky_calendar.server import create_server

    server = create_server(
        caldav_url=args.caldav_url,
        caldav_username=args.caldav_username,
        caldav_password=args.caldav_password,
        host=args.host,
        port=args.port,
    )

    print("[pinky-calendar] Starting...", file=sys.stderr)
    if args.transport == "sse":
        server.run(transport="sse")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
