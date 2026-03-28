"""Claude Code SDK runner — uses the official claude_agent_sdk.

This is the preferred runner for PinkyBot. It uses Anthropic's official
Python SDK for programmatic Claude Code sessions with streaming,
hooks, and proper session management.

Falls back to ClaudeRunner (subprocess) if the SDK is not installed.
"""

from __future__ import annotations

import sys
import time
from dataclasses import dataclass, field

from pinky_daemon.claude_runner import RunResult


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


@dataclass
class SDKRunnerConfig:
    """Configuration for the SDK-based runner."""

    # Working directory (where CLAUDE.md and .mcp.json live)
    working_dir: str = "."

    # Model selection
    model: str | None = None

    # MCP servers (dict of name -> config)
    mcp_servers: dict = field(default_factory=dict)

    # Tool permissions
    allowed_tools: list[str] = field(default_factory=lambda: [
        "Read", "Glob", "Grep",
        "mcp__memory__*",
        "mcp__outreach__*",
    ])

    # Max turns per query
    max_turns: int | None = 25

    # Budget limit per query (USD)
    max_budget_usd: float | None = None

    # Permission mode
    permission_mode: str = "bypassPermissions"

    # System prompt
    system_prompt: str = ""


class SDKRunner:
    """Runs Claude Code via the official SDK.

    Each call to run() invokes the SDK's query() function which
    handles the full agent loop — tool execution, context management,
    and response generation.
    """

    def __init__(self, config: SDKRunnerConfig | None = None) -> None:
        self._config = config or SDKRunnerConfig()
        self._ensure_sdk()

    def _ensure_sdk(self) -> None:
        """Verify the SDK is available."""
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            raise ImportError(
                "claude-agent-sdk is required for SDKRunner. "
                "Install it: pip install claude-agent-sdk"
            )

    async def run(
        self,
        prompt: str,
        *,
        session_id: str = "",
        resume: bool = False,
        system_prompt: str = "",
    ) -> RunResult:
        """Run a prompt through Claude Code via SDK.

        Args:
            prompt: The message/prompt to send.
            session_id: Session ID to resume (must be a valid UUID or empty).
            resume: Whether to resume the session.
            system_prompt: System prompt override for this call.
        """
        from claude_agent_sdk import (
            AssistantMessage,
            ClaudeAgentOptions,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            TextBlock,
            query,
        )

        # Build options
        options = ClaudeAgentOptions(
            cwd=self._config.working_dir,
            allowed_tools=self._config.allowed_tools,
            permission_mode=self._config.permission_mode,
        )

        if self._config.model:
            options.model = self._config.model

        if self._config.max_turns:
            options.max_turns = self._config.max_turns

        if self._config.max_budget_usd:
            options.max_budget_usd = self._config.max_budget_usd

        if self._config.mcp_servers:
            options.mcp_servers = self._config.mcp_servers

        # Session management
        if session_id and resume:
            options.resume = session_id
        elif resume:
            options.continue_conversation = True

        # System prompt
        sys_prompt = system_prompt or self._config.system_prompt
        if sys_prompt:
            options.system_prompt = sys_prompt

        _log(f"sdk-runner: query len={len(prompt)} session={session_id or 'new'}")

        start = time.time()
        output_parts: list[str] = []
        result_session_id = ""
        cost_usd = 0.0

        try:
            async for message in query(prompt=prompt, options=options):
                if isinstance(message, SystemMessage):
                    # Extract session ID from init
                    result_session_id = getattr(message, "session_id", "") or ""
                    _log(f"sdk-runner: session={result_session_id}")

                elif isinstance(message, AssistantMessage):
                    # Collect text content
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            output_parts.append(block.text)

                elif isinstance(message, ResultMessage):
                    # Final result
                    if hasattr(message, "result") and message.result:
                        output_parts.append(message.result)
                    if hasattr(message, "session_id"):
                        result_session_id = message.session_id or result_session_id
                    if hasattr(message, "total_cost_usd"):
                        cost_usd = message.total_cost_usd or 0.0

            elapsed_ms = int((time.time() - start) * 1000)
            output = "\n".join(output_parts).strip()

            _log(f"sdk-runner: done in {elapsed_ms}ms, output_len={len(output)}")

            return RunResult(
                output=output,
                exit_code=0,
                session_id=result_session_id,
                cost_usd=cost_usd,
                duration_ms=elapsed_ms,
            )

        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            error_msg = str(e)
            _log(f"sdk-runner: error after {elapsed_ms}ms: {error_msg}")

            return RunResult(
                output="",
                exit_code=1,
                session_id=result_session_id,
                error=error_msg,
                duration_ms=elapsed_ms,
            )

    async def health_check(self) -> bool:
        """Check if Claude Code SDK is working."""
        try:
            result = await self.run("Say 'ok' and nothing else.")
            return result.ok and "ok" in result.output.lower()
        except Exception:
            return False


def create_runner(config: SDKRunnerConfig | None = None) -> SDKRunner:
    """Create an SDKRunner, or raise if SDK not available."""
    return SDKRunner(config)


def sdk_available() -> bool:
    """Check if the Claude Agent SDK is installed."""
    try:
        import claude_agent_sdk  # noqa: F401
        return True
    except ImportError:
        return False
