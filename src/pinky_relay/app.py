"""Token-scoped HTTP queue with bounded requests, leases and long polling."""

from __future__ import annotations

import asyncio
import base64
import binascii
import logging
import time
from collections import Counter, deque
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from pinky_relay.config import RelayConfig, parse_json
from pinky_relay.store import MAX_BLOB, GrowthStopError, QueueFullError, QueueStore

LOGGER = logging.getLogger(__name__)
HEADER_CAP = 16 * 1024
SEND_CAP = 400 * 1024
ACK_CAP = 16 * 1024


class WindowLimit:
    def __init__(self, rate, *, clock=time.monotonic, max_keys=4096):
        self.rate, self.clock, self.max_keys = rate, clock, max_keys
        self.rows = {}

    def take(self, key):
        now = self.clock()
        for old in tuple(self.rows):
            row = self.rows[old]
            while row and row[0] <= now - 60:
                row.popleft()
            if not row:
                del self.rows[old]
        if key not in self.rows:
            if len(self.rows) >= self.max_keys:
                return False
            self.rows[key] = deque()
        row = self.rows[key]
        if len(row) >= self.rate:
            return False
        row.append(now)
        return True


class RelayState:
    def __init__(self, config, store, *, clock=time.monotonic, wall=time.time):
        self.config, self.store, self.clock, self.wall = config, store, clock, wall
        self.requests = 0
        self.per_token = Counter()
        self.polls = 0
        self.token_polls = Counter()
        self.auth_limit = WindowLimit(60, clock=clock, max_keys=len(config.digests))
        self.failure_limit = WindowLimit(30, clock=clock)
        self.reasons = Counter()
        self.logged = {}
        self.writer = asyncio.Lock()
        self.changed = asyncio.Event()

    def refusal(self, reason, status, *, retry=False):
        self.reasons[reason] += 1
        now = self.clock()
        if reason not in self.logged or now - self.logged[reason] >= 60:
            LOGGER.warning("relay refusal reason=%s count=%d", reason, self.reasons[reason])
            self.logged[reason] = now
        return JSONResponse({"ok": False, "error": reason}, status_code=status,
                            headers={"Retry-After": "1"} if retry else None)


class Admission:
    def __init__(self, app, state):
        self.app, self.state = app, state

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        state = self.state
        if state.requests >= 64:
            return await state.refusal("global request limit", 429, retry=True)(scope, receive, send)
        state.requests += 1
        fleet = None
        try:
            headers = Headers(scope=scope)
            path = scope["path"]
            root = scope.get("root_path", "")
            if root and path.startswith(root + "/"):
                path = path[len(root):]
            if path != "/health":
                authorization = headers.getlist("authorization")
                token = ""
                if len(authorization) == 1:
                    scheme, _, value = authorization[0].partition(" ")
                    if scheme.lower() == "bearer":
                        token = value
                fleet = state.config.authenticate(token)
                if fleet is None:
                    address = (scope.get("client") or ("unknown", 0))[0]
                    if address == "127.0.0.1":
                        address = headers.get("x-real-ip", address)
                    allowed = state.failure_limit.take(address)
                    response = state.refusal("unauthorized" if allowed else "authentication rate limit",
                                             401 if allowed else 429, retry=not allowed)
                    return await response(scope, receive, send)
                if state.per_token[fleet] >= 8:
                    return await state.refusal("token request limit", 429, retry=True)(scope, receive, send)
                if not state.auth_limit.take(fleet):
                    return await state.refusal("token rate limit", 429, retry=True)(scope, receive, send)
                state.per_token[fleet] += 1
                scope.setdefault("state", {})["relay_fleet"] = fleet
            if sum(len(k) + len(v) + 4 for k, v in scope["headers"]) > HEADER_CAP:
                return await state.refusal("request headers too large", 431)(scope, receive, send)
            await self.app(scope, receive, send)
        finally:
            state.requests -= 1
            if fleet is not None and scope.get("state", {}).get("relay_fleet") == fleet:
                state.per_token[fleet] -= 1


async def _body(request, cap):
    state = request.app.state.relay
    if request.headers.get("transfer-encoding"):
        return None, state.refusal("chunked body refused", 400)
    lengths = request.headers.getlist("content-length")
    if not lengths:
        return None, state.refusal("content length required", 411)
    if len(lengths) != 1 or not lengths[0].isascii() or not lengths[0].isdigit():
        return None, state.refusal("invalid content length", 400)
    if len(lengths[0]) > 12:
        return None, state.refusal("request body too large", 413)
    length = int(lengths[0])
    if length > cap:
        return None, state.refusal("request body too large", 413)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > cap:
            return None, state.refusal("request body too large", 413)
        raw.extend(chunk)
    if len(raw) != length:
        return None, state.refusal("body length mismatch", 400)
    try:
        value = parse_json(bytes(raw))
    except ValueError:
        return None, state.refusal("invalid request JSON", 400)
    if not isinstance(value, dict):
        return None, state.refusal("request object required", 400)
    return value, None


def create_relay_app(config: RelayConfig, store: QueueStore, *, clock=time.monotonic,
                     wall=time.time, root_path="/ferry-relay") -> FastAPI:
    state = RelayState(config, store, clock=clock, wall=wall)

    async def sweep():
        while True:
            await asyncio.sleep(60)
            while True:
                try:
                    async with state.writer:
                        count = store.sweep(now=int(wall() * 1000))
                except GrowthStopError:
                    state.refusal("growth stopped", 503)
                    async with state.writer:
                        store.barrier()
                    break
                if count < store.batch:
                    break
                await asyncio.sleep(0)

    @asynccontextmanager
    async def lifespan(app):
        sweeper = asyncio.create_task(sweep())
        try:
            yield
        finally:
            sweeper.cancel()
            await asyncio.gather(sweeper, return_exceptions=True)
            store.close()

    app = FastAPI(root_path=root_path, lifespan=lifespan, docs_url=None, redoc_url=None,
                  openapi_url=None)
    app.state.relay = state
    app.add_middleware(Admission, state=state)

    @app.get("/health")
    async def health():
        return {"ok": True}

    @app.post("/v1/send")
    async def enqueue(request: Request):
        body, failure = await _body(request, SEND_CAP)
        if failure is not None:
            return failure
        target, encoded = body.get("to_fleet"), body.get("blob_b64")
        if not isinstance(target, str) or target not in config.digests:
            return state.refusal("unknown destination", 404)
        if not isinstance(encoded, str):
            return state.refusal("invalid relay blob", 400)
        try:
            blob = base64.b64decode(encoded, validate=True)
        except (ValueError, binascii.Error):
            return state.refusal("invalid relay blob", 400)
        if len(blob) > MAX_BLOB:
            return state.refusal("decoded blob too large", 413)
        try:
            async with state.writer:
                ident = store.send(request.state.relay_fleet, target, blob, now=int(wall() * 1000))
        except GrowthStopError:
            return state.refusal("growth stopped", 507)
        except QueueFullError:
            return state.refusal("queue quota exceeded", 429, retry=True)
        state.changed.set()
        return {"id": ident}

    @app.get("/v1/poll")
    async def poll(request: Request):
        fleet = request.state.relay_fleet
        try:
            wait = float(request.query_params.get("wait", "0"))
            if not 0 <= wait <= 25:
                raise ValueError
        except ValueError:
            return state.refusal("invalid poll wait", 400)
        if state.polls >= 32 or state.token_polls[fleet] >= 2:
            return state.refusal("long poll limit", 429, retry=True)
        state.polls += 1
        state.token_polls[fleet] += 1
        deadline = clock() + wait
        try:
            while True:
                state.changed.clear()
                try:
                    async with state.writer:
                        rows = store.poll(fleet, now=int(wall() * 1000))
                except GrowthStopError:
                    return state.refusal("growth stopped", 503, retry=True)
                remaining = deadline - clock()
                if rows or remaining <= 0:
                    return {"rows": rows}
                try:
                    await asyncio.wait_for(state.changed.wait(), timeout=remaining)
                except TimeoutError:
                    return {"rows": []}
        finally:
            state.polls -= 1
            state.token_polls[fleet] -= 1

    @app.post("/v1/ack")
    async def acknowledge(request: Request):
        body, failure = await _body(request, ACK_CAP)
        if failure is not None:
            return failure
        ids = body.get("ids")
        if (not isinstance(ids, list) or len(ids) > 100
                or any(not isinstance(i, str) or not i or not i.isascii() or len(i) > 64 for i in ids)):
            return state.refusal("invalid acknowledgement ids", 400)
        try:
            async with state.writer:
                count = store.ack(request.state.relay_fleet, ids)
        except GrowthStopError:
            return state.refusal("growth stopped", 503, retry=True)
        return {"deleted": count}

    return app
