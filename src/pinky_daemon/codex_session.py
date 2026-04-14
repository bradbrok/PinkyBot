"""Codex Session — Codex CLI as an agent execution engine.

Wraps OpenAI's Codex CLI (`codex exec`) to provide a session interface
compatible with StreamingSession. Each user message spawns a `codex exec`
subprocess; multi-turn context is maintained via Codex's native session
resume (`codex exec resume <session_id>`).

JSONL event types from `codex exec --json`:
  thread.started   — {"thread_id": "..."}
  turn.started     — (no payload)
  item.started     — {"item": {"id", "type", "command", "status"}}
  item.completed   — {"item": {"id", "type", "text"|"command", ...}}
  turn.completed   — {"usage": {"input_tokens", "output_tokens", "cached_input_tokens"}}
  turn.failed      — {"error": {"message": "..."}}
  error            — {"message": "..."}
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass, field

from pinky_daemon.sessions import CHARS_PER_TOKEN, MODEL_CONTEXT_SIZES, SessionUsage
from pinky_daemon.streaming_session import (
    StreamingSessionConfig,
    StreamingTurnResult,
    _is_outreach_tool,
    _log,
)


@dataclass
class CodexTurnResult:
    """Accumulated result from a single codex exec invocation."""

    thread_id: str = ""
    text_parts: list[str] = field(default_factory=list)
    tool_uses: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    failed: bool = False


class CodexSession:
    """Agent session backed by Codex CLI.

    Drop-in replacement for StreamingSession — exposes the same public
    interface so the broker/API can treat them interchangeably.
    """

    def __init__(
        self,
        config: StreamingSessionConfig,
        *,
        response_callback=None,     # async fn(StreamingTurnResult)
        conversation_store=None,    # ConversationStore for history logging
        cost_callback=None,         # fn(agent_name, cost_usd, input_tokens, output_tokens, session_id)
        stream_event_callback=None,  # async fn(event: dict) for incremental UI streaming
        analytics_store=None,
    ) -> None:
        self._config = config
        self._response_callback = response_callback
        self._cost_callback = cost_callback
        self._conversation_store = conversation_store
        self._stream_event_callback = stream_event_callback
        self._analytics_store = analytics_store
        self._connected = False
        self._processing = False  # True while a codex exec is running
        self._message_queue: asyncio.Queue[tuple[str, str, str, str]] = asyncio.Queue()
        self._worker_task: asyncio.Task | None = None
        self._current_proc: asyncio.subprocess.Process | None = None  # For cleanup on disconnect

        self.agent_name = config.agent_name
        self.session_id = ""  # Codex thread_id for session resume
        self.codex_session_id = ""  # Same, kept for internal tracking
        self.created_at = time.time()
        self.last_active = self.created_at
        self.usage = SessionUsage()
        self._stats = {
            "turns": 0,
            "messages_sent": 0,
            "errors": 0,
            "reconnects": 0,
            "auto_restarts": 0,
        }
        self._current_activity = ""
        self._activity_log: list[str] = []
        self._current_thinking = ""
        self.account_info: dict = {"apiProvider": "codex_cli"}
        self._on_session_id = None  # async fn(agent_name, session_id)
        self._pending_session_id_update = ""  # Set by sync _handle_event, consumed by async worker
        self._internal_context_texts: list[str] = []
        self._current_turn_seq = 0
        self._last_user_message = ""  # For analytics keyword classification

        # Codex-specific config
        self._codex_model = config.model or ""
        self._approval_mode = "full-auto"  # Could be configurable later
        self._working_dir = config.working_dir or "."
        self._openai_api_key = config.provider_key or os.environ.get("OPENAI_API_KEY", "")
        self._reasoning_effort = config.thinking_effort or "medium"

        # MCP server config for Codex CLI (injected via -c flags)
        # Uses the shared MCP server's streamable HTTP transport
        self._mcp_servers = config.mcp_servers or {}

    async def connect(self) -> None:
        """Start the session. Sends wake prompt via codex exec."""
        self._connected = True
        self._analytics_session_started()

        # Start the message processing worker
        self._worker_task = asyncio.create_task(self._message_worker())

        _log(f"codex[{self.agent_name}]: connected, worker started")

        # Send wake prompt
        is_resume = bool(self.codex_session_id)
        ctx_block = ""
        if self._config.wake_context:
            ctx_block = f"\n\n── Saved State ──\n{self._config.wake_context}\n──────────────────"

        tools_hint = (
            "You have explicit pinky-messaging outreach tools: "
            "send, thread, react, send_gif, send_voice, send_photo, send_document, broadcast."
        )
        wake_prompt = (
            f"Session resumed after daemon restart.{ctx_block}\n\n"
            "Pick up where you left off. Users will message you through Telegram. "
            "Use send(chat_id, platform, text) for normal responses. "
            "Use thread(message_id, text) only when you want to quote/thread a specific message. "
            f"{tools_hint} If you do not call an outreach tool, Pinky may fall back to "
            "plain-text delivery based on agent settings."
            if is_resume else
            f"New session started.{ctx_block}\n\n"
            "You're connected via Pinky's message broker. Users will message you through Telegram. "
            "Use send(chat_id, platform, text) for normal responses. "
            "Use thread(message_id, text) only when you want to quote/thread a specific message. "
            f"{tools_hint} If you do not call an outreach tool, Pinky may fall back to "
            "plain-text delivery based on agent settings."
        )

        # Queue wake prompt (no chat routing — internal)
        self._record_internal_context_text(wake_prompt)
        await self._message_queue.put((wake_prompt, "", "", ""))

    async def send(
        self,
        prompt: str,
        platform: str = "",
        chat_id: str = "",
        message_id: str = "",
    ) -> None:
        """Send a message to the agent. Non-blocking — queued for processing."""
        if not self._connected:
            _log(f"codex[{self.agent_name}]: not connected, dropping message")
            return

        self.last_active = time.time()
        self._stats["messages_sent"] += 1
        self._last_user_message = prompt  # Capture for analytics classification
        self._analytics_log_activity(
            "prompt_submitted",
            metadata={"platform": platform, "chat_id": chat_id, "message_id": message_id},
        )

        # Log to conversation store
        if self._conversation_store:
            try:
                self._conversation_store.append(
                    self.id, "user", prompt,
                    platform=platform, chat_id=chat_id,
                )
            except Exception:
                pass

        await self._message_queue.put((prompt, platform, chat_id, message_id))
        _log(f"codex[{self.agent_name}]: queued message (chat={chat_id})")

    async def _message_worker(self) -> None:
        """Process queued messages sequentially via codex exec."""
        _log(f"codex[{self.agent_name}]: message worker started")
        try:
            while self._connected:
                prompt, platform, chat_id, message_id = await self._message_queue.get()
                try:
                    self._processing = True
                    self._current_turn_seq = self._stats["turns"] + 1
                    result = await self._exec_codex(prompt)

                    # Fire async session_id callback (set by sync _handle_event)
                    if self._pending_session_id_update and self._on_session_id:
                        try:
                            await self._on_session_id(
                                self.agent_name, self._pending_session_id_update
                            )
                        except Exception:
                            pass
                        self._pending_session_id_update = ""

                    # Build turn result
                    response_text = "\n".join(result.text_parts)
                    turn_result = StreamingTurnResult(
                        agent_name=self.agent_name,
                        session_id=self.id,
                        platform=platform,
                        chat_id=chat_id,
                        message_id=message_id,
                        response_text=response_text,
                        tool_uses=result.tool_uses,
                        used_outreach_tools=any(
                            _is_outreach_tool(tu.get("tool", ""))
                            for tu in result.tool_uses
                        ),
                        total_cost_usd=0.0,  # Codex doesn't report cost in JSONL
                        num_turns=1,
                        model_usage={
                            "input_tokens": result.input_tokens,
                            "output_tokens": result.output_tokens,
                            "cached_input_tokens": result.cached_input_tokens,
                        },
                    )

                    # Update stats
                    self.usage.input_tokens += result.input_tokens
                    self.usage.output_tokens += result.output_tokens
                    self._stats["turns"] += 1
                    self.last_active = time.time()
                    if response_text and not self._conversation_store:
                        self._record_internal_context_text(response_text)

                    # Fire response callback
                    if self._response_callback and (response_text or result.tool_uses):
                        try:
                            await self._response_callback(turn_result)
                        except Exception as e:
                            _log(f"codex[{self.agent_name}]: callback error: {e}")

                    # Log to conversation store
                    if response_text and self._conversation_store:
                        try:
                            metadata = {}
                            if result.tool_uses:
                                metadata["tool_uses"] = result.tool_uses
                            if result.input_tokens or result.output_tokens:
                                metadata["model_usage"] = {
                                    "input_tokens": result.input_tokens,
                                    "output_tokens": result.output_tokens,
                                }
                            self._conversation_store.append(
                                self.id, "assistant", response_text,
                                platform=platform, chat_id=chat_id,
                                metadata=metadata if metadata else None,
                            )
                        except Exception as e:
                            _log(f"codex[{self.agent_name}]: conversation store error: {e}")

                    # Clear activity tracking
                    self._current_activity = ""
                    self._activity_log = []

                    if result.failed:
                        self._stats["errors"] += 1
                        _log(f"codex[{self.agent_name}]: turn failed: {result.errors}")
                    self._current_turn_seq = 0

                except Exception as e:
                    self._current_turn_seq = 0
                    self._stats["errors"] += 1
                    _log(f"codex[{self.agent_name}]: exec error: {e}")
                finally:
                    self._processing = False

        except asyncio.CancelledError:
            _log(f"codex[{self.agent_name}]: worker cancelled")
        except Exception as e:
            _log(f"codex[{self.agent_name}]: worker error: {e}")
            self._connected = False

    async def _exec_codex(self, prompt: str) -> CodexTurnResult:
        """Run a single codex exec invocation and parse JSONL output.

        Streams stdout line-by-line for real-time activity tracking.
        """
        result = CodexTurnResult()

        # Build command
        cmd = ["codex", "exec"]

        # Resume previous session for multi-turn context
        if self.codex_session_id:
            cmd.extend(["resume", self.codex_session_id])

        is_resume = bool(self.codex_session_id)

        cmd.extend(["--json", "--full-auto"])

        if self._codex_model:
            cmd.extend(["-m", self._codex_model])

        # -C (working dir) is only valid for new sessions, not resume
        if not is_resume:
            cmd.extend(["-C", self._working_dir])

        # Reasoning effort (maps thinking_effort to Codex's model_reasoning_effort)
        if self._reasoning_effort and self._reasoning_effort != "medium":
            # Codex supports: low, medium, high
            effort = self._reasoning_effort
            if effort == "max":
                effort = "high"  # Codex doesn't have "max", map to highest
            cmd.extend(["-c", f'model_reasoning_effort="{effort}"'])

        # Inject MCP servers via -c flags (works on both new and resume calls)
        for server_name, server_config in self._mcp_servers.items():
            url = server_config.get("url", "")
            if url:
                cmd.extend(["-c", f'mcp_servers.{server_name}.url="{url}"'])
                # Inject custom headers (e.g. X-Agent-Name for identity scoping)
                headers = server_config.get("headers", {})
                for hdr_key, hdr_val in headers.items():
                    cmd.extend([
                        "-c", f'mcp_servers.{server_name}.http_headers.{hdr_key}="{hdr_val}"'
                    ])

        # Pass prompt via stdin to avoid shell escaping issues
        cmd.append("-")

        # Build environment
        env = {**os.environ}
        if self._openai_api_key:
            env["OPENAI_API_KEY"] = self._openai_api_key

        _log(
            f"codex[{self.agent_name}]: exec "
            f"{'resume ' + self.codex_session_id[:12] + ' ' if self.codex_session_id else ''}"
            f"(prompt: {len(prompt)} chars)"
        )

        proc = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._working_dir,
            )
            self._current_proc = proc

            # Feed prompt via stdin, then close stdin to signal EOF
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()

            # Stream stdout line-by-line for real-time activity tracking
            async def _read_and_parse():
                async for raw_line in proc.stdout:
                    line = raw_line.decode().strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_event(event, result)

            await asyncio.wait_for(_read_and_parse(), timeout=600)

            # Wait for process to finish and collect stderr
            await proc.wait()

            if proc.stderr:
                stderr_data = await proc.stderr.read()
                if stderr_data:
                    stderr_str = stderr_data.decode().strip()
                    if stderr_str:
                        _log(f"codex[{self.agent_name}]: stderr: {stderr_str[:200]}")

            if proc.returncode and proc.returncode != 0:
                _log(f"codex[{self.agent_name}]: exit code {proc.returncode}")
                if not result.text_parts and not result.errors:
                    result.errors.append(f"codex exited with code {proc.returncode}")
                    result.failed = True

        except asyncio.TimeoutError:
            result.failed = True
            result.errors.append("codex exec timed out after 600s")
            _log(f"codex[{self.agent_name}]: exec timed out")
            await self._emit_stream_event({"type": "turn_failed", "agent": self.agent_name, "session_id": self.id, "error": "codex exec timed out after 600s"})
            if proc:
                proc.kill()
                await proc.wait()
        except Exception as e:
            result.failed = True
            result.errors.append(str(e))
            _log(f"codex[{self.agent_name}]: exec exception: {e}")
            await self._emit_stream_event({"type": "turn_failed", "agent": self.agent_name, "session_id": self.id, "error": str(e)})
            if proc:
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
        finally:
            self._current_proc = None

        return result

    async def _emit_stream_event(self, event: dict) -> None:
        """Best-effort incremental stream event forwarding for UI consumers."""
        if not self._stream_event_callback:
            return
        try:
            await self._stream_event_callback(event)
        except Exception as e:
            _log(f"codex[{self.agent_name}]: stream event callback error: {e}")

    async def _handle_event(self, event: dict, result: CodexTurnResult) -> None:
        """Parse a single JSONL event and update result + activity tracking."""
        event_type = event.get("type", "")

        if event_type == "thread.started":
            thread_id = event.get("thread_id", "")
            if thread_id and thread_id != self.codex_session_id:
                self.codex_session_id = thread_id
                self.session_id = thread_id
                result.thread_id = thread_id
                _log(f"codex[{self.agent_name}]: thread_id={thread_id[:12]}")
                # _on_session_id callback is fired by the worker after _exec_codex returns.
                self._pending_session_id_update = thread_id
                self._analytics_session_started()

        elif event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")

            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    result.text_parts.append(text)
                    await self._emit_stream_event({
                        "type": "assistant_delta",
                        "agent": self.agent_name,
                        "session_id": self.id,
                        "delta": text,
                    })

            elif item_type == "command_execution":
                cmd_str = item.get("command", "")
                exit_code = item.get("exit_code")
                output = item.get("aggregated_output", "")
                result.tool_uses.append({
                    "tool": "Bash",
                    "input": {"command": cmd_str},
                    "exit_code": exit_code,
                    "result_preview": output[:200] if output else "",
                })
                desc = cmd_str[:60] if cmd_str else "command"
                self._current_activity = f"Bash — {desc}"
                self._activity_log.append(f"Bash — {desc}")
                await self._emit_stream_event({
                    "type": "tool_use",
                    "agent": self.agent_name,
                    "session_id": self.id,
                    "tool": "Bash",
                    "label": f"Bash — {desc}",
                })
                self._analytics_finish_tool_call(
                    tool_call_key=item.get("id", ""),
                    success=(exit_code == 0 if exit_code is not None else True),
                    metadata={"command": cmd_str, "exit_code": exit_code},
                )

            elif item_type == "file_edit":
                filepath = item.get("filepath", "")
                result.tool_uses.append({
                    "tool": "Edit",
                    "input": {"file_path": filepath},
                })
                fname = filepath.rsplit("/", 1)[-1] if filepath else ""
                self._current_activity = f"Edit — {fname}"
                self._activity_log.append(f"Edit — {fname}")
                await self._emit_stream_event({
                    "type": "tool_use",
                    "agent": self.agent_name,
                    "session_id": self.id,
                    "tool": "Edit",
                    "label": f"Edit — {fname}",
                })
                self._analytics_finish_tool_call(
                    tool_call_key=item.get("id", ""),
                    success=True,
                    metadata={"file_path": filepath},
                )

            elif item_type == "file_read":
                filepath = item.get("filepath", "")
                result.tool_uses.append({
                    "tool": "Read",
                    "input": {"file_path": filepath},
                })
                fname = filepath.rsplit("/", 1)[-1] if filepath else ""
                self._current_activity = f"Read — {fname}"
                self._activity_log.append(f"Read — {fname}")
                await self._emit_stream_event({
                    "type": "tool_use",
                    "agent": self.agent_name,
                    "session_id": self.id,
                    "tool": "Read",
                    "label": f"Read — {fname}",
                })
                self._analytics_finish_tool_call(
                    tool_call_key=item.get("id", ""),
                    success=True,
                    metadata={"file_path": filepath},
                )

            elif item_type == "mcp_tool_call":
                tool_name = item.get("tool_name", "")
                tool_input = item.get("input", {})
                result.tool_uses.append({
                    "tool": tool_name,
                    "input": tool_input,
                })
                self._current_activity = tool_name
                self._activity_log.append(tool_name)
                await self._emit_stream_event({
                    "type": "tool_use",
                    "agent": self.agent_name,
                    "session_id": self.id,
                    "tool": tool_name,
                    "label": tool_name,
                })
                self._analytics_finish_tool_call(
                    tool_call_key=item.get("id", ""),
                    success=True,
                    metadata={"tool_input": tool_input if isinstance(tool_input, dict) else {}},
                )

            elif item_type in ("function_call", "tool_call", "tool_use"):
                # Alternative Codex event types for tool calls
                tool_name = (
                    item.get("tool_name", "")
                    or item.get("name", "")
                    or item.get("function", {}).get("name", "")
                )
                tool_input = (
                    item.get("input", {})
                    or item.get("arguments", {})
                    or item.get("function", {}).get("arguments", {})
                )
                if isinstance(tool_input, str):
                    try:
                        import json as _json
                        tool_input = _json.loads(tool_input)
                    except Exception:
                        tool_input = {"raw": tool_input}
                result.tool_uses.append({
                    "tool": tool_name or item_type,
                    "input": tool_input if isinstance(tool_input, dict) else {},
                })
                label = tool_name or item_type
                self._current_activity = label
                self._activity_log.append(label)
                await self._emit_stream_event({
                    "type": "tool_use",
                    "agent": self.agent_name,
                    "session_id": self.id,
                    "tool": label,
                    "label": label,
                })
                self._analytics_finish_tool_call(
                    tool_call_key=item.get("id", ""),
                    success=True,
                    metadata={"tool_input": tool_input if isinstance(tool_input, dict) else {}},
                )

            elif item_type == "error":
                err_msg = item.get("message", "unknown error")
                result.errors.append(err_msg)
                await self._emit_stream_event({
                    "type": "turn_error",
                    "agent": self.agent_name,
                    "session_id": self.id,
                    "error": err_msg,
                })
                self._analytics_finish_tool_call(
                    tool_call_key=item.get("id", ""),
                    success=False,
                    error_type="item_error",
                    metadata={"message": err_msg},
                )

            else:
                # Log unrecognized item types so we can add proper handlers
                _log(
                    f"codex[{self.agent_name}]: unrecognized item type '{item_type}' "
                    f"keys={list(item.keys())}"
                )

        elif event_type == "item.started":
            item = event.get("item", {})
            item_type = item.get("type", "")
            if item_type == "command_execution":
                cmd_str = item.get("command", "")
                self._current_activity = f"Bash — {cmd_str[:60]}"
            tool_name, tool_namespace, metadata = self._tool_metadata_from_item(item)
            if tool_name:
                self._analytics_start_tool_call(
                    tool_call_key=item.get("id", ""),
                    tool_name=tool_name,
                    tool_namespace=tool_namespace,
                    metadata=metadata,
                )

        elif event_type == "turn.completed":
            usage = event.get("usage", {})
            result.input_tokens = usage.get("input_tokens", 0)
            result.output_tokens = usage.get("output_tokens", 0)
            result.cached_input_tokens = usage.get("cached_input_tokens", 0)
            self._current_thinking = ""
            self._current_activity = ""
            self._analytics_log_turn_usage(
                input_tokens=result.input_tokens,
                output_tokens=result.output_tokens,
                cached_input_tokens=result.cached_input_tokens,
                error=False,
            )
            self._analytics_log_activity(
                "turn_completed",
                metadata={
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cached_input_tokens": result.cached_input_tokens,
                },
            )
            await self._emit_stream_event({
                "type": "turn_completed",
                "agent": self.agent_name,
                "session_id": self.id,
                "usage": {
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cached_input_tokens": result.cached_input_tokens,
                },
            })

        elif event_type == "turn.failed":
            result.failed = True
            err = event.get("error", {})
            err_msg = err.get("message", "turn failed")
            result.errors.append(err_msg)
            self._analytics_log_activity("turn_failed", metadata={"error": err_msg})
            await self._emit_stream_event({
                "type": "turn_failed",
                "agent": self.agent_name,
                "session_id": self.id,
                "error": err_msg,
            })

        elif event_type == "error":
            err_msg = event.get("message", "unknown error")
            result.errors.append(err_msg)
            self._analytics_log_activity("turn_error", metadata={"error": err_msg})
            await self._emit_stream_event({
                "type": "turn_error",
                "agent": self.agent_name,
                "session_id": self.id,
                "error": err_msg,
            })

    async def force_restart(self) -> bool:
        """Force a context restart — clear codex session, start fresh."""
        if self._config.restart_guard:
            try:
                guard = self._config.restart_guard(self)
            except Exception:
                guard = {}
            if guard and not guard.get("restart_safe", False):
                _log(f"codex[{self.agent_name}]: restart blocked")
                return False

        _log(f"codex[{self.agent_name}]: force restarting")

        # Clear session ID
        if self._on_session_id:
            try:
                await self._on_session_id(self.agent_name, "")
            except Exception:
                pass

        await self.disconnect()

        # Refresh wake context
        if self._config.wake_context_builder:
            try:
                self._config.wake_context = self._config.wake_context_builder(self.agent_name)
            except Exception as e:
                _log(f"codex[{self.agent_name}]: failed to refresh wake context: {e}")

        self.codex_session_id = ""
        self.session_id = ""

        try:
            await self.connect()
            _log(f"codex[{self.agent_name}]: force restart complete")
            return True
        except Exception as e:
            _log(f"codex[{self.agent_name}]: force restart failed: {e}")
            self._connected = False
            return False

    async def idle_sleep(self) -> bool:
        """Put the session to sleep. Codex session ID preserved for resume."""
        if not self._connected:
            return False

        _log(f"codex[{self.agent_name}]: idle sleep triggered")

        # Ask agent to save state before sleeping
        try:
            await self._exec_codex(
                "[SYSTEM] You've been idle for over an hour. Auto-sleep is activating.\n\n"
                "Before your session is suspended:\n"
                "1. Use reflect() to persist key learnings and current task state\n"
                "2. Note what you were working on so you can resume later\n\n"
                "Your session will be preserved and resumed when you're needed next."
            )
            _log(f"codex[{self.agent_name}]: memory save prompt sent before idle sleep")
        except Exception as e:
            _log(f"codex[{self.agent_name}]: memory save failed before idle sleep: {e}")

        await self.disconnect()
        self._stats["auto_restarts"] += 1
        _log(f"codex[{self.agent_name}]: idle sleep complete")
        return True

    async def disconnect(self) -> None:
        """Disconnect — kill any running subprocess and cancel the worker."""
        self._connected = False
        self._analytics_log_activity("session_end")
        self._analytics_session_ended()

        # Kill any in-flight codex subprocess
        if self._current_proc:
            try:
                self._current_proc.kill()
                await self._current_proc.wait()
            except Exception:
                pass
            self._current_proc = None

        if self._worker_task and not self._worker_task.done():
            self._worker_task.cancel()
            try:
                await self._worker_task
            except asyncio.CancelledError:
                pass
        self._worker_task = None
        self._processing = False
        _log(f"codex[{self.agent_name}]: disconnected")

    @property
    def is_connected(self) -> bool:
        return self._connected

    @property
    def max_tokens(self) -> int:
        """Estimated max context tokens for this session's model."""
        model_name = (self._codex_model or self._config.model or "").lower()
        for key, size in MODEL_CONTEXT_SIZES.items():
            if key != "default" and key in model_name:
                return size
        return MODEL_CONTEXT_SIZES["default"]

    @property
    def estimated_tokens(self) -> int:
        """Estimate current context size from persisted chat plus internal prompts."""
        total = sum(
            max(1, len(text) // CHARS_PER_TOKEN)
            for text in self._internal_context_texts
            if text
        )
        if not self._conversation_store:
            return total
        try:
            history = self._conversation_store.get_history(self.id, limit=1000)
        except Exception:
            return total
        return total + sum(
            max(1, len(msg.content) // CHARS_PER_TOKEN)
            for msg in history
            if msg.content
        )

    @property
    def context_used_pct(self) -> float:
        if self.max_tokens <= 0:
            return 0.0
        return (self.estimated_tokens / self.max_tokens) * 100

    def get_context_info(self) -> dict:
        """Best-effort context info for APIs that expect session context details."""
        total = self.estimated_tokens
        max_tokens = self.max_tokens
        pct = round(total / max_tokens * 100, 1) if max_tokens > 0 else 0.0
        return {
            "total_tokens": total,
            "max_tokens": max_tokens,
            "percentage": pct,
            "categories": [],
            "mcp_tools": [],
        }

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "connected": self._connected,
            "processing": self._processing,
            "pending_messages": self._message_queue.qsize(),
            "current_activity": self._current_activity,
            "current_thinking": self._current_thinking,
            "activity_log": list(self._activity_log),
            "cost_usd": round(self.usage.total_cost_usd, 6),
            "account": self.account_info,
            "thinking_effort": self._reasoning_effort,
        }

    @property
    def id(self) -> str:
        return f"{self.agent_name}-{self._config.label or 'main'}"

    def _record_internal_context_text(self, text: str) -> None:
        """Track prompts/responses that do not appear in the conversation store."""
        if text:
            self._internal_context_texts.append(text)

    def _analytics_session_started(self) -> None:
        if not self._analytics_store:
            return
        try:
            self._analytics_store.ensure_session_fact(
                session_id=self.id,
                agent_name=self.agent_name,
                session_label=self._config.label or "main",
                provider=self.account_info.get("apiProvider", "codex_cli"),
                model=self._codex_model or self._config.model or "",
            )
        except Exception as e:
            _log(f"codex[{self.agent_name}]: analytics session start failed: {e}")

    def _analytics_session_ended(self) -> None:
        if not self._analytics_store:
            return
        try:
            self._analytics_store.mark_session_ended(self.id)
        except Exception as e:
            _log(f"codex[{self.agent_name}]: analytics session end failed: {e}")

    def _analytics_log_activity(self, event_type: str, *, metadata: dict | None = None) -> None:
        if not self._analytics_store:
            return
        try:
            self._analytics_store.log_activity(
                session_id=self.id,
                agent_name=self.agent_name,
                event_type=event_type,
                turn_seq=self._current_turn_seq or None,
                metadata=metadata,
            )
        except Exception as e:
            _log(f"codex[{self.agent_name}]: analytics activity failed: {e}")

    def _analytics_log_turn_usage(
        self,
        *,
        input_tokens: int,
        output_tokens: int,
        cached_input_tokens: int,
        error: bool,
    ) -> None:
        if not self._analytics_store or not self._current_turn_seq:
            return
        try:
            self._analytics_store.log_turn_usage(
                session_id=self.id,
                agent_name=self.agent_name,
                turn_seq=self._current_turn_seq,
                provider=self.account_info.get("apiProvider", "codex_cli"),
                model=self._codex_model or self._config.model or "",
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                cached_input_tokens=cached_input_tokens,
                error=error,
                user_message_snippet=self._last_user_message,
            )
        except Exception as e:
            _log(f"codex[{self.agent_name}]: analytics usage failed: {e}")

    def _analytics_start_tool_call(
        self,
        *,
        tool_call_key: str,
        tool_name: str,
        tool_namespace: str = "",
        metadata: dict | None = None,
    ) -> None:
        if not self._analytics_store or not tool_name:
            return
        try:
            self._analytics_store.start_tool_call(
                session_id=self.id,
                agent_name=self.agent_name,
                turn_seq=self._current_turn_seq or None,
                tool_call_key=tool_call_key,
                tool_name=tool_name,
                tool_namespace=tool_namespace,
                metadata=metadata,
            )
            self._analytics_log_activity(
                "tool_started",
                metadata={"tool_name": tool_name, "tool_namespace": tool_namespace, **(metadata or {})},
            )
        except Exception as e:
            _log(f"codex[{self.agent_name}]: analytics tool start failed: {e}")

    def _analytics_finish_tool_call(
        self,
        *,
        tool_call_key: str,
        success: bool,
        error_type: str = "",
        metadata: dict | None = None,
    ) -> None:
        if not self._analytics_store or not tool_call_key:
            return
        try:
            self._analytics_store.finish_tool_call(
                session_id=self.id,
                agent_name=self.agent_name,
                tool_call_key=tool_call_key,
                success=success,
                error_type=error_type,
                metadata=metadata,
            )
            self._analytics_log_activity(
                "tool_finished",
                metadata={"tool_call_key": tool_call_key, "success": success, **(metadata or {})},
            )
        except Exception as e:
            _log(f"codex[{self.agent_name}]: analytics tool finish failed: {e}")

    def _tool_metadata_from_item(self, item: dict) -> tuple[str, str, dict]:
        item_type = item.get("type", "")
        if item_type == "command_execution":
            return "Bash", "", {"command": item.get("command", "")}
        if item_type == "file_edit":
            return "Edit", "", {"file_path": item.get("filepath", "")}
        if item_type == "file_read":
            return "Read", "", {"file_path": item.get("filepath", "")}
        if item_type == "mcp_tool_call":
            tool_name = item.get("tool_name", "")
            namespace = tool_name.split(".", 1)[0] if "." in tool_name else ""
            return tool_name, namespace, {"tool_input": item.get("input", {})}
        if item_type in ("function_call", "tool_call", "tool_use"):
            tool_name = (
                item.get("tool_name", "")
                or item.get("name", "")
                or item.get("function", {}).get("name", "")
            )
            return tool_name or item_type, "", {"tool_input": item.get("input", {})}
        return "", "", {}
