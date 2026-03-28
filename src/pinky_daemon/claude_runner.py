"""Claude Code runner — executes Claude Code as a subprocess.

Two modes:
  1. CLI mode (default): Invokes `claude` CLI with --print flag
  2. SDK mode (optional): Uses claude_agent_sdk for persistent sessions

CLI mode is the reliable default. It works with any Claude Code installation
and doesn't depend on SDK availability.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RunResult:
    """Result from a Claude Code invocation."""

    output: str
    exit_code: int
    session_id: str = ""
    cost_usd: float = 0.0
    duration_ms: int = 0
    error: str = ""

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.error


@dataclass
class ClaudeRunnerConfig:
    """Configuration for the Claude Code runner."""

    # Working directory (where CLAUDE.md and .mcp.json live)
    working_dir: str = "."

    # Session management
    session_id: str = ""  # Empty = new session each time
    resume: bool = False  # Resume previous session

    # Model selection
    model: str = ""  # Empty = default (usually sonnet)

    # MCP server config path
    mcp_config: str = ""  # Path to .mcp.json (auto-detected if empty)

    # Tool permissions
    allowed_tools: list[str] = field(default_factory=list)

    # System prompt additions (appended to CLAUDE.md)
    system_prompt: str = ""

    # Timeout for each invocation (seconds)
    timeout: float = 300.0

    # Max turns per invocation
    max_turns: int = 25

    # Claude Code binary path (auto-detected if empty)
    claude_bin: str = ""


def _find_claude_binary() -> str:
    """Find the claude CLI binary."""
    # Check common locations
    candidates = [
        shutil.which("claude"),
        os.path.expanduser("~/.claude/local/claude"),
        "/usr/local/bin/claude",
    ]
    for path in candidates:
        if path and os.path.isfile(path):
            return path

    raise FileNotFoundError(
        "Claude Code CLI not found. Install it from https://claude.ai/code"
    )


class ClaudeRunner:
    """Runs Claude Code via CLI subprocess.

    Each call to `run()` invokes the `claude` CLI with --print mode,
    which runs non-interactively and returns the result as text.

    For persistent sessions, set session_id in the config. Claude Code
    will maintain conversation context across calls with the same session.
    """

    def __init__(self, config: ClaudeRunnerConfig | None = None) -> None:
        self._config = config or ClaudeRunnerConfig()
        self._claude_bin = self._config.claude_bin or _find_claude_binary()
        self._working_dir = Path(self._config.working_dir).resolve()

    async def run(
        self,
        prompt: str,
        *,
        session_id: str = "",
        resume: bool = False,
        system_prompt: str = "",
    ) -> RunResult:
        """Run a prompt through Claude Code.

        Args:
            prompt: The message/prompt to send.
            session_id: Override session ID for this call.
            resume: Resume the previous session (--continue).
            system_prompt: Additional system prompt for this call.
        """
        cmd = self._build_command(
            prompt,
            session_id=session_id or self._config.session_id,
            resume=resume or self._config.resume,
            system_prompt=system_prompt or self._config.system_prompt,
        )

        _log(f"runner: executing claude with prompt length={len(prompt)}")

        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=str(self._working_dir),
                env=self._build_env(),
            )

            stdout, stderr = await asyncio.wait_for(
                process.communicate(),
                timeout=self._config.timeout,
            )

            output = stdout.decode("utf-8", errors="replace").strip()
            error = stderr.decode("utf-8", errors="replace").strip()

            # Parse JSON output if available
            result = RunResult(
                output=output,
                exit_code=process.returncode or 0,
                error=error if process.returncode != 0 else "",
            )

            # Try to extract session info from output
            result = self._parse_output(result, output)

            _log(f"runner: completed exit_code={result.exit_code} output_len={len(output)}")
            return result

        except asyncio.TimeoutError:
            _log(f"runner: timeout after {self._config.timeout}s")
            return RunResult(
                output="",
                exit_code=1,
                error=f"Timed out after {self._config.timeout} seconds",
            )
        except FileNotFoundError:
            return RunResult(
                output="",
                exit_code=1,
                error=f"Claude binary not found at {self._claude_bin}",
            )

    async def health_check(self) -> bool:
        """Check if Claude Code is available and working."""
        try:
            result = await self.run("Say 'ok' and nothing else.")
            return result.ok and "ok" in result.output.lower()
        except Exception:
            return False

    def _build_command(
        self,
        prompt: str,
        *,
        session_id: str = "",
        resume: bool = False,
        system_prompt: str = "",
    ) -> list[str]:
        """Build the claude CLI command."""
        cmd = [self._claude_bin, "--print"]

        # Output format
        cmd.extend(["--output-format", "text"])

        # Session management
        if session_id:
            cmd.extend(["--session-id", session_id])
        if resume:
            cmd.append("--continue")

        # Model
        if self._config.model:
            cmd.extend(["--model", self._config.model])

        # Max turns
        if self._config.max_turns:
            cmd.extend(["--max-turns", str(self._config.max_turns)])

        # Allowed tools
        for tool in self._config.allowed_tools:
            cmd.extend(["--allowedTools", tool])

        # System prompt
        if system_prompt:
            cmd.extend(["--system-prompt", system_prompt])

        # The prompt itself
        cmd.extend(["--prompt", prompt])

        return cmd

    def _build_env(self) -> dict[str, str]:
        """Build environment variables for the subprocess."""
        env = os.environ.copy()
        # Ensure non-interactive mode
        env["CLAUDE_CODE_NON_INTERACTIVE"] = "1"
        return env

    def _parse_output(self, result: RunResult, raw: str) -> RunResult:
        """Try to extract metadata from Claude's output."""
        # Claude --print with --output-format json would give us structured data
        # For text mode, we just return the raw output
        return result


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)
