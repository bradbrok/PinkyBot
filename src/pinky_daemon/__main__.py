"""Run Pinky daemon or API server.

Usage:
    # API server (stateful sessions via HTTP)
    python -m pinky_daemon --mode api --port 8888

    # Polling daemon (auto-processes inbound messages)
    python -m pinky_daemon --mode poll --config pinky.yaml

    # Default is API mode
    python -m pinky_daemon
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def _load_dotenv() -> None:
    """Load .env file from working directory if it exists."""
    env_path = os.path.join(os.getcwd(), ".env")
    if not os.path.isfile(env_path):
        return
    with open(env_path) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip("\"'")
            if key and key not in os.environ:  # Don't override existing env vars
                os.environ[key] = value


def main() -> None:
    _load_dotenv()
    parser = argparse.ArgumentParser(description="Pinky — headless Claude Code")
    parser.add_argument(
        "--mode",
        choices=["api", "poll"],
        default="api",
        help="Run mode: api (HTTP server) or poll (message polling daemon)",
    )
    parser.add_argument("--host", default="0.0.0.0", help="API server host")
    parser.add_argument("--port", type=int, default=8888, help="API server port")
    parser.add_argument(
        "--config",
        default=os.environ.get("PINKY_CONFIG", "pinky.yaml"),
        help="Config file (poll mode)",
    )
    parser.add_argument(
        "--working-dir",
        default=".",
        help="Working directory (where CLAUDE.md lives)",
    )
    parser.add_argument(
        "--max-sessions",
        type=int,
        default=50,
        help="Max concurrent sessions (api mode)",
    )
    args = parser.parse_args()

    if args.mode == "api":
        _run_api(args)
    elif args.mode == "poll":
        _run_poll(args)


def _run_api(args) -> None:
    """Start the stateful API server."""
    import uvicorn

    from pinky_daemon.api import create_api

    working_dir = os.path.abspath(args.working_dir)

    print(
        f"[pinky] Starting API server\n"
        f"  Host: {args.host}:{args.port}\n"
        f"  Working dir: {working_dir}\n"
        f"  Max sessions: {args.max_sessions}",
        file=sys.stderr,
    )

    app = create_api(
        max_sessions=args.max_sessions,
        default_working_dir=working_dir,
    )

    uvicorn.run(app, host=args.host, port=args.port)


def _run_poll(args) -> None:
    """Start the polling daemon."""
    from pinky_daemon.daemon import Daemon, DaemonConfig

    config = DaemonConfig.from_yaml(args.config)
    config.working_dir = os.path.abspath(args.working_dir)

    # Fallback to env vars
    if not config.telegram_token:
        config.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not config.discord_token:
        config.discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not config.slack_token:
        config.slack_token = os.environ.get("SLACK_BOT_TOKEN", "")

    print(
        f"[pinky] Starting polling daemon\n"
        f"  Config: {args.config}\n"
        f"  Working dir: {config.working_dir}\n"
        f"  Telegram: {'yes' if config.telegram_token else 'no'}\n"
        f"  Discord: {'yes' if config.discord_token else 'no'}\n"
        f"  Slack: {'yes' if config.slack_token else 'no'}",
        file=sys.stderr,
    )

    daemon = Daemon(config)
    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        print("\n[pinky] Interrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
