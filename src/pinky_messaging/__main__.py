"""Run pinky-messaging MCP server standalone."""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def _default_agents_db() -> str:
    """Canonical agents DB path, used when PINKY_AGENTS_DB isn't set. Repo root
    is two parents up from ``src/pinky_messaging/__main__.py``; the daemon's
    default agents DB is ``data/conversations_agents.db`` under it."""
    return str(Path(__file__).resolve().parents[2] / "data" / "conversations_agents.db")


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
    parser.add_argument(
        "--db-path", default="",
        help="Agents DB path for request-time signing-key lookup (#641); "
             "defaults to $PINKY_AGENTS_DB or the canonical data path.",
    )
    args = parser.parse_args()

    if not args.agent:
        print("Error: --agent is required", file=sys.stderr)
        sys.exit(1)

    from pinky_daemon.auth import make_db_signing_key_resolver
    from pinky_messaging.server import create_server

    # #641: look up the current signing key from the agents DB at request time
    # so a stale baked PINKY_AGENT_KEY can't lock this stdio server out after a
    # daemon restart. Resolver present → env key ignored, global-secret fallback.
    db_path = args.db_path or os.environ.get("PINKY_AGENTS_DB", "") or _default_agents_db()
    resolver = make_db_signing_key_resolver(db_path)

    server = create_server(
        agent_name=args.agent, api_url=args.api_url, signing_key_resolver=resolver,
    )
    print(f"[pinky-messaging] {args.agent} -> {args.api_url}", file=sys.stderr)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
