"""Pinky CLI entry point."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="pinky",
        description="Pinky -- Personal AI companion framework",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # init
    init_parser = subparsers.add_parser("init", help="Initialize a new Pinky project")
    init_parser.add_argument("--name", default="Pinky", help="Agent name")
    init_parser.add_argument("--dir", default=".", help="Project directory")

    # serve
    serve_parser = subparsers.add_parser("serve", help="Start MCP servers")
    serve_parser.add_argument(
        "--server",
        default="all",
        choices=["memory", "outreach", "all"],
        help="Which server to start",
    )

    # connect
    subparsers.add_parser("connect", help="Write Claude Code MCP config")

    # run (daemon)
    run_parser = subparsers.add_parser("run", help="Run the Pinky daemon (headless Claude Code)")
    run_parser.add_argument(
        "--mode",
        default="api",
        choices=["api", "poll"],
        help="Run mode: api (HTTP server) or poll (message polling daemon)",
    )
    run_parser.add_argument(
        "--config",
        default="pinky.yaml",
        help="Path to pinky.yaml config (poll mode)",
    )
    run_parser.add_argument(
        "--working-dir",
        default=".",
        help="Working directory (where CLAUDE.md lives)",
    )

    # ACP daemon-backed stdio connector
    acp_parser = subparsers.add_parser(
        "acp",
        help="Run an ACP stdio connector backed by the Pinky daemon",
    )
    acp_parser.add_argument(
        "--agent",
        default=os.environ.get("PINKY_AGENT"),
        help="Agent identity (or PINKY_AGENT)",
    )
    acp_parser.add_argument(
        "--daemon-url",
        default=os.environ.get("PINKY_DAEMON_URL", "http://127.0.0.1:8888"),
        help="Pinky daemon URL (or PINKY_DAEMON_URL)",
    )

    args = parser.parse_args()

    if args.command == "init":
        from pinky_cli.init import run_init
        run_init(args.name, args.dir)
    elif args.command == "serve":
        from pinky_cli.serve import run_serve
        run_serve(server=args.server)
    elif args.command == "connect":
        from pinky_cli.connect import run_connect
        run_connect()
    elif args.command == "run":
        from pinky_daemon.__main__ import main as daemon_main
        # Override sys.argv for the daemon's own arg parser
        sys.argv = [
            "pinky-daemon",
            "--mode", args.mode,
            "--config", args.config,
            "--working-dir", args.working_dir,
        ]
        daemon_main()
    elif args.command == "acp":
        if not args.agent:
            acp_parser.error("--agent is required when PINKY_AGENT is not set")
        from pinky_cli.acp import run_acp

        try:
            asyncio.run(run_acp(args.agent, args.daemon_url))
        except (RuntimeError, ValueError) as exc:
            print(f"[pinky-acp] {exc}", file=sys.stderr, flush=True)
            sys.exit(2)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
