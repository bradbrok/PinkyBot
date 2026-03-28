"""Run Pinky daemon.

Usage:
    python -m pinky_daemon
    python -m pinky_daemon --config pinky.yaml
    python -m pinky_daemon --working-dir /path/to/project
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys


def main() -> None:
    parser = argparse.ArgumentParser(description="Pinky Daemon — headless Claude Code")
    parser.add_argument(
        "--config",
        default=os.environ.get("PINKY_CONFIG", "pinky.yaml"),
        help="Path to pinky.yaml config file",
    )
    parser.add_argument(
        "--working-dir",
        default=".",
        help="Working directory (where CLAUDE.md lives)",
    )
    args = parser.parse_args()

    from pinky_daemon.daemon import Daemon, DaemonConfig

    # Load config
    config = DaemonConfig.from_yaml(args.config)
    config.working_dir = os.path.abspath(args.working_dir)

    # Also check env vars directly as fallback
    if not config.telegram_token:
        config.telegram_token = os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not config.discord_token:
        config.discord_token = os.environ.get("DISCORD_BOT_TOKEN", "")
    if not config.slack_token:
        config.slack_token = os.environ.get("SLACK_BOT_TOKEN", "")

    print(
        f"[pinky-daemon] Starting...\n"
        f"  Config: {args.config}\n"
        f"  Working dir: {config.working_dir}\n"
        f"  Telegram: {'configured' if config.telegram_token else 'not configured'}\n"
        f"  Discord: {'configured' if config.discord_token else 'not configured'}\n"
        f"  Slack: {'configured' if config.slack_token else 'not configured'}\n"
        f"  Session strategy: {config.session_strategy}\n"
        f"  Max concurrent: {config.max_concurrent}",
        file=sys.stderr,
    )

    daemon = Daemon(config)

    try:
        asyncio.run(daemon.start())
    except KeyboardInterrupt:
        print("\n[pinky-daemon] Interrupted", file=sys.stderr)


if __name__ == "__main__":
    main()
