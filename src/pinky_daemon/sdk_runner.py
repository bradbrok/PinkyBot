"""Claude Code SDK runner — uses the official claude_agent_sdk.

This is the preferred runner for PinkyBot. It uses Anthropic's official
Python SDK for programmatic Claude Code sessions with streaming,
hooks, and proper session management.

Falls back to ClaudeRunner (subprocess) if the SDK is not installed.
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field

from pinky_daemon.claude_runner import RunResult
from pinky_daemon.effort import resolve_cli_effort
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

    # MCP servers (dict of name -> config, or an explicit --mcp-config path)
    mcp_servers: dict | str = field(default_factory=dict)

    # Optional session bounds; unset fields retain the SDK's defaults.
    tools: list[str] | None = None
    strict_mcp_config: bool = False
    setting_sources: list[str] | None = None
    hooks: dict | None = None

    # Tool permissions (outreach removed — broker handles messaging)
    allowed_tools: list[str] = field(default_factory=lambda: [
        "Read", "Glob", "Grep",
        "Agent",  # subagent spawning
        "mcp__memory__*",
        "mcp__pinky-memory__*",
        "mcp__pinky-messaging__*",
        "mcp__pinky-self__*",
    ])

    # Max turns per query
    max_turns: int | None = None

    # Permission mode
    permission_mode: str = "bypassPermissions"

    # System prompt
    system_prompt: str = ""

    # Named subagent definitions (name -> AgentDefinition)
    agents: dict = field(default_factory=dict)

    # Explicitly blocked tools (complement to allowed_tools)
    disallowed_tools: list[str] = field(default_factory=list)

    # Thinking effort: low, medium, high, xhigh, max, ultracode (#151).
    # ultracode resolves to xhigh for the SDK effort knob.
    thinking_effort: str = "medium"

    # When True, the verify_effort CLI hook blocks tool calls if the runtime
    # effort drifts from thinking_effort. Default False (warn-only). See #429.
    strict_effort_enforcement: bool = False

    # Provider overrides — set these to use Ollama or other compatible endpoints
    provider_url: str = ""   # ANTHROPIC_BASE_URL override
    provider_key: str = ""   # ANTHROPIC_API_KEY override


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
        session_id_callback: Callable[[str], None] | None = None,
    ) -> None:
        self._config = config or SDKRunnerConfig()
        self._hook_manager = hook_manager
        self._agent_name = agent_name
        self._session_id_callback = session_id_callback
        self._ensure_sdk()

    def _ensure_sdk(self) -> None:
        """Verify the SDK is available and its bundled binary is executable."""
        try:
            import claude_agent_sdk  # noqa: F401
        except ImportError:
            raise ImportError(
                "claude-agent-sdk is required for SDKRunner. "
                "Install it: pip install claude-agent-sdk"
            )
        # The SDK bundles a Claude binary that pip sometimes installs without
        # the execute bit (wheel doesn't preserve permissions). Fix it here
        # so dreams and other SDK-driven sessions don't fail with EACCES.
        self._fix_bundled_binary_permissions(claude_agent_sdk)

    @staticmethod
    def _fix_bundled_binary_permissions(sdk_module) -> None:
        """Ensure the SDK's bundled Claude binary has the execute bit set.

        pip wheels don't preserve POSIX permissions, so the bundled ELF
        binary at ``claude_agent_sdk/_bundled/claude`` can end up 0644
        after install/upgrade — causing ``[Errno 13] Permission denied``
        when the SDK tries to spawn it (it prefers the bundled copy over
        ``shutil.which('claude')``).  Best-effort; silently ignored on
        failure (the SDK will still fall back to the system binary).
        """
        import os
        import stat
        from pathlib import Path

        try:
            bundled = Path(sdk_module.__file__).resolve().parent / "_bundled" / "claude"
            if bundled.is_file() and not os.access(bundled, os.X_OK):
                bundled.chmod(bundled.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
                _log(f"sdk-runner: fixed +x on bundled CLI at {bundled}")
        except Exception:
            pass  # best-effort — system `claude` binary is the fallback

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
            ConversationResetMessage,
            RateLimitEvent,
            ResultMessage,
            StreamEvent,
            SystemMessage,
            TextBlock,
            UserMessage,
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

        if self._config.mcp_servers:
            options.mcp_servers = self._config.mcp_servers

        if self._config.tools is not None:
            options.tools = self._config.tools

        if self._config.strict_mcp_config:
            options.strict_mcp_config = self._config.strict_mcp_config

        if self._config.setting_sources is not None:
            options.setting_sources = self._config.setting_sources

        if self._config.hooks is not None:
            options.hooks = self._config.hooks

        if self._config.disallowed_tools:
            options.disallowed_tools = self._config.disallowed_tools

        if self._config.agents:
            options.agents = self._config.agents

        # Apply thinking effort. ultracode resolves to xhigh (#151) — the SDK
        # forwards options.effort straight to the CLI's --effort flag, which
        # rejects the literal "ultracode".
        effort = resolve_cli_effort(self._config.thinking_effort or "medium")
        if effort != "medium":
            options.effort = effort

        # Build env overrides — always set MCP non-blocking for faster startup,
        # plus any provider overrides (Ollama / custom compatible endpoints)
        env_overrides: dict[str, str] = {
            "MCP_CONNECTION_NONBLOCKING": "true",
        }
        if self._config.provider_url:
            env_overrides["ANTHROPIC_BASE_URL"] = self._config.provider_url
        if self._config.provider_key:
            env_overrides["ANTHROPIC_API_KEY"] = self._config.provider_key
        # #429: surface configured effort + agent identity to CLI hooks so
        # verify_effort.py can detect drift from PINKY_EXPECTED_EFFORT vs
        # $CLAUDE_EFFORT at PreToolUse time. The hook no-ops on "auto" /
        # empty (intentionally adaptive).
        if self._agent_name:
            env_overrides["PINKY_AGENT_NAME"] = self._agent_name
        if effort:
            env_overrides["PINKY_EXPECTED_EFFORT"] = effort
        if self._config.strict_effort_enforcement:
            env_overrides["PINKY_STRICT_EFFORT"] = "1"
        options.env = env_overrides

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

        # Fire session_start hook
        await self._fire_hook(HookEvent.session_start, session_id=session_id)

        try:
            got_result = False
            invalidated_session_ids: set[str] = set()

            def capture_session_id(candidate: object) -> None:
                """Capture a non-empty ID not invalidated by a reset frame."""
                nonlocal result_session_id
                if not isinstance(candidate, str) or not candidate:
                    return
                if candidate in invalidated_session_ids:
                    _log(
                        "sdk-runner: WARNING refusing invalidated session_id "
                        f"{candidate[:12]}"
                    )
                    return
                if candidate == result_session_id:
                    return
                result_session_id = candidate
                if self._session_id_callback:
                    self._session_id_callback(result_session_id)

            async for message in query(prompt=prompt, options=options):
                if isinstance(message, SystemMessage):
                    # Extract session ID from init
                    capture_session_id(getattr(message, "session_id", ""))
                    _log(f"sdk-runner: session={result_session_id}")

                elif isinstance(message, ResultMessage):
                    capture_session_id(getattr(message, "session_id", None))
                    # Prefer ResultMessage over AssistantMessage to avoid duplicates
                    # Only suppress AssistantMessage if ResultMessage has actual content
                    if hasattr(message, "result") and message.result:
                        got_result = True
                        output_parts.clear()
                        output_parts.append(message.result)
                    if hasattr(message, "total_cost_usd"):
                        cost_usd = message.total_cost_usd or 0.0
                    # Extended usage fields
                    num_turns = getattr(message, "num_turns", 0) or 0
                    stop_reason = getattr(message, "stop_reason", "") or ""
                    usage = getattr(message, "usage", {}) or {}
                    model_usage = getattr(message, "model_usage", {}) or {}
                    duration_api_ms = getattr(message, "duration_api_ms", 0) or 0

                elif isinstance(message, AssistantMessage) and not got_result:
                    capture_session_id(message.session_id)
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

                elif isinstance(message, AssistantMessage):
                    # Result content already won the existing dedupe rule.
                    capture_session_id(message.session_id)

                elif isinstance(message, ConversationResetMessage):
                    # /clear invalidates the outgoing ID immediately. Clear
                    # both local and durable state synchronously, before the
                    # iterator can advance to a fresh-session frame.
                    if message.session_id:
                        invalidated_session_ids.add(message.session_id)
                    if result_session_id:
                        invalidated_session_ids.add(result_session_id)
                    result_session_id = ""
                    if self._session_id_callback:
                        self._session_id_callback("")

                    _log(
                        "sdk-runner: WARNING SDK conversation reset; transcript "
                        "was discarded "
                        f"new_conversation_id={message.new_conversation_id!r} "
                        f"session_id={message.session_id!r}"
                    )

                elif isinstance(message, RateLimitEvent):
                    _log(
                        "sdk-runner: WARNING SDK rate-limit event "
                        f"status={message.rate_limit_info.status!r}"
                    )

                elif isinstance(message, (UserMessage, StreamEvent)):
                    # Known non-result frames are intentionally ignored.
                    pass

                else:
                    # The SDK Message union is open across dependency bumps.
                    # Never let a newly added frame disappear silently.
                    _log(
                        "sdk-runner: WARNING unhandled SDK message "
                        f"type={type(message).__name__}; continuing"
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
