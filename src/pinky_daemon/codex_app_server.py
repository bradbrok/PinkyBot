"""Async JSON-RPC client for ``codex app-server``.

Transport layer only. Speaks the newline-delimited JSON dialect that
``codex app-server`` exposes over stdio. Higher layers (CodexSession) build
on this to host long-lived *Threads* instead of spawning ``codex exec`` once
per user message — avoiding per-turn cold-start cost and gaining native
reconnect (Tier 2 of the Codex modernisation, task #98).

Wire protocol (observed against codex-cli 0.125, ``--listen stdio://``):

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
        notification_handler: NotificationHandler | None = None,
        server_request_handler: ServerRequestHandler | None = None,
        log: Callable[[str], None] = _log,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self._notification_handler = notification_handler
        self._server_request_handler = server_request_handler
        self._log = log
        self._next_id = 0
        self._pending: dict[int, asyncio.Future] = {}
        self._read_task: asyncio.Task | None = None
        self._closed = False

    def start(self) -> None:
        """Begin consuming the read stream. Idempotent."""
        if self._read_task is None:
            self._read_task = asyncio.create_task(self._read_loop())

    async def request(
        self,
        method: str,
        params: dict | None = None,
        *,
        timeout: float = DEFAULT_REQUEST_TIMEOUT,
    ) -> object:
        """Send a request and await its result. Raises CodexAppServerError on
        an error frame, transport close, or timeout."""
        if self._closed:
            raise CodexAppServerError("client is closed")
        self._next_id += 1
        rid = self._next_id
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            await self._send({"id": rid, "method": method, "params": params or {}})
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as exc:
            raise CodexAppServerError(f"request {method!r} timed out after {timeout}s") from exc
        finally:
            self._pending.pop(rid, None)

    async def notify(self, method: str, params: dict | None = None) -> None:
        """Send a notification (no id, no response expected)."""
        await self._send({"method": method, "params": params or {}})

    async def initialize(self, *, name: str = "pinkybot", version: str = "1") -> object:
        """Perform the required ``initialize`` handshake."""
        return await self.request("initialize", {"clientInfo": {"name": name, "version": version}})

    async def _send(self, msg: dict) -> None:
        if self._closed:
            raise CodexAppServerError("client is closed")
        self._writer.write((json.dumps(msg) + "\n").encode())
        await self._writer.drain()

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
            self._fail_pending(CodexAppServerError("connection closed"))

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

    async def close(self) -> None:
        """Stop reading and close the write side. Idempotent."""
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            try:
                await self._read_task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
            self._read_task = None
        self._fail_pending(CodexAppServerError("client is closed"))
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
        notification_handler=notification_handler,
        server_request_handler=server_request_handler,
        log=log,
    )
    client.start()
    return client, proc
