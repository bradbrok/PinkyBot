"""Run pinky-self MCP server standalone."""

import argparse
import os
import sys
from pathlib import Path

from pinky_daemon.auth import make_db_signing_key_resolver
from pinky_self.server import create_server


def _default_agents_db() -> str:
    """Canonical agents DB path, used when PINKY_AGENTS_DB isn't set.

    This file lives at ``src/pinky_self/__main__.py``; the repo root is two
    parents up and the daemon's default agents DB is
    ``data/conversations_agents.db`` under it. PINKY_AGENTS_DB (injected via
    .mcp.json) overrides this.
    """
    return str(Path(__file__).resolve().parents[2] / "data" / "conversations_agents.db")


def main():
    parser = argparse.ArgumentParser(description="Pinky Self-Management MCP Server")
    parser.add_argument("--agent", required=True, help="Agent name (e.g. oleg)")
    parser.add_argument("--api-url", default="http://localhost:8888", help="PinkyBot API URL")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8010)
    parser.add_argument(
        "--db-path", default="",
        help="Agents DB path for request-time signing-key lookup (#641); "
             "defaults to $PINKY_AGENTS_DB or the canonical data path.",
    )
    parser.add_argument(
        "--tool-gates", default="",
        help="Comma-separated tool gates to activate "
             "(e.g. kb,research,presentations,triggers,schedule,skill-admin,admin,tasks-admin)",
    )
    args = parser.parse_args()

    gates = [g.strip() for g in args.tool_gates.split(",") if g.strip()] if args.tool_gates else []

    # #641: resolve the signing key from the agents DB at request time so a
    # stale baked PINKY_AGENT_KEY can't lock this stdio server out after a
    # daemon restart. With a resolver present, the server ignores the env key
    # and falls back to the global secret only.
    db_path = args.db_path or os.environ.get("PINKY_AGENTS_DB", "") or _default_agents_db()
    resolver = make_db_signing_key_resolver(db_path)

    server = create_server(
        agent_name=args.agent,
        api_url=args.api_url,
        host=args.host,
        port=args.port,
        tool_gates=gates,
        signing_key_resolver=resolver,
    )

    print(f"[pinky-self] Starting for agent '{args.agent}'", file=sys.stderr)
    print(f"  API: {args.api_url}", file=sys.stderr)
    print(f"  Host: {args.host}:{args.port}", file=sys.stderr)
    print(f"  Tool gates: {gates or '(none — core only)'}", file=sys.stderr)
    server.run(transport="stdio")


if __name__ == "__main__":
    main()
