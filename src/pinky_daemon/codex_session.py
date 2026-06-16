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

from pinky_daemon.codex_app_server import (
    CodexAppServerClient,
    CodexAppServerError,
    spawn_app_server,
)
from pinky_daemon.context_estimator import ContextTextEstimator
from pinky_daemon.sessions import MODEL_CONTEXT_SIZES, SessionUsage
from pinky_daemon.streaming_session import (
    StreamingSessionConfig,
    _is_outreach_tool,
    _log,
)
from pinky_daemon.transport_state import SessionState
from pinky_daemon.turn_response import TurnResponse
from pinky_daemon.wake_prompt import WakeReason


@dataclass
class CodexTurnResult:
    """Accumulated result from a single codex exec invocation."""

    thread_id: str = ""
    text_parts: list[str] = field(default_factory=list)
    tool_uses: list[dict] = field(default_factory=list)
    input_tokens: int = 0
    output_tokens: int = 0
    cached_input_tokens: int = 0
    # codex-cli 0.125+ added this to ``turn.completed.usage``. Tracked
    # separately so cost math (when codex starts reporting it) can
    # distinguish reasoning tokens from visible output tokens.
    reasoning_output_tokens: int = 0
    errors: list[str] = field(default_factory=list)
    failed: bool = False

    @property
    def uncached_input_tokens(self) -> int:
        """Billable (uncached) input under the daemon's disjoint convention.

        Codex/OpenAI report ``input_tokens`` INCLUSIVE of the cached prefix
        (``cached_input_tokens`` ⊆ ``input_tokens``), while the rest of the
        daemon — and the analytics cost math (``_compute_usage_cost``) —
        follow the Anthropic convention where ``input_tokens`` is the
        *uncached* remainder priced at the full input rate and the cached
        span is priced separately at the cached rate. Feeding the raw codex
        ``input_tokens`` into that math double-bills the cached tokens, so
        downstream cost/usage rows use this disjoint value instead.
        """
        return max(0, self.input_tokens - self.cached_input_tokens)


class CodexSession:
    """Agent session backed by Codex CLI.

    Drop-in replacement for StreamingSession — exposes the same public
    interface so the broker/API can treat them interchangeably.
    """

    def __init__(
        self,
        config: StreamingSessionConfig,
        *,
        response_callback=None,     # async fn(TurnResponse)
        conversation_store=None,    # ConversationStore for history logging
        cost_callback=None,         # fn(agent_name, cost_usd, input_tokens, output_tokens, resume_handle)
        stream_event_callback=None,  # async fn(event: dict) for incremental UI streaming
        analytics_store=None,
        registry=None,  # AgentRegistry — for server-side presence stamping
    ) -> None:
        self._config = config
        self._response_callback = response_callback
        self._cost_callback = cost_callback
        self._conversation_store = conversation_store
        self._stream_event_callback = stream_event_callback
        self._analytics_store = analytics_store
        self._registry = registry
        self._connected = False
        self._idle_sleeping = False
        self._connect_attempted = False  # distinguishes UNINITIALIZED from DEAD
        self._processing = False  # True while a codex exec is running
        self._message_queue: asyncio.Queue[tuple[str, str, str, str]] = asyncio.Queue()
        # Serializes _exec_codex between the worker and out-of-band callers
        # (idle_sleep's save turn): two concurrent execs would resume the same
        # codex thread and clobber the shared _current_proc / app-server
        # turn-correlation state.
        self._exec_lock = asyncio.Lock()
        self._worker_task: asyncio.Task | None = None
        self._current_proc: asyncio.subprocess.Process | None = None  # For cleanup on disconnect
        # #591 P1#2 (Murzik round-2): callback fired after the NEXT
        # codex-exec succeeds. ``connect()`` sets it when on_wake_delivered
        # is wired, so the cycle-gate boundary advances only on confirmed
        # wake-prompt delivery (post-exec), not at queue.put time. Single-
        # pending semantics — wake prompts only come from connect(),
        # which is mutually exclusive with another in-flight wake.
        self._pending_wake_callback: object = None  # Callable() -> None

        self.agent_name = config.agent_name
        self.resume_handle = ""  # Codex thread_id used to resume (opaque resume token)
        self.codex_session_id = ""  # Last-seen thread_id, dedupe state for the resume-handle callback
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
        self._on_resume_handle = None  # async fn(agent_name, resume_handle)
        self._pending_resume_handle_update = ""  # Set by sync _handle_event, consumed by async worker
        self._context_estimator = ContextTextEstimator()
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

        # Tier 2 (#98): when PINKY_CODEX_APP_SERVER=1, route turns through a
        # long-lived ``codex app-server`` JSON-RPC connection instead of
        # spawning ``codex exec`` per message. Legacy exec stays the default
        # and the fallback until the soak proves the app-server path. One
        # app-server process + one thread per session here (chunk 2); a shared
        # supervisor lands in chunk 3.
        self._use_app_server = os.environ.get("PINKY_CODEX_APP_SERVER") == "1"
        self._app_client: CodexAppServerClient | None = None
        self._app_proc: asyncio.subprocess.Process | None = None
        # Active turn correlation: the read loop dispatches notifications onto
        # a single handler, but turns are processed sequentially by the worker,
        # so a single in-flight result + completion future is sufficient.
        self._active_turn_result: CodexTurnResult | None = None
        self._turn_done: asyncio.Future | None = None
        # Latest per-turn token breakdown from thread/tokenUsage/updated —
        # app-server reports usage out-of-band, not inside turn/completed.
        self._appserver_last_usage: dict = {}

    async def connect(self) -> None:
        """Start the session. Sends wake prompt via codex exec."""
        self._connect_attempted = True  # tracks state ≠ UNINITIALIZED
        if self._connected:
            self._idle_sleeping = False
            return

        self._connected = True
        self._idle_sleeping = False
        self._analytics_session_started()

        # Start the message processing worker
        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._message_worker())
            # WEDGE FIX: surface silent worker death. Without this
            # callback, an unhandled exception or unexpected loop exit
            # leaves the broker thinking the session is alive while the
            # queue piles up forever. The callback flips ``_connected``
            # so the broker's reconnect path can re-spawn us.
            self._worker_task.add_done_callback(self._worker_done_callback)

        _log(f"codex[{self.agent_name}]: connected, worker started")

        # Send wake prompt
        is_resume = bool(self.codex_session_id)
        wake_reason = WakeReason.RESUME if is_resume else WakeReason.NEW_SESSION
        # #591 — rebuild wake-context body with the now-known reason so
        # the builder can gate the saved-state manifest (RESUME drops
        # the bulk; NEW_SESSION emits it). The static
        # ``self._config.wake_context`` set at config-create time was
        # built without reason context and is now a commit=False preview
        # only — relying on it here would re-emit a stale manifest on
        # warm Codex resumes. TypeError fallback keeps legacy 1-arg
        # builders working.
        wake_context_body = self._config.wake_context or ""
        if self._config.wake_context_builder:
            try:
                wake_context_body = self._config.wake_context_builder(
                    self.agent_name, wake_reason
                )
            except TypeError:
                pass
            except Exception as e:
                _log(
                    f"codex[{self.agent_name}]: wake context rebuild failed: {e} "
                    "— using stored body"
                )
        ctx_block = ""
        if wake_context_body:
            ctx_block = f"\n\n── Saved State ──\n{wake_context_body}\n──────────────────"

        tools_hint = (
            "You have explicit pinky-messaging outreach tools: "
            "send, thread, react, send_gif, send_voice, send_photo, send_document, send_video, broadcast."
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

        # #591 P1#2 (Murzik round-2): arm the post-delivery callback
        # BEFORE the put so the worker can fire it after _exec_codex
        # actually runs the wake prompt. Firing at queue.put time would
        # advance the cycle-gate boundary against a wake that may
        # never reach the model (exec failure, subprocess crash, etc.),
        # eating the directive on the next RESUME.
        if self._config.on_wake_delivered:
            _config_cb = self._config.on_wake_delivered
            _agent_name = self.agent_name
            _wake_reason = wake_reason

            def _wake_delivered_cb() -> None:
                _config_cb(_agent_name, _wake_reason)

            self._pending_wake_callback = _wake_delivered_cb

        # Queue wake prompt (no chat routing — internal)
        self._record_internal_context_text(wake_prompt)
        await self._message_queue.put((wake_prompt, "", "", ""))

    async def send(
        self,
        prompt: str,
        platform: str = "",
        chat_id: str = "",
        message_id: str = "",
        agent_hint: str = "",
    ) -> None:
        """Send a message to the agent. Non-blocking — queued for processing.

        Args:
            prompt: The formatted message to send.
            platform: The platform the message came from (e.g. 'telegram').
            chat_id: The chat_id to route the response back to.
            message_id: The source message_id to route reactions back to.
            agent_hint: Extra context appended to the queued prompt but NOT
                stored in conversation history (e.g. reply-platform hints).
                Mirrors StreamingSession.send so the broker can call both
                session types polymorphically.
        """
        if not self._connected:
            _log(f"codex[{self.agent_name}]: not connected, dropping message")
            return

        self.last_active = time.time()
        self._stats["messages_sent"] += 1
        # Extract raw user text for analytics classification (strip broker headers)
        self._last_user_message = self._strip_prompt_headers(prompt)
        self._analytics_log_activity(
            "prompt_submitted",
            metadata={"platform": platform, "chat_id": chat_id, "message_id": message_id},
        )

        # Log to conversation store BEFORE appending the hint so chat history
        # only contains the user's actual prompt, not the agent-only routing
        # hint. Matches StreamingSession's "stored prompt vs. queried prompt"
        # split (see streaming_session.py: query is `prompt + agent_hint`).
        if self._conversation_store:
            try:
                self._conversation_store.append(
                    self.id, "user", prompt,
                    platform=platform, chat_id=chat_id,
                )
            except Exception as e:
                _log(f"codex[{self.agent_name}]: conversation store append failed: {e}")

        queued_prompt = prompt + agent_hint if agent_hint else prompt
        await self._message_queue.put((queued_prompt, platform, chat_id, message_id))
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
                    async with self._exec_lock:
                        result = await self._exec_codex(prompt)

                    # #591 P1#2 (Murzik round-2): fire the pending wake
                    # callback after exec confirms delivery. Only on
                    # success — failed execs leave the boundary intact
                    # so the next attempt re-emits the directive. The
                    # callback is one-shot per arm; cleared whether
                    # fired or skipped so a subsequent non-wake turn
                    # doesn't fire it spuriously.
                    if self._pending_wake_callback is not None:
                        cb = self._pending_wake_callback
                        self._pending_wake_callback = None
                        if not result.failed:
                            try:
                                cb()
                            except Exception as _cb_e:
                                _log(
                                    f"codex[{self.agent_name}]: "
                                    f"on_wake_delivered callback failed: {_cb_e}"
                                )

                    # Fire async resume-handle callback (set by sync _handle_event)
                    if self._pending_resume_handle_update and self._on_resume_handle:
                        try:
                            await self._on_resume_handle(
                                self.agent_name, self._pending_resume_handle_update
                            )
                        except Exception:
                            pass
                        self._pending_resume_handle_update = ""

                    # Build turn result
                    response_text = "\n".join(result.text_parts)
                    turn_result = TurnResponse(
                        agent_name=self.agent_name,
                        session_id=self.id,
                        platform=platform,
                        chat_id=chat_id,
                        message_id=message_id,
                        text=response_text,
                        tool_uses=result.tool_uses,
                        used_outreach_tools=any(
                            _is_outreach_tool(tu.get("tool", ""))
                            for tu in result.tool_uses
                        ),
                        usage={
                            "input_tokens": result.uncached_input_tokens,
                            "output_tokens": result.output_tokens,
                            "cached_input_tokens": result.cached_input_tokens,
                        },
                        # Per-turn dollar figure stays 0.0 here — Codex JSONL
                        # carries no cost. The authoritative Codex cost is
                        # computed by the analytics store from token counts +
                        # the seeded OpenAI pricing (compute_usage_cost), same
                        # as the tmux transport (#648).
                        total_cost_usd=0.0,
                        num_turns=1,
                        model_usage={
                            "input_tokens": result.uncached_input_tokens,
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

    def _worker_done_callback(self, task: asyncio.Task) -> None:
        """Surface silent worker death.

        Called via ``task.add_done_callback`` when the worker exits for
        any reason. The worker normally only exits when ``_connected``
        is already False (graceful disconnect) or via an unhandled
        exception (caught + flips ``_connected``). The pathological
        case this guards is: worker exits while ``_connected`` is still
        True — broker still thinks we're alive, queue keeps accepting
        sends, nothing processes them. Flip ``_connected`` so the
        broker's reconnect path can re-spawn us.

        Defensive: callback is sync (asyncio constraint) and runs from
        the event loop's task-finalisation step, so we keep work
        minimal — just log + flip the flag. Anything heavier would
        risk re-entering broker code at an awkward moment.
        """
        if task.cancelled():
            return  # graceful — disconnect() cancelled us
        exc = task.exception()
        if self._connected:
            if exc is not None:
                _log(
                    f"codex[{self.agent_name}]: WORKER DIED with "
                    f"{type(exc).__name__}: {exc} — flipping _connected=False"
                )
            else:
                _log(
                    f"codex[{self.agent_name}]: WORKER EXITED unexpectedly "
                    f"(no exception, no cancel) — flipping _connected=False"
                )
            self._connected = False

    def is_healthy(self) -> dict:
        """Return diagnostic state for the broker / liveness probe.

        Used by the wedge-detection path to distinguish "session alive"
        from "session looks alive but worker is dead." Cheap synchronous
        check — broker can poll without piling up async work.

        Returns:
            dict with:
              connected: bool — the session's own view
              worker_alive: bool — worker task exists + not done
              processing: bool — currently inside _exec_codex
              queue_depth: int — pending messages waiting on worker
              seconds_since_active: float — staleness signal
              wedged: bool — True if the session is in a stuck shape
                (connected + worker dead, OR processing + very stale)
        """
        now = time.time()
        worker_alive = bool(
            self._worker_task and not self._worker_task.done()
        )
        seconds_since_active = max(0.0, now - self.last_active)
        # Wedge shapes we can detect synchronously:
        # 1. broker thinks we're connected but worker is dead
        # 2. processing flag stuck True for longer than the inner 600s
        #    timeout could reasonably take + a generous buffer
        wedged = bool(
            (self._connected and not worker_alive)
            or (self._processing and seconds_since_active > 900)
        )
        return {
            "connected": self._connected,
            "worker_alive": worker_alive,
            "processing": self._processing,
            "queue_depth": self._message_queue.qsize(),
            "seconds_since_active": round(seconds_since_active, 1),
            "wedged": wedged,
        }

    def _build_codex_cmd(self) -> list[str]:
        """Build the codex CLI invocation for the current session state.

        Extracted from `_exec_codex` so the command construction is unit-
        testable. Returns a list suitable for `asyncio.create_subprocess_exec`,
        terminating with `-` to signal that the prompt comes via stdin.

        Resume vs fresh:
          - Fresh: `codex exec ...`
          - Resume: `codex exec resume <session_id> ...`

        Sandbox/approval:
          --dangerously-bypass-approvals-and-sandbox (a.k.a. "yolo") bypasses
          both the sandbox AND the approval channel. In `codex exec --json`
          there is no approval channel surfaced to us, so any MCP `tools/call`
          (and other network-touching tools) is auto-cancelled in ~8ms with
          "user cancelled MCP tool call" unless we bypass approvals too.

          We previously used `--sandbox=danger-full-access`, which works on
          `codex exec` for fresh sessions but is rejected by `codex exec
          resume` ("error: unexpected argument '--sandbox' found") — every
          reply to an existing thread therefore failed silently (see #351).
          The yolo flag is accepted on both subcommands and bypasses both
          gates uniformly.

          Safety note: codex agents already have shell + write access via
          exec_command, so the sandbox gate isn't buying us meaningful
          safety. The daemon is the trust boundary. Do NOT combine with
          `--full-auto` — that's a convenience alias for `workspace-write +
          on-failure` and overrides explicit flags (verified 2026-04-27 in
          PR #333).
        """
        cmd = ["codex", "exec"]

        # Resume previous session for multi-turn context
        if self.codex_session_id:
            cmd.extend(["resume", self.codex_session_id])

        is_resume = bool(self.codex_session_id)

        cmd.extend(["--json", "--dangerously-bypass-approvals-and-sandbox"])

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
        return cmd

    async def _exec_codex(self, prompt: str) -> CodexTurnResult:
        """Run a single codex exec invocation and parse JSONL output.

        Streams stdout line-by-line for real-time activity tracking.
        """
        if self._use_app_server:
            return await self._exec_codex_app_server(prompt)

        result = CodexTurnResult()

        cmd = self._build_codex_cmd()

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
        stderr_task = None
        try:
            # limit=10MB — codex emits large tool-result events that exceed
            # asyncio's default 64KB StreamReader limit and kill the session
            # with LimitOverrunError. 10MB is comfortably above observed max.
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
                cwd=self._working_dir,
                limit=10 * 1024 * 1024,
            )
            self._current_proc = proc

            # Drain stderr concurrently: an undrained PIPE blocks the child
            # once the OS buffer (~64KiB) fills, wedging the turn (same hazard
            # codex_app_server._drain_stderr guards against).
            if proc.stderr:
                stderr_task = asyncio.create_task(proc.stderr.read())

            # Feed prompt via stdin, then close stdin to signal EOF
            proc.stdin.write(prompt.encode())
            await proc.stdin.drain()
            proc.stdin.close()
            await proc.stdin.wait_closed()

            # Stream stdout line-by-line for real-time activity tracking.
            # Defensive: skip-and-continue on a single oversized line rather
            # than letting the session tear down.
            #
            # StreamReader.readline() catches LimitOverrunError internally,
            # drains its buffer past the separator, and re-raises as
            # ValueError("Separator is not found, and chunk exceed the limit").
            # So we catch ValueError (narrowly filtered by message) rather
            # than LimitOverrunError, and no manual buffer drain is needed.
            async def _read_and_parse():
                while True:
                    try:
                        raw_line = await proc.stdout.readline()
                    except ValueError as exc:
                        msg = str(exc)
                        if "Separator is not found" not in msg and "chunk exceed" not in msg:
                            # Unrelated ValueError — don't swallow it
                            raise
                        _log(
                            f"codex[{self.agent_name}]: oversized stdout line "
                            f"({msg}) — skipping"
                        )
                        continue
                    if not raw_line:
                        break
                    line = raw_line.decode(errors="replace").strip()
                    if not line:
                        continue
                    try:
                        event = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    await self._handle_event(event, result)

            await asyncio.wait_for(_read_and_parse(), timeout=600)

            # Wait for process to finish and collect stderr.
            # WEDGE FIX: the proc.wait() was previously unbounded — if the
            # codex subprocess closed its stdout (EOF on _read_and_parse)
            # but never actually exited (zombie / unflushed buffer / OS
            # reaping race), the worker hung on this await indefinitely
            # with `_processing=True`, so no further queued messages got
            # processed and the session looked alive in the broker but
            # was wedged from the user's POV. 30s is generous: codex
            # always exits within ~1s of stdout EOF in practice.
            try:
                await asyncio.wait_for(proc.wait(), timeout=30)
            except asyncio.TimeoutError:
                _log(
                    f"codex[{self.agent_name}]: subprocess didn't exit "
                    f"within 30s of stdout EOF — killing"
                )
                try:
                    proc.kill()
                    await asyncio.wait_for(proc.wait(), timeout=5)
                except Exception:
                    pass

            if stderr_task is not None:
                stderr_data = await stderr_task
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
                try:
                    proc.kill()
                    await proc.wait()
                except Exception:
                    pass
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
            if stderr_task is not None and not stderr_task.done():
                stderr_task.cancel()

        return result

    # ── Codex app-server path (#98 Tier 2) ────────────────────────────────
    #
    # The app-server hosts a long-lived Thread; each user message is a
    # ``turn/start`` against it. The wire protocol is JSON-RPC over stdio,
    # handled by CodexAppServerClient. The slash-notation notifications it
    # emits are translated back onto the legacy dot-notation event shapes
    # so ``_handle_event`` — and all its activity/analytics/stream-event
    # bookkeeping — is reused verbatim.

    _APPROVAL_POLICY = "never"  # AskForApproval — full-auto, never prompt
    _SANDBOX_MODE = "danger-full-access"  # SandboxMode — parity with the exec yolo flag

    async def _ensure_app_server(self) -> None:
        """Spawn + initialise the app-server connection if not already live."""
        if self._app_client is not None and self._app_proc is not None:
            if self._app_proc.returncode is None:
                return  # still running
            # Process died under us — drop the stale client and respawn.
            await self._teardown_app_server()

        env = {**os.environ}
        if self._openai_api_key:
            env["OPENAI_API_KEY"] = self._openai_api_key

        self._app_client, self._app_proc = await spawn_app_server(
            cwd=self._working_dir,
            env=env,
            notification_handler=self._on_appserver_notification,
            server_request_handler=self._on_appserver_request,
            log=_log,
        )
        await self._app_client.initialize(name="pinkybot", version="1")
        _log(f"codex[{self.agent_name}]: app-server connected (pid={self._app_proc.pid})")

    async def _teardown_app_server(self) -> None:
        """Close the client and kill the process. Idempotent."""
        if self._app_client is not None:
            try:
                await self._app_client.close()
            except Exception:
                pass
            self._app_client = None
        if self._app_proc is not None:
            try:
                if self._app_proc.returncode is None:
                    self._app_proc.kill()
                    await asyncio.wait_for(self._app_proc.wait(), timeout=5)
            except Exception:
                pass
            self._app_proc = None

    def _appserver_config(self) -> dict:
        """Build the per-thread ``config`` override for MCP servers.

        Mirrors _build_codex_cmd's ``-c mcp_servers.<name>.url=...`` injection,
        expressed as the nested config object app-server's thread/start accepts.
        """
        mcp: dict = {}
        for name, server_config in self._mcp_servers.items():
            url = server_config.get("url", "")
            if not url:
                continue
            entry: dict = {"url": url}
            headers = server_config.get("headers", {})
            if headers:
                entry["http_headers"] = dict(headers)
            mcp[name] = entry
        return {"mcp_servers": mcp} if mcp else {}

    def _appserver_effort(self) -> str | None:
        """Map the configured thinking_effort onto a ReasoningEffort value."""
        effort = self._reasoning_effort or ""
        if not effort:
            return None
        if effort == "max":
            return "high"  # parity with legacy: Codex has no "max"
        if effort in ("none", "minimal", "low", "medium", "high", "xhigh"):
            return effort
        return None

    async def _exec_codex_app_server(self, prompt: str) -> CodexTurnResult:
        """Run a single turn over the long-lived app-server connection."""
        result = CodexTurnResult()
        try:
            await self._ensure_app_server()
        except Exception as e:  # noqa: BLE001 — surface as a failed turn, keep session alive
            result.failed = True
            result.errors.append(f"app-server connect failed: {e}")
            _log(f"codex[{self.agent_name}]: app-server connect failed: {e}")
            await self._emit_stream_event({
                "type": "turn_failed", "agent": self.agent_name,
                "session_id": self.id, "error": str(e),
            })
            return result

        client = self._app_client
        assert client is not None
        loop = asyncio.get_running_loop()
        self._active_turn_result = result
        self._turn_done = loop.create_future()
        self._appserver_last_usage = {}

        config = self._appserver_config()
        _log(
            f"codex[{self.agent_name}]: app-server turn "
            f"{'resume ' + self.codex_session_id[:12] + ' ' if self.codex_session_id else 'new '}"
            f"(prompt: {len(prompt)} chars)"
        )

        try:
            if self.codex_session_id:
                params: dict = {
                    "threadId": self.codex_session_id,
                    "approvalPolicy": self._APPROVAL_POLICY,
                    "sandbox": self._SANDBOX_MODE,
                }
                if self._codex_model:
                    params["model"] = self._codex_model
                if config:
                    params["config"] = config
                resp = await client.request("thread/resume", params)
            else:
                params = {
                    "cwd": self._working_dir,
                    "approvalPolicy": self._APPROVAL_POLICY,
                    "sandbox": self._SANDBOX_MODE,
                }
                if self._codex_model:
                    params["model"] = self._codex_model
                if config:
                    params["config"] = config
                resp = await client.request("thread/start", params)

            # The thread/started notification normally sets codex_session_id via
            # _handle_event; cover the case where only the response carries it.
            thread_id = ""
            if isinstance(resp, dict):
                thread_id = (resp.get("thread") or {}).get("id", "")
            if thread_id and thread_id != self.codex_session_id:
                self.codex_session_id = thread_id
                self.resume_handle = thread_id
                result.thread_id = thread_id
                self._pending_resume_handle_update = thread_id

            turn_params: dict = {
                "threadId": self.codex_session_id,
                "input": [{"type": "text", "text": prompt}],
            }
            effort = self._appserver_effort()
            if effort:
                turn_params["effort"] = effort
            await client.request("turn/start", turn_params)

            # turn/start returns immediately; notifications drive the turn.
            # _on_appserver_notification resolves _turn_done on turn/completed.
            await asyncio.wait_for(self._turn_done, timeout=600)

        except asyncio.TimeoutError:
            result.failed = True
            result.errors.append("codex app-server turn timed out after 600s")
            _log(f"codex[{self.agent_name}]: app-server turn timed out")
            await self._emit_stream_event({
                "type": "turn_failed", "agent": self.agent_name,
                "session_id": self.id, "error": "codex app-server turn timed out after 600s",
            })
            # A wedged thread is unrecoverable — drop the connection so the next
            # turn respawns and resumes cleanly.
            await self._teardown_app_server()
        except CodexAppServerError as e:
            result.failed = True
            result.errors.append(str(e))
            _log(f"codex[{self.agent_name}]: app-server error: {e}")
            await self._emit_stream_event({
                "type": "turn_failed", "agent": self.agent_name,
                "session_id": self.id, "error": str(e),
            })
            await self._teardown_app_server()
        except Exception as e:  # noqa: BLE001 — keep the session alive across turn failures
            result.failed = True
            result.errors.append(str(e))
            _log(f"codex[{self.agent_name}]: app-server turn exception: {e}")
            await self._emit_stream_event({
                "type": "turn_failed", "agent": self.agent_name,
                "session_id": self.id, "error": str(e),
            })
            await self._teardown_app_server()
        finally:
            self._active_turn_result = None
            self._turn_done = None

        return result

    async def _on_appserver_notification(self, method: str, params: dict) -> None:
        """Translate an app-server notification onto the legacy event path."""
        # Incremental streaming text — UI only; full text arrives via the
        # final item/completed agentMessage (matches the legacy non-delta path).
        if method == "item/agentMessage/delta":
            delta = params.get("delta", "")
            if delta:
                await self._emit_stream_event({
                    "type": "assistant_delta", "agent": self.agent_name,
                    "session_id": self.id, "delta": delta,
                })
            return

        if method == "thread/tokenUsage/updated":
            token_usage = params.get("tokenUsage") or {}
            self._appserver_last_usage = (
                token_usage.get("last") or token_usage.get("total") or {}
            )
            return

        event = self._appserver_to_event(method, params)
        if event is not None and self._active_turn_result is not None:
            await self._handle_event(event, self._active_turn_result)

        if method == "turn/completed" and self._turn_done is not None:
            if not self._turn_done.done():
                self._turn_done.set_result(None)

    async def _on_appserver_request(self, method: str, params: dict) -> dict:
        """Auto-approve server->client requests (full-auto).

        With approvalPolicy=never + danger-full-access these should not fire,
        but the handler is a defensive net: an unanswered approval request
        would otherwise cancel the underlying tool call (cf. #351).
        """
        if method in ("execCommandApproval", "applyPatchApproval"):
            return {"decision": "approved"}
        if method in (
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
        ):
            return {"decision": "accept"}
        if method == "item/permissions/requestApproval":
            return {"permissions": {}, "scope": "session"}
        # Elicitations / tool-call / user-input requests: nothing useful to add
        # under full-auto; an empty result lets the server proceed.
        return {}

    def _appserver_to_event(self, method: str, params: dict) -> dict | None:
        """Map a slash-notation notification onto a legacy dot-notation event."""
        if method == "thread/started":
            return {
                "type": "thread.started",
                "thread_id": (params.get("thread") or {}).get("id", ""),
            }
        if method == "turn/started":
            return {"type": "turn.started"}
        if method == "item/started":
            return {
                "type": "item.started",
                "item": self._appserver_item_to_legacy(params.get("item") or {}),
            }
        if method == "item/completed":
            return {
                "type": "item.completed",
                "item": self._appserver_item_to_legacy(params.get("item") or {}),
            }
        if method == "turn/completed":
            turn = params.get("turn") or {}
            if turn.get("status") == "failed":
                return {"type": "turn.failed", "error": turn.get("error") or {}}
            return {"type": "turn.completed", "usage": self._appserver_usage_to_legacy()}
        if method == "error":
            err = params.get("error")
            msg = err.get("message", "unknown error") if isinstance(err, dict) else str(err)
            return {"type": "error", "message": msg}
        return None

    def _appserver_usage_to_legacy(self) -> dict:
        """Convert the captured camelCase token breakdown to legacy snake_case."""
        u = self._appserver_last_usage or {}
        return {
            "input_tokens": u.get("inputTokens", 0),
            "output_tokens": u.get("outputTokens", 0),
            "cached_input_tokens": u.get("cachedInputTokens", 0),
            "reasoning_output_tokens": u.get("reasoningOutputTokens", 0),
        }

    @staticmethod
    def _appserver_item_to_legacy(item: dict) -> dict:
        """Translate an app-server ThreadItem onto the legacy item shape.

        App-server uses camelCase types/fields (``agentMessage``, ``exitCode``);
        the legacy ``codex exec --json`` stream — which _handle_event parses —
        uses snake_case (``agent_message``, ``exit_code``). Unknown types pass
        through with their type so _handle_event logs them benignly.
        """
        item_type = item.get("type", "")
        item_id = item.get("id", "")
        if item_type == "agentMessage":
            return {"type": "agent_message", "text": item.get("text", ""), "id": item_id}
        if item_type == "commandExecution":
            return {
                "type": "command_execution",
                "command": item.get("command", ""),
                "exit_code": item.get("exitCode"),
                "aggregated_output": item.get("aggregatedOutput", ""),
                "id": item_id,
            }
        if item_type == "fileChange":
            changes = item.get("changes") or []
            filepath = ""
            if isinstance(changes, list) and changes and isinstance(changes[0], dict):
                filepath = changes[0].get("path", "")
            return {"type": "file_edit", "filepath": filepath, "id": item_id}
        if item_type == "mcpToolCall":
            return {
                "type": "mcp_tool_call",
                "tool_name": item.get("tool", ""),
                "input": item.get("arguments") or {},
                "id": item_id,
            }
        if item_type == "dynamicToolCall":
            return {
                "type": "function_call",
                "tool_name": item.get("tool", ""),
                "input": item.get("arguments") or {},
                "id": item_id,
            }
        if item_type == "error":
            return {"type": "error", "message": item.get("message", "unknown error"), "id": item_id}
        return {"type": item_type, "id": item_id}

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
                self.resume_handle = thread_id
                result.thread_id = thread_id
                _log(f"codex[{self.agent_name}]: thread_id={thread_id[:12]}")
                # _on_resume_handle callback is fired by the worker after _exec_codex returns.
                self._pending_resume_handle_update = thread_id
                self._analytics_session_started()

        elif event_type == "item.completed":
            item = event.get("item", {})
            item_type = item.get("type", "")

            if item_type == "agent_message":
                text = item.get("text", "")
                if text:
                    result.text_parts.append(text)
                    # App-server mode already streamed this text incrementally
                    # via item/agentMessage/delta; emitting the full text again
                    # would duplicate it in the live chat stream.
                    if not self._use_app_server:
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
                    # PII-safe: no raw command string. arg_keys records that
                    # Bash has one argument ("command"); exit_code is numeric.
                    metadata={"arg_keys": ["command"], "exit_code": exit_code},
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
                    # PII-safe: no raw filepath.
                    metadata={"arg_keys": ["file_path"]},
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
                    # PII-safe: no raw filepath.
                    metadata={"arg_keys": ["file_path"]},
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
                    # PII-safe: record argument key names only, not values.
                    metadata={
                        "arg_keys": sorted(tool_input.keys()) if isinstance(tool_input, dict) else []
                    },
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
                    # PII-safe: record argument key names only, not values.
                    metadata={
                        "arg_keys": sorted(tool_input.keys()) if isinstance(tool_input, dict) else []
                    },
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
                    # PII-safe: error_type is captured above; err_msg may contain
                    # file paths / command output / user data, so strip it here.
                    metadata={"arg_keys": []},
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
            # codex-cli 0.125+ usage block. ``.get`` with a 0 default so
            # older codex versions (without this field) stay benign.
            result.reasoning_output_tokens = usage.get(
                "reasoning_output_tokens", 0
            )
            self._current_thinking = ""
            self._current_activity = ""
            # Log the UNCACHED input (cached span priced separately) so the
            # analytics cost math doesn't double-bill the cached tokens. The
            # observability metadata/stream below keep the raw codex totals.
            self._analytics_log_turn_usage(
                input_tokens=result.uncached_input_tokens,
                output_tokens=result.output_tokens,
                cached_input_tokens=result.cached_input_tokens,
                error=False,
            )
            # Reasoning tokens are a SUBSET of output_tokens (output = visible +
            # reasoning) and are intentionally NOT billed — Codex cost is computed
            # from input/output/cached only. Guard the invariant: reasoning >
            # output means the Codex usage parse mis-attributed tokens. Surface it
            # as an observability flag (queryable in analytics_activity_events)
            # rather than silently trusting the number or corrupting billed counts.
            reasoning_tokens = result.reasoning_output_tokens
            reasoning_gt_output = reasoning_tokens > result.output_tokens
            self._analytics_log_activity(
                "turn_completed",
                metadata={
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cached_input_tokens": result.cached_input_tokens,
                    "reasoning_output_tokens": reasoning_tokens,
                    "reasoning_gt_output": reasoning_gt_output,
                },
            )
            self._stamp_last_seen()
            await self._emit_stream_event({
                "type": "turn_completed",
                "agent": self.agent_name,
                "session_id": self.id,
                "usage": {
                    "input_tokens": result.input_tokens,
                    "output_tokens": result.output_tokens,
                    "cached_input_tokens": result.cached_input_tokens,
                    "reasoning_output_tokens": reasoning_tokens,
                    "reasoning_gt_output": reasoning_gt_output,
                },
            })

        elif event_type == "turn.failed":
            result.failed = True
            err = event.get("error", {})
            err_msg = err.get("message", "turn failed")
            result.errors.append(err_msg)
            self._analytics_log_activity("turn_failed", metadata={"error": err_msg})
            self._stamp_last_seen()
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
            self._stamp_last_seen()
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

        # Clear the resume handle
        if self._on_resume_handle:
            try:
                await self._on_resume_handle(self.agent_name, "")
            except Exception:
                pass

        await self.disconnect()

        # #591 P1#1 (Murzik round-2): the prior eager refresh here ran
        # the builder 1-arg (commit=True), consuming inbox + restart-
        # manifest BEFORE connect() ran its own reason-aware committed
        # rebuild — same double-consume pattern as the API-layer sites.
        # Removed: connect() is the single source-of-truth for both the
        # wake_context body and side-effect consumption.

        self.codex_session_id = ""
        self.resume_handle = ""

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

        # Ask agent to save state before sleeping. The lock keeps this exec
        # from racing a worker turn against the same codex thread.
        try:
            async with self._exec_lock:
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

        # Set the idle-sleeping flag BEFORE disconnect() so the derived
        # ``state`` property reports IDLE_SLEEPING throughout the teardown
        # window. Otherwise a concurrent heartbeat-watchdog tick between
        # disconnect() landing _connected=False and this line setting
        # _idle_sleeping=True would observe state == DEAD and call
        # _heartbeat_resurrect on a session that's about to be IDLE_SLEEPING.
        # Same class as PR3 Bug 1 on StreamingSession.force_restart, just
        # on the codex backend (per @pushok PR #492 Nit 2). Echoes the
        # "no flicker DEAD ↔ IDLE_SLEEPING/RECONNECTING" invariant from
        # transport_state.py §5.
        self._idle_sleeping = True
        await self.disconnect()
        self._stats["auto_restarts"] += 1
        _log(f"codex[{self.agent_name}]: idle sleep complete")
        return True

    # Reconnect backoff schedule (seconds). Kept in step with StreamingSession's
    # watchdog contract so api._heartbeat_resurrect can treat runtimes uniformly.
    _RECONNECT_BACKOFF = (2, 8, 30)

    async def attempt_reconnect(self) -> None:
        """Attempt to reconnect after a failure with bounded retries."""
        try:
            await self.disconnect()
        except Exception as e:
            _log(f"codex[{self.agent_name}]: pre-reconnect disconnect raised: {e}")

        last_error: Exception | None = None
        for attempt_idx, delay in enumerate(self._RECONNECT_BACKOFF, start=1):
            self._stats["reconnects"] += 1
            _log(
                f"codex[{self.agent_name}]: reconnect attempt {attempt_idx}/"
                f"{len(self._RECONNECT_BACKOFF)} (#{self._stats['reconnects']} total) "
                f"after {delay}s backoff"
            )
            await asyncio.sleep(delay)
            try:
                await self.connect()
                _log(f"codex[{self.agent_name}]: reconnected successfully")
                return
            except Exception as e:
                last_error = e
                _log(f"codex[{self.agent_name}]: reconnect attempt {attempt_idx} failed: {e}")
                try:
                    await self.disconnect()
                except Exception:
                    pass

        self._connected = False
        _log(
            f"codex[{self.agent_name}]: all {len(self._RECONNECT_BACKOFF)} reconnect "
            f"attempts failed (last error: {last_error}); session left disconnected"
        )

    async def disconnect(self) -> None:
        """Disconnect — kill any running subprocess and cancel the worker."""
        self._connected = False
        self._analytics_log_activity("session_end")
        self._analytics_session_ended()

        # Kill any in-flight codex subprocess (legacy exec path)
        if self._current_proc:
            try:
                self._current_proc.kill()
                await self._current_proc.wait()
            except Exception:
                pass
            self._current_proc = None

        # Unblock an in-flight app-server turn, then tear down the connection.
        if self._turn_done is not None and not self._turn_done.done():
            self._turn_done.set_exception(CodexAppServerError("session disconnected"))
        await self._teardown_app_server()

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
    def state(self) -> SessionState:
        """Lifecycle state derived from internal ``_connected``,
        ``_idle_sleeping``, and ``_connect_attempted`` bools.

        CodexSession does not (yet) embed the full ``StateMachine`` that
        StreamingSession adopted in PR3 of #486 — the codex backend's
        lifecycle is simpler (no reconnect retry loop, no resume handle
        capture race). Until/unless it adopts the matrix, the state
        property is a derived view over the legacy bools:

        - ``_idle_sleeping=True``                       → ``IDLE_SLEEPING``
        - ``_connected=True``                           → ``CONNECTED``
        - ``_connect_attempted=False`` (never tried)    → ``UNINITIALIZED``
        - otherwise (tried and currently disconnected)  → ``DEAD``

        RECONNECTING is not modeled; the external readers (broker, api,
        scheduler) only branch on ``CONNECTED`` / ``IDLE_SLEEPING`` /
        ``DEAD`` / ``UNINITIALIZED`` so the coarser mapping is sufficient
        for the Transport protocol's polymorphic contract. Distinguishing
        UNINITIALIZED from DEAD on a never-connected fresh session
        (@pushok PR #492 Nit 1) preserves the "never tried" semantic.
        """
        if self._idle_sleeping:
            return SessionState.IDLE_SLEEPING
        if self._connected:
            return SessionState.CONNECTED
        if not getattr(self, "_connect_attempted", False):
            return SessionState.UNINITIALIZED
        return SessionState.DEAD

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
        return self._context_estimator.estimated_tokens(
            session_id=self.id,
            conversation_store=self._conversation_store,
        )

    @property
    def context_used_pct(self) -> float:
        if self.max_tokens <= 0:
            return 0.0
        # Clamped for the same reason as ContextTextEstimator.context_info
        # (#745): the char-count estimate never learns about Codex CLI's
        # internal auto-compaction, so beyond the window size it's drift.
        return min(100.0, (self.estimated_tokens / self.max_tokens) * 100)

    def get_context_info(self) -> dict:
        """Best-effort context info for APIs that expect session context details."""
        return self._context_estimator.context_info(
            session_id=self.id,
            conversation_store=self._conversation_store,
            max_tokens=self.max_tokens,
        )

    @property
    def stats(self) -> dict:
        return {
            **self._stats,
            "connected": self._connected,
            # Consistency with tmux/streaming stats (#109). Codex only ever
            # reports CONNECTED/IDLE_SLEEPING/UNINITIALIZED/DEAD — it never
            # models BOOTING/RECONNECTING — so it's naturally excluded from
            # the lifecycle transition-age watchdog.
            "state": self.state.value,
            "idle_sleeping": self._idle_sleeping,
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
        self._context_estimator.record_internal_text(text)

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

    @staticmethod
    def _strip_prompt_headers(prompt: str) -> str:
        """Extract raw user text from a broker-formatted prompt."""
        lines = prompt.split("\n")
        body_lines = []
        for line in lines:
            if line.startswith("[") and "|" in line and line.rstrip().endswith("]"):
                continue
            if line.startswith("📎 Attachments:"):
                continue
            if line.startswith("💬 Reply on"):
                continue
            body_lines.append(line)
        return "\n".join(body_lines).strip()

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

    def _stamp_last_seen(self) -> None:
        """Server-side presence: stamp agent last_seen_at (agent-agnostic)."""
        if not self._registry:
            return
        try:
            self._registry.stamp_last_seen(self.agent_name)
        except Exception as e:
            _log(f"codex[{self.agent_name}]: stamp_last_seen failed: {e}")

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
        """Return (tool_name, namespace, metadata) for analytics.

        PII-safety: metadata records argument key names (``arg_keys``) only,
        never raw values. Commands, file paths, and tool inputs can contain
        user data, secrets, or PII and must not leak into analytics.
        """
        item_type = item.get("type", "")
        if item_type == "command_execution":
            # Bash has a single implicit arg: command
            return "Bash", "", {"arg_keys": ["command"]}
        if item_type == "file_edit":
            return "Edit", "", {"arg_keys": ["file_path"]}
        if item_type == "file_read":
            return "Read", "", {"arg_keys": ["file_path"]}
        if item_type == "mcp_tool_call":
            tool_name = item.get("tool_name", "")
            namespace = tool_name.split(".", 1)[0] if "." in tool_name else ""
            tool_input = item.get("input", {})
            arg_keys = sorted(tool_input.keys()) if isinstance(tool_input, dict) else []
            return tool_name, namespace, {"arg_keys": arg_keys}
        if item_type in ("function_call", "tool_call", "tool_use"):
            tool_name = (
                item.get("tool_name", "")
                or item.get("name", "")
                or item.get("function", {}).get("name", "")
            )
            tool_input = item.get("input", {})
            arg_keys = sorted(tool_input.keys()) if isinstance(tool_input, dict) else []
            return tool_name or item_type, "", {"arg_keys": arg_keys}
        return "", "", {}
