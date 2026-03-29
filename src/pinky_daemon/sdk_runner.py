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
from pinky_daemon.hooks import HookContext, HookEvent, HookManager


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
        "mcp__pinky-memory__*",
        "mcp__outreach__*",
        "mcp__pinky-outreach__*",
        "mcp__pinky-self__*",
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

    def __init__(
        self,
        config: SDKRunnerConfig | None = None,
        *,
        hook_manager: HookManager | None = None,
        agent_name: str = "",
    ) -> None:
        self._config = config or SDKRunnerConfig()
        self._hook_manager = hook_manager
        self._agent_name = agent_name
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
        num_turns = 0
        stop_reason = ""
        usage = {}
        model_usage = {}
        duration_api_ms = 0
        turn_usage: list[dict] = []  # Per-turn usage tracking

        # Fire session_start hook
        await self._fire_hook(HookEvent.session_start, session_id=session_id)

        try:
            got_result = False

            async for message in query(prompt=prompt, options=options):
                if isinstance(message, SystemMessage):
                    # Extract session ID from init
                    result_session_id = getattr(message, "session_id", "") or ""
                    _log(f"sdk-runner: session={result_session_id}")

                elif isinstance(message, ResultMessage):
                    # Prefer ResultMessage over AssistantMessage to avoid duplicates
                    got_result = True
                    if hasattr(message, "result") and message.result:
                        output_parts.clear()
                        output_parts.append(message.result)
                    if hasattr(message, "session_id"):
                        result_session_id = message.session_id or result_session_id
                    if hasattr(message, "total_cost_usd"):
                        cost_usd = message.total_cost_usd or 0.0
                    # Extended usage fields
                    num_turns = getattr(message, "num_turns", 0) or 0
                    stop_reason = getattr(message, "stop_reason", "") or ""
                    usage = getattr(message, "usage", {}) or {}
                    model_usage = getattr(message, "model_usage", {}) or {}
                    duration_api_ms = getattr(message, "duration_api_ms", 0) or 0

                elif isinstance(message, AssistantMessage) and not got_result:
                    # Track per-turn usage
                    turn_data = getattr(message, "usage", None)
                    if turn_data:
                        turn_usage.append(dict(turn_data) if hasattr(turn_data, "__iter__") else {"raw": turn_data})
                    # Collect text content + detect tool use
                    for block in message.content:
                        if isinstance(block, TextBlock):
                            output_parts.append(block.text)
                        elif hasattr(block, "type") and block.type == "tool_use":
                            # Fire pre_tool_use hook
                            tool_name = getattr(block, "name", "")
                            tool_input = getattr(block, "input", {})
                            await self._fire_hook(
                                HookEvent.pre_tool_use,
                                session_id=result_session_id,
                                data={"tool": {"tool_name": tool_name, "tool_input": tool_input}},
                            )

                # Detect subagent activity
                if isinstance(message, AssistantMessage):
                    for block in message.content:
                        if hasattr(block, "type") and block.type == "tool_use":
                            if getattr(block, "name", "") == "Agent":
                                await self._fire_hook(
                                    HookEvent.subagent_start,
                                    session_id=result_session_id,
                                    data={"subagent": getattr(block, "input", {})},
                                )

            elapsed_ms = int((time.time() - start) * 1000)
            output = "\n".join(output_parts).strip()

            _log(f"sdk-runner: done in {elapsed_ms}ms, turns={num_turns}, cost=${cost_usd:.4f}, output_len={len(output)}")

            # Fire session_end hook with extended cost data
            await self._fire_hook(
                HookEvent.session_end,
                session_id=result_session_id,
                data={
                    "cost_usd": cost_usd,
                    "duration_ms": elapsed_ms,
                    "duration_api_ms": duration_api_ms,
                    "num_turns": num_turns,
                    "stop_reason": stop_reason,
                    "usage": usage,
                    "success": True,
                    "output_length": len(output),
                },
            )

            return RunResult(
                output=output,
                exit_code=0,
                session_id=result_session_id,
                cost_usd=cost_usd,
                duration_ms=elapsed_ms,
                duration_api_ms=duration_api_ms,
                num_turns=num_turns,
                stop_reason=stop_reason,
                usage=usage,
                model_usage=model_usage,
            )

        except Exception as e:
            elapsed_ms = int((time.time() - start) * 1000)
            error_msg = str(e)
            _log(f"sdk-runner: error after {elapsed_ms}ms: {error_msg}")

            # Fire error hook
            await self._fire_hook(
                HookEvent.error,
                session_id=result_session_id,
                data={"error": error_msg, "duration_ms": elapsed_ms, "success": False},
            )

            return RunResult(
                output="",
                exit_code=1,
                session_id=result_session_id,
                error=error_msg,
                duration_ms=elapsed_ms,
            )

    async def _fire_hook(
        self,
        event: HookEvent,
        *,
        session_id: str = "",
        data: dict | None = None,
    ) -> None:
        """Fire a hook event if hook manager is configured."""
        if not self._hook_manager:
            return
        ctx = HookContext(
            event=event,
            agent_name=self._agent_name,
            session_id=session_id,
            data=data or {},
        )
        await self._hook_manager.fire(ctx)

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
