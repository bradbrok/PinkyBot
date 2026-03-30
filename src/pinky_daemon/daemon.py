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

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.autonomy import AgentEvent, AutonomyEngine, EventType
from pinky_daemon.claude_runner import ClaudeRunner, ClaudeRunnerConfig
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.message_handler import HandlerConfig, MessageHandler
from pinky_daemon.pollers import TelegramPoller
from pinky_daemon.scheduler import AgentScheduler
from pinky_daemon.task_store import TaskStore
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
        "mcp__pinky-memory__*",
        "mcp__pinky-messaging__*",
        "mcp__pinky-self__*",
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

        # Data stores (shared SQLite DB)
        db_path = Path(config.working_dir) / "data" / "pinky.db"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        db_str = str(db_path)
        self._registry = AgentRegistry(db_path=db_str.replace(".db", "_agents.db"))
        self._task_store = TaskStore(db_path=db_str.replace(".db", "_tasks.db"))
        self._conversation_store = ConversationStore(db_path=db_str.replace(".db", "_conversations.db"))

        # Session sender callback — sends prompts to Claude via the runner
        async def session_sender(agent_name: str, session_id: str, prompt: str) -> str:
            # Prepend wake context if available
            context = self._registry.get_context(agent_name)
            if context and context.task:
                ctx_prompt = context.to_prompt()
                if ctx_prompt:
                    prompt = f"[WAKE CONTEXT]\n{ctx_prompt}\n\n{prompt}"
            result = await self._runner.run(prompt, session_id=session_id, resume=True)
            return result.output if result.ok else ""

        self._session_sender = session_sender

        # Autonomy engine — self-directed agent work loops
        self._autonomy = AutonomyEngine(
            self._registry,
            self._task_store,
            self._conversation_store,
            session_sender=self._session_sender,
        )

        # Scheduler — cron-based wake system
        async def wake_callback(agent_name: str, session_id: str, prompt: str):
            event = AgentEvent(
                type=EventType.schedule_wake,
                agent_name=agent_name,
                data={"prompt": prompt, "session_id": session_id},
            )
            await self._autonomy.push_event(event)

        self._scheduler = AgentScheduler(
            self._registry,
            wake_callback=wake_callback,
        )

        # Response callback — sends Claude's reply back to the platform
        async def response_callback(platform: str, chat_id: str, text: str):
            if platform == "telegram" and self._pollers:
                for poller in self._pollers:
                    if isinstance(poller, TelegramPoller):
                        try:
                            poller._adapter.send_message(chat_id, text)
                        except Exception as e:
                            _log(f"daemon: failed to send response: {e}")
                        break

        # Event callback — pushes inbound messages to the autonomy engine
        async def event_callback(platform: str, chat_id: str, sender: str, content: str):
            event = AgentEvent(
                type=EventType.message_received,
                agent_name="pinky",  # Default agent for daemon mode
                data={
                    "platform": platform,
                    "chat_id": chat_id,
                    "sender": sender,
                    "content": content,
                },
            )
            await self._autonomy.push_event(event)

        self._event_callback = event_callback

        handler_config = HandlerConfig(
            session_strategy=config.session_strategy,
            session_prefix=config.session_prefix,
            max_concurrent=config.max_concurrent,
        )
        self._handler = MessageHandler(
            self._runner, handler_config,
            response_callback=response_callback,
        )

    async def start(self) -> None:
        """Start the daemon and all pollers."""
        self._running = True
        _log("daemon: starting Pinky daemon")

        # Register signal handlers
        loop = asyncio.get_event_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(self.stop()))

        # Start scheduler and autonomy engine
        await self._scheduler.start()
        _log("daemon: scheduler started")
        await self._autonomy.start()
        _log("daemon: autonomy engine started")

        # Start work loops for auto-start agents
        auto_start_agents = self._registry.list_auto_start_agents()
        for agent in auto_start_agents:
            await self._autonomy.start_agent_loop(agent.name)
        if auto_start_agents:
            _log(f"daemon: started autonomy loops for {len(auto_start_agents)} agent(s)")

        # Start platform pollers
        if self._config.telegram_token:
            adapter = TelegramAdapter(self._config.telegram_token)
            poller = TelegramPoller(
                adapter,
                self._handler,
                poll_timeout=self._config.telegram_poll_timeout,
                allowed_chat_ids=self._config.telegram_allowed_chats or None,
                event_callback=self._event_callback,
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

        # Stop autonomy engine and scheduler
        await self._autonomy.stop()
        await self._scheduler.stop()

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
            "scheduler_running": self._scheduler.running,
            "autonomy": self._autonomy.get_status(),
        }


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
