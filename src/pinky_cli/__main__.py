"""Pinky CLI entry point."""

from __future__ import annotations

import argparse
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
    serve_parser.add_argument("--memory-only", action="store_true", help="Only start memory server")

    # connect
    subparsers.add_parser("connect", help="Write Claude Code MCP config")

    args = parser.parse_args()

    if args.command == "init":
        from pinky_cli.init import run_init
        run_init(args.name, args.dir)
    elif args.command == "serve":
        from pinky_cli.serve import run_serve
        run_serve(memory_only=args.memory_only)
    elif args.command == "connect":
        from pinky_cli.connect import run_connect
        run_connect()
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
