"""Message handler — routes inbound messages to Claude Code.

Receives messages from any platform adapter, formats them as prompts,
sends to Claude Code, and handles the response. This is the core
brain loop of the daemon.
"""

from __future__ import annotations

import asyncio
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone

from pinky_daemon.claude_runner import ClaudeRunner


@dataclass
class InboundMessage:
    """An inbound message from any platform."""

    platform: str  # telegram, discord, slack
    chat_id: str
    sender_name: str
    sender_id: str
    content: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    message_id: str = ""
    chat_title: str = ""
    is_group: bool = False
    metadata: dict = field(default_factory=dict)


@dataclass
class HandlerConfig:
    """Configuration for the message handler."""

    # Session strategy: "shared" (one session) or "per_chat" (session per chat_id)
    session_strategy: str = "per_chat"

    # Session ID prefix
    session_prefix: str = "pinky"

    # System prompt to prepend to messages
    context_template: str = (
        "You received a message on {platform} from {sender_name} "
        "(chat_id: {chat_id}).\n"
        "Respond using the outreach MCP tools to send your reply back.\n"
        "Use send_message with platform=\"{platform}\" and chat_id=\"{chat_id}\".\n\n"
        "Message: {content}"
    )

    # Ignore messages from bots
    ignore_bots: bool = True

    # Rate limiting: min seconds between processing messages from same chat
    rate_limit_seconds: float = 1.0

    # Max concurrent Claude invocations
    max_concurrent: int = 3


class MessageHandler:
    """Routes inbound messages to Claude Code and manages sessions.

    The handler maintains a session-per-chat model by default, so
    each conversation has its own context window. This prevents
    cross-talk between different chats.
    """

    def __init__(
        self,
        runner: ClaudeRunner,
        config: HandlerConfig | None = None,
        response_callback=None,
    ) -> None:
        self._runner = runner
        self._config = config or HandlerConfig()
        self._response_callback = response_callback  # async fn(platform, chat_id, text)
        self._semaphore = asyncio.Semaphore(self._config.max_concurrent)
        self._last_processed: dict[str, float] = {}
        self._message_count: int = 0

    async def handle(self, message: InboundMessage) -> str | None:
        """Process an inbound message through Claude Code.

        Returns the raw output from Claude, or None if the message
        was skipped (rate limited, bot message, etc).
        """
        # Rate limiting
        chat_key = f"{message.platform}:{message.chat_id}"
        now = datetime.now(timezone.utc).timestamp()
        last = self._last_processed.get(chat_key, 0)

        if now - last < self._config.rate_limit_seconds:
            _log(f"handler: rate limited {chat_key}")
            return None

        self._last_processed[chat_key] = now
        self._message_count += 1

        # Build prompt
        prompt = self._format_prompt(message)

        # Determine session ID
        session_id = self._get_session_id(message)

        _log(
            f"handler: processing message #{self._message_count} "
            f"from {message.platform}:{message.chat_id} "
            f"session={session_id}"
        )

        # Run through Claude with concurrency limit
        async with self._semaphore:
            result = await self._runner.run(
                prompt,
                session_id=session_id,
                resume=True,  # Always resume to maintain context
            )

        if result.ok:
            _log(f"handler: success for {chat_key}")
            # Send response back to the platform as a fallback
            # (Claude should use outreach tools, but if it returns plain text, send it directly)
            if self._response_callback and result.output:
                try:
                    await self._response_callback(message.platform, message.chat_id, result.output)
                except Exception as e:
                    _log(f"handler: response callback error for {chat_key}: {e}")
            return result.output
        else:
            _log(f"handler: error for {chat_key}: {result.error}")
            return None

    def _format_prompt(self, message: InboundMessage) -> str:
        """Format an inbound message as a Claude prompt."""
        return self._config.context_template.format(
            platform=message.platform,
            sender_name=message.sender_name,
            sender_id=message.sender_id,
            chat_id=message.chat_id,
            content=message.content,
            chat_title=message.chat_title or "DM",
            message_id=message.message_id,
        )

    def _get_session_id(self, message: InboundMessage) -> str:
        """Generate a session ID based on the configured strategy."""
        prefix = self._config.session_prefix

        if self._config.session_strategy == "shared":
            return f"{prefix}-shared"
        elif self._config.session_strategy == "per_chat":
            return f"{prefix}-{message.platform}-{message.chat_id}"
        else:
            return f"{prefix}-{message.platform}-{message.chat_id}"

    @property
    def message_count(self) -> int:
        return self._message_count

    @property
    def active_sessions(self) -> int:
        return len(self._last_processed)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
