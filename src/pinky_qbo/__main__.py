"""Run pinky-qbo MCP server standalone (onesie-only, TOD fleet)."""

from __future__ import annotations

import argparse
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky QBO MCP Server (read-only QuickBooks Online)")
    parser.add_argument(
        "--agent",
        required=True,
        help="Agent name — must be 'onesie'",
    )
    parser.add_argument(
        "--qbo-realm-id",
        default=os.environ.get("QBO_REALM_ID", ""),
        help="QuickBooks Online company realm ID",
    )
    parser.add_argument(
        "--qbo-env",
        choices=["sandbox", "production"],
        default=os.environ.get("QBO_ENV", "production"),
        help="QuickBooks Online environment",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8110)
    parser.add_argument(
        "--transport",
        choices=["stdio", "sse"],
        default="stdio",
    )
    args = parser.parse_args()

    # Guard: this server is onesie-only
    if args.agent != "onesie":
        print(
            f"[pinky-qbo] ERROR: --agent must be 'onesie', got '{args.agent}'. "
            "This MCP server is scoped to the onesie agent only.",
            file=sys.stderr,
        )
        sys.exit(1)

    # Build the agent-scoped TokenStore backed by AgentRegistry
    store = None
    agents_db = os.environ.get("PINKY_AGENTS_DB", "")
    if agents_db:
        try:
            from pinky_daemon.agent_registry import AgentRegistry  # type: ignore[import]

            registry = AgentRegistry(db_path=agents_db)
            agent_name = "onesie"

            def _get(key: str) -> str | None:
                val = registry.get_agent_setting(agent_name, key)
                return val if val else None

            def _set(key: str, value: str) -> None:
                registry.set_agent_setting(agent_name, key, value)

            def _del(key: str) -> None:
                registry.delete_setting(f"agent:{agent_name}:{key}")

            from pinky_qbo.store import QboTokenStore

            store = QboTokenStore(get_fn=_get, set_fn=_set, delete_fn=_del)
        except Exception as exc:
            print(f"[pinky-qbo] WARNING: failed to build TokenStore: {exc}", file=sys.stderr)
    else:
        print(
            "[pinky-qbo] WARNING: PINKY_AGENTS_DB not set. "
            "Token store unavailable — tools will return 'not configured'.",
            file=sys.stderr,
        )

    from pinky_qbo.server import create_server

    server = create_server(
        agent=args.agent,
        realm_id=args.qbo_realm_id,
        env=args.qbo_env,
        store=store,
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
