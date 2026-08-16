"""Async JSON-RPC client for ``codex app-server``.

Transport layer only. Speaks the newline-delimited JSON dialect that
``codex app-server`` exposes over stdio. Higher layers (CodexSession) build
on this to host long-lived *Threads* instead of spawning ``codex exec`` once
per user message — avoiding per-turn cold-start cost and gaining native
reconnect (Tier 2 of the Codex modernisation, task #98).

Wire protocol (re-verified against codex-cli 0.144, ``--listen stdio://``):

  * One JSON object per line (NDJSON). No ``Content-Length`` framing and no
    ``"jsonrpc"`` version field — the server neither emits nor requires it.
  * Client request:   ``{"id": <int>, "method": "<m>", "params": {...}}``
  * Server response:  ``{"id": <int>, "result": {...}}``
                  or  ``{"id": <int>, "error": {"code", "message", ...}}``
  * Server notification (no id): ``{"method": "<m>", "params": {...}}``
  * Server->client request (has id + method, expects a response):
    e.g. approval prompts (``item/commandExecution/requestApproval``). Routed
    to ``server_request_handler``; unhandled requests get a JSON-RPC error
    response so the server is never left waiting.

Methods use slash notation (``initialize``, ``thread/start``, ``turn/start``,
``item/completed`` ...) — distinct from the dot notation of the legacy
``codex exec --json`` stream. Mapping app-server notifications back onto the
existing event shapes is the integration layer's job, not this module's.

This module is pure transport: it does not know about agents, turns, or
PinkyBot session state. That keeps it unit-testable against an in-memory
stream pair with no real subprocess.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable

from pinky_daemon.streaming_session import _log

DEFAULT_COMMAND = ("codex", "app-server")
# Generous default: a single turn can run for minutes under heavy tool use.
DEFAULT_REQUEST_TIMEOUT = 600.0
# Match codex_session's subprocess read limit — large tool-result frames can
# exceed asyncio's default 64KiB line buffer and raise LimitOverrunError.
_STREAM_LIMIT = 10 * 1024 * 1024

# async fn(method: str, params: dict) -> None
NotificationHandler = Callable[[str, dict], Awaitable[None]]
# async fn(method: str, params: dict) -> dict (the result payload)
ServerRequestHandler = Callable[[str, dict], Awaitable[dict | None]]
class CodexAppServerError(Exception):
    """A JSON-RPC error frame, or a transport failure (EOF / closed)."""

    def __init__(self, message: str, *, code: int | None = None, data: object = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data

    @classmethod
    def from_frame(cls, err: object) -> CodexAppServerError:
        if isinstance(err, dict):
            return cls(
                str(err.get("message", "unknown error")),
                code=err.get("code"),
                data=err.get("data"),
            )
        return cls(str(err))


# Synchronous by design: EOF must be able to wake a higher-layer turn future
# without making the transport read loop await teardown (or itself).
TransportClosedHandler = Callable[[CodexAppServerError], None]


class CodexAppServerClient:
    """JSON-RPC client over a duplex byte stream.

    Constructed from an ``asyncio.StreamReader``/``StreamWriter`` pair so it can
    be driven by a real subprocess (see :func:`spawn_app_server`) or by an
    in-memory stream in tests. Call :meth:`start` to begin reading, then
    :meth:`request` / :meth:`notify`. Always :meth:`close` when done.
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        stderr: asyncio.StreamReader | None = None,
        notification_handler: NotificationHandler | None = None,
        server_request_handler: ServerRequestHandler | None = None,
        log: Callable[[str], None] = _log,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._stderr = stderr
        self._notification_handler = notification_handler
        self._server_request_handler = server_request_handler
        self._transport_closed_handler: TransportClosedHandler | None = None
        self._log = log
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: asyncio.Task | None = None
        self._stderr_task: asyncio.Task | None = None
        self._closed = False
        self._close_reason: str | None = None
        self._closed_event = asyncio.Event()

    @property
    def is_closed(self) -> bool:
        """Whether either side deliberately closed or the read side died."""
        return self._closed

    def _raise_if_closed(self) -> None:
        if self._closed:
            raise CodexAppServerError(self._close_reason or "client is closed")

    def start(self) -> None:
        """Begin consuming the read stream. Idempotent."""
        self._raise_if_closed()
        if self._read_task is None:
            self._read_task = asyncio.create_task(self._read_loop())
        # Drain stderr continuously: an undrained PIPE on a long-lived process
        # blocks the child once the OS buffer (~64KiB) fills, wedging every turn.
        if self._stderr is not None and self._stderr_task is None:
            self._stderr_task = asyncio.create_task(self._drain_stderr())

    def set_transport_closed_handler(
        self, handler: TransportClosedHandler | None
    ) -> None:
        """Register a synchronous callback for unexpected read-side closure.

        Deliberate :meth:`close` does not invoke it. Higher layers use this
        edge to fail work that was accepted after its request future resolved;
        at that point ``_pending`` alone no longer represents active work.
        """
        self._transport_closed_handler = handler

    async def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> object:
        """Send a request and await its result. Raises CodexAppServerError on
        an error frame, transport close, or timeout."""
        self._raise_if_closed()
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        send_completed = False
        try:
            # The public timeout is an end-to-end request deadline. In
            # particular, a backpressured writer.drain() cannot consume an
            # unbounded pre-response phase before the Future wait starts.
            async with asyncio.timeout(timeout):
                await self._send({"id": rid, "method": method, "params": params or {}})
                send_completed = True
                return await fut
        except asyncio.TimeoutError as exc:
            if self._closed:
                raise CodexAppServerError(
                    self._close_reason or "connection closed"
                ) from exc
            if not send_completed:
                # A timed-out drain leaves delivery ambiguous at the byte
                # boundary. Retire the transport so buffered frame remnants
                # cannot wedge or corrupt a later request on this connection.
                self._latch_transport_closed(CodexAppServerError("connection closed"))
                try:
                    self._writer.close()
                except Exception:  # noqa: BLE001 — latch is already terminal
                    pass
            raise CodexAppServerError(f"request {method!r} timed out after {timeout}s") from exc
        finally:
            self._pending.pop(rid, None)
            # EOF can fail the response Future while request() is still inside
            # _send. Consume that exception even though it was never awaited,
            # avoiding a false "Future exception was never retrieved" report.
            if fut.done() and not fut.cancelled():
                fut.exception()

    async def notify(self, method: str, params: dict | None = None) -> None:
        """Send a notification (no id, no response expected)."""
        await self._send({"method": method, "params": params or {}})

    async def initialize(self, *, name: str = "pinkybot", version: str = "1") -> object:
        """Perform the required ``initialize`` handshake."""
        return await self.request(
            "initialize",
            {
                "clientInfo": {"name": name, "version": version},
                # 0.144 added capability negotiation. An empty declaration is
                # the minimal stable set: do not opt into experimentalApi,
                # attestation requests, or extended elicitation semantics.
                "capabilities": {},
            },
        )

    async def _send(self, msg: dict) -> None:
        self._raise_if_closed()
        payload = (json.dumps(msg) + "\n").encode()
        drain_task: asyncio.Task | None = None
        closed_task: asyncio.Task | None = None
        try:
            self._writer.write(payload)
            # Cover EOF landing after the first latch check but at the write
            # edge. If the read loop has already run, never enter drain.
            self._raise_if_closed()
            drain_task = asyncio.create_task(self._writer.drain())
            closed_task = asyncio.create_task(self._closed_event.wait())
            await asyncio.wait(
                {drain_task, closed_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            # The close edge wins ties: drain may surface BrokenPipeError at
            # the same moment stdout EOF latches the connection terminal.
            if self._closed_event.is_set():
                if not drain_task.done():
                    drain_task.cancel()
                await asyncio.gather(drain_task, return_exceptions=True)
                self._raise_if_closed()
            await drain_task
            self._raise_if_closed()
        except asyncio.CancelledError:
            raise
        except CodexAppServerError:
            raise
        except Exception as exc:  # noqa: BLE001 — normalize write-side death
            close_error = CodexAppServerError("connection closed")
            self._latch_transport_closed(close_error)
            raise close_error from exc
        finally:
            cleanup = []
            for task in (drain_task, closed_task):
                if task is not None and not task.done():
                    task.cancel()
                    cleanup.append(task)
            if cleanup:
                await asyncio.gather(*cleanup, return_exceptions=True)

    async def _read_loop(self) -> None:
        try:
            while True:
                line = await self._reader.readline()
                if not line:
                    break  # EOF — server closed stdout
                stripped = line.strip()
                if not stripped:
                    continue
                try:
                    msg = json.loads(stripped)
                except json.JSONDecodeError:
                    self._log(f"codex-app-server: skipping non-JSON line: {stripped[:200]!r}")
                    continue
                await self._dispatch(msg)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — read loop must not die silently
            self._log(f"codex-app-server: read loop error: {exc}")
        finally:
            close_error = CodexAppServerError("connection closed")
            self._latch_transport_closed(close_error)

    async def _dispatch(self, msg: dict) -> None:
        mid = msg.get("id")
        method = msg.get("method")
        if method is not None and mid is not None:
            await self._handle_server_request(mid, method, msg.get("params") or {})
        elif method is not None:
            if self._notification_handler is not None:
                await self._notification_handler(method, msg.get("params") or {})
        elif mid is not None:
            fut = self._pending.get(mid)
            if fut is not None and not fut.done():
                if "error" in msg:
                    fut.set_exception(CodexAppServerError.from_frame(msg["error"]))
                else:
                    fut.set_result(msg.get("result"))
        else:
            self._log(f"codex-app-server: unrecognized message: {msg}")

    async def _drain_stderr(self) -> None:
        """Consume the subprocess stderr stream so it can never block the child.

        Logs non-empty lines (truncated) for diagnostics; ends on EOF when the
        process exits or is killed.
        """
        assert self._stderr is not None
        try:
            while True:
                line = await self._stderr.readline()
                if not line:
                    break  # EOF — process closed stderr
                text = line.decode(errors="replace").rstrip()
                if text:
                    self._log(f"codex-app-server[stderr]: {text[:500]}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — drain must not crash the client
            self._log(f"codex-app-server: stderr drain error: {exc}")

    async def _handle_server_request(self, mid: object, method: str, params: dict) -> None:
        if self._server_request_handler is None:
            await self._send(
                {"id": mid, "error": {"code": -32601, "message": f"no handler for {method!r}"}}
            )
            return
        try:
            result = await self._server_request_handler(method, params)
            await self._send({"id": mid, "result": result if result is not None else {}})
        except Exception as exc:  # noqa: BLE001 — surface handler failure as an error frame
            await self._send({"id": mid, "error": {"code": -32000, "message": str(exc)}})

    def _fail_pending(self, exc: CodexAppServerError) -> None:
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(exc)
        self._pending.clear()

    def _latch_transport_closed(self, exc: CodexAppServerError) -> None:
        """Publish one terminal transport edge to requests and the session."""
        unexpected_close = not self._closed
        if unexpected_close:
            self._closed = True
            self._close_reason = str(exc)
        self._closed_event.set()
        self._fail_pending(exc)
        if unexpected_close and self._transport_closed_handler is not None:
            try:
                self._transport_closed_handler(exc)
            except Exception as handler_exc:  # noqa: BLE001 — closure stays terminal
                self._log(
                    "codex-app-server: transport-close handler error: "
                    f"{handler_exc}"
                )

    async def close(self) -> None:
        """Stop reading and close the write side. Idempotent."""
        self._closed = True
        if self._close_reason is None:
            self._close_reason = "client is closed"
        self._closed_event.set()
        self._fail_pending(CodexAppServerError("client is closed"))
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._read_task = None
        if self._stderr_task is not None:
            self._stderr_task.cancel()
            try:
                await self._stderr_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._stderr_task = None
        try:
            self._writer.close()
        except Exception:  # noqa: BLE001 — best-effort close
            pass


async def spawn_app_server(
    *,
    command: tuple[str, ...] | list[str] | None = None,
    cwd: str | None = None,
    env: dict | None = None,
    notification_handler: NotificationHandler | None = None,
    server_request_handler: ServerRequestHandler | None = None,
    log: Callable[[str], None] = _log,
) -> tuple[CodexAppServerClient, asyncio.subprocess.Process]:
    """Launch ``codex app-server`` and return a started client plus its process.

    The caller owns the process lifecycle: call ``client.close()`` then
    terminate/kill ``proc`` on shutdown.
    """
    cmd = list(command or DEFAULT_COMMAND)
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        cwd=cwd,
        env=env,
        limit=_STREAM_LIMIT,
    )
    assert proc.stdout is not None and proc.stdin is not None
    client = CodexAppServerClient(
        proc.stdout,
        proc.stdin,
        stderr=proc.stderr,
        notification_handler=notification_handler,
        server_request_handler=server_request_handler,
        log=log,
    )
    client.start()
    return client, proc
