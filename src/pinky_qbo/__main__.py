"""Run the pinky-qbo MCP server standalone (per-agent stdio).

Bound at startup to one agent + realm + env. Secrets are loaded from the
agent-scoped TokenStore (agents DB), NOT from argv/env, so a rotated refresh
token never goes stale in a process row (spec §4).
"""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky QBO MCP Server (read-only)")
    parser.add_argument(
        "--agent",
        default=os.environ.get("QBO_AGENT", ""),
        help="Bound agent name (e.g. onesie). Loads agent:<agent>:qbo_* settings.",
    )
    parser.add_argument(
        "--qbo-realm-id",
        default=os.environ.get("QBO_REALM_ID_EXPECTED", ""),
        help="Bound QBO realm (company) id.",
    )
    parser.add_argument(
        "--qbo-env",
        default=os.environ.get("QBO_ENV_EXPECTED", "production"),
        choices=["production", "sandbox"],
        help="QBO environment (production|sandbox).",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8106)
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
    )
    args = parser.parse_args()

    if not args.agent:
        print(
            "Warning: --agent not set. QBO tools will return 'not configured'.",
            file=sys.stderr,
        )
    if not args.qbo_realm_id:
        print(
            "Warning: --qbo-realm-id not set. QBO tools will return 'not configured'.",
            file=sys.stderr,
        )

    from pinky_qbo.server import create_server

    server = create_server(
        agent=args.agent,
        realm_id=args.qbo_realm_id,
        env=args.qbo_env,
        host=args.host,
        port=args.port,
    )

    print("[pinky-qbo] Starting...", file=sys.stderr)
    if args.transport == "sse":
        server.run(transport="sse")
    else:
        server.run(transport="stdio")


if __name__ == "__main__":
    main()
