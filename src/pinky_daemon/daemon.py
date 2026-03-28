"""Pinky Daemon — the main event loop.

Starts platform pollers, routes messages through Claude Code,
and manages the lifecycle of all components.

Usage:
    python -m pinky_daemon
    python -m pinky_daemon --config pinky.yaml
    pinky run  # via CLI
"""

from __future__ import annotations

import asyncio
import signal
import sys
from dataclasses import dataclass, field
from pathlib import Path

import yaml

from pinky_daemon.claude_runner import ClaudeRunner, ClaudeRunnerConfig
from pinky_daemon.message_handler import HandlerConfig, MessageHandler
from pinky_daemon.pollers import TelegramPoller
from pinky_outreach.telegram import TelegramAdapter


@dataclass
class DaemonConfig:
    """Full daemon configuration, loaded from pinky.yaml."""

    # Working directory
    working_dir: str = "."

    # Claude Code settings
    claude_model: str = ""
    claude_max_turns: int = 25
    claude_timeout: float = 300.0
    claude_allowed_tools: list[str] = field(default_factory=lambda: [
        "mcp__memory__*",
        "mcp__outreach__*",
        "Read",
        "Glob",
        "Grep",
    ])

    # Session strategy
    session_strategy: str = "per_chat"
    session_prefix: str = "pinky"
    max_concurrent: int = 3

    # Telegram
    telegram_token: str = ""
    telegram_allowed_chats: list[str] = field(default_factory=list)
    telegram_poll_timeout: int = 30

    # Discord (future)
    discord_token: str = ""

    # Slack (future)
    slack_token: str = ""

    @classmethod
    def from_yaml(cls, path: str) -> DaemonConfig:
        """Load config from a pinky.yaml file."""
        config = cls()
        yaml_path = Path(path)

        if not yaml_path.exists():
            _log(f"daemon: config file {path} not found, using defaults")
            return config

        with open(yaml_path) as f:
            data = yaml.safe_load(f) or {}

        # Claude settings
        claude = data.get("claude", {})
        if claude.get("model"):
            config.claude_model = claude["model"]
        if claude.get("max_turns"):
            config.claude_max_turns = claude["max_turns"]
        if claude.get("timeout"):
            config.claude_timeout = claude["timeout"]

        # Session settings
        daemon = data.get("daemon", {})
        if daemon.get("session_strategy"):
            config.session_strategy = daemon["session_strategy"]
        if daemon.get("session_prefix"):
            config.session_prefix = daemon["session_prefix"]
        if daemon.get("max_concurrent"):
            config.max_concurrent = daemon["max_concurrent"]

        # Outreach / platform tokens
        outreach = data.get("outreach", {})

        tg = outreach.get("telegram", {})
        config.telegram_token = _resolve_env(tg.get("bot_token", ""))
        if tg.get("allowed_chats"):
            config.telegram_allowed_chats = [str(c) for c in tg["allowed_chats"]]
        if tg.get("poll_timeout"):
            config.telegram_poll_timeout = tg["poll_timeout"]

        dc = outreach.get("discord", {})
        config.discord_token = _resolve_env(dc.get("bot_token", ""))

        sl = outreach.get("slack", {})
        config.slack_token = _resolve_env(sl.get("bot_token", ""))

        return config


def _resolve_env(value: str) -> str:
    """Resolve ${ENV_VAR} references in config values."""
    import os
    import re

    def replacer(match):
        env_var = match.group(1)
        return os.environ.get(env_var, "")

    if isinstance(value, str) and "${" in value:
        return re.sub(r"\$\{(\w+)\}", replacer, value)
    return value or ""


class Daemon:
    """The main Pinky daemon process.

    Lifecycle:
        1. Load config from pinky.yaml
        2. Initialize Claude Code runner
        3. Initialize message handler
        4. Start platform pollers
        5. Run until SIGTERM/SIGINT
        6. Graceful shutdown
    """

    def __init__(self, config: DaemonConfig) -> None:
        self._config = config
        self._pollers: list = []
        self._tasks: list[asyncio.Task] = []
        self._running = False

        # Build components
        runner_config = ClaudeRunnerConfig(
            working_dir=config.working_dir,
            model=config.claude_model,
            max_turns=config.claude_max_turns,
            timeout=config.claude_timeout,
            allowed_tools=config.claude_allowed_tools,
        )
        self._runner = ClaudeRunner(runner_config)

        handler_config = HandlerConfig(
            session_strategy=config.session_strategy,
            session_prefix=config.session_prefix,
            max_concurrent=config.max_concurrent,
        )
        self._handler = MessageHandler(self._runner, handler_config)

    async def start(self) -> None:
        """Start the daemon and all pollers."""
        self._running = True
        _log("daemon: starting Pinky daemon")

        # Register signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # Start platform pollers
        if self._config.telegram_token:
            adapter = TelegramAdapter(self._config.telegram_token)
            poller = TelegramPoller(
                adapter,
                self._handler,
                poll_timeout=self._config.telegram_poll_timeout,
                allowed_chat_ids=self._config.telegram_allowed_chats or None,
            )
            self._pollers.append(poller)
            task = asyncio.create_task(poller.start())
            self._tasks.append(task)
            _log("daemon: Telegram poller started")
        else:
            _log("daemon: Telegram not configured (no bot_token)")

        if not self._pollers:
            _log("daemon: no platform pollers configured! Set tokens in pinky.yaml")
            return

        _log(f"daemon: running with {len(self._pollers)} poller(s)")

        # Wait for all tasks (or until stopped)
        try:
            await asyncio.gather(*self._tasks, return_exceptions=True)
        except asyncio.CancelledError:
            pass

        _log("daemon: stopped")

    async def stop(self) -> None:
        """Gracefully stop the daemon."""
        if not self._running:
            return

        _log("daemon: shutting down...")
        self._running = False

        # Stop all pollers
        for poller in self._pollers:
            poller.stop()

        # Cancel all tasks
        for task in self._tasks:
            task.cancel()

        _log("daemon: shutdown complete")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def stats(self) -> dict:
        return {
            "running": self._running,
            "pollers": len(self._pollers),
            "messages_processed": self._handler.message_count,
            "active_sessions": self._handler.active_sessions,
        }


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
