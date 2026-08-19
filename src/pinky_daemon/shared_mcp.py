"""Shared MCP server infrastructure.

Provides:
- ContextVar-based agent identification for shared (SSE) MCP servers
- ASGI middleware to extract agent name from X-Agent-Name header
- Factory to create a combined Starlette app mounting multiple MCP servers

In stdio mode (per-agent processes), the agent_name is captured as a closure
variable in create_server(). In shared SSE mode, the middleware sets a
ContextVar that tools read instead.
"""

from __future__ import annotations

import hmac
import ipaddress
import json
import os
import re
import sys
import threading
import time
import uuid
from collections.abc import Callable
from contextvars import ContextVar

# Valid agent name pattern — lowercase alphanumeric, hyphens, underscores
_AGENT_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)

# ── Agent Identity ContextVar ─────────────────────────────────

# Set by ASGI middleware in shared mode, read by tool functions.
# In stdio mode this stays at default ("") and tools use their closure variable.
_current_agent: ContextVar[str] = ContextVar("current_agent", default="")
_current_dream_correlation: ContextVar[str] = ContextVar(
    "current_dream_correlation", default=""
)


def get_current_agent() -> str:
    """Get the current agent name from ContextVar (shared mode)."""
    return _current_agent.get()


def get_current_dream_correlation() -> str:
    """Return the transport-enforced correlation for a dream MCP request."""
    return _current_dream_correlation.get()


def make_agent_name_resolver(closure_agent_name: str):
    """Create a resolver that checks ContextVar first, falls back to closure.

    Usage in create_server():
        resolve_agent = make_agent_name_resolver(agent_name)
        # Then in any tool:
        name = resolve_agent()
    """
    def resolve() -> str:
        ctx_name = _current_agent.get()
        return ctx_name if ctx_name else closure_agent_name
    return resolve


def resolve_lazy(obj):
    """Recursively resolve LazyAgentName instances for json.dumps.

    json.dumps bypasses __str__ on str subclasses, accessing the raw
    underlying str value. Call this on dicts/lists before serializing
    to ensure LazyAgentName resolves to the correct dynamic value.
    """
    if isinstance(obj, dict):
        return {k: resolve_lazy(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [resolve_lazy(v) for v in obj]
    if isinstance(obj, LazyAgentName):
        return str(obj)
    return obj


class LazyAgentName(str):
    """A str subclass that dynamically resolves to the current agent name.

    In stdio mode (per-agent process), resolves to the fallback name passed
    at creation. In shared SSE mode, resolves to the ContextVar value set
    by the middleware.

    Works transparently in f-strings, API calls, and string operations.
    The __str__ override makes format() and f-strings use the dynamic value.

    Hash/eq contract: In shared mode (empty fallback), hashing raises
    TypeError to prevent silent dict/set bugs where eq is dynamic but hash
    is static. In stdio mode (non-empty fallback), hashing uses the fallback
    which is stable and matches eq (ContextVar won't be set).

    Usage in create_server():
        agent_name = LazyAgentName(agent_name)
        # All existing code using agent_name in f-strings, API calls, etc.
        # continues to work, but now resolves dynamically in shared mode.
    """

    _fallback: str

    def __new__(cls, fallback: str = ""):
        instance = super().__new__(cls, fallback)
        instance._fallback = fallback
        return instance

    def _resolve(self) -> str:
        ctx_name = _current_agent.get()
        return ctx_name if ctx_name else self._fallback

    def __str__(self) -> str:
        return self._resolve()

    def __repr__(self) -> str:
        return self._resolve()

    def __eq__(self, other) -> bool:
        return self._resolve() == str(other)

    def __ne__(self, other) -> bool:
        return self._resolve() != str(other)

    def __hash__(self) -> int:
        # In shared mode (empty fallback), dynamic eq makes hashing unsafe.
        # Raise TypeError to prevent silent dict/set corruption.
        if not self._fallback:
            raise TypeError(
                "LazyAgentName with empty fallback (shared mode) is unhashable — "
                "use str(name) to get the resolved value for dict keys/sets"
            )
        # In stdio mode, fallback is stable and matches eq behavior
        return hash(self._fallback)

    def __format__(self, format_spec: str) -> str:
        return format(self._resolve(), format_spec)

    def __mod__(self, args):
        return self._resolve() % args

    def __add__(self, other: str) -> str:
        return self._resolve() + other

    def __radd__(self, other: str) -> str:
        return other + self._resolve()


# ── MCP bind ledger (issue #663) ──────────────────────────────
#
# Tracks, per agent, the last successful round-trip through THIS gateway
# generation — used to detect a resumed CC session whose MCP transport came
# up unbound. Upstream CC bug (anthropics/claude-code #60949, #9608, both
# closed-as-not-planned): CC does not re-initialize an HTTP/SSE MCP transport
# after a 404 "session not found", so a daemon restart that tears down this
# gateway leaves live agents' tools dead until a fresh relaunch.
#
# A tool *executing* is positive proof the agent's real MCP client traversed
# the current gateway, so the ledger is written from inside the tool handler
# (record_probe_success) — never on mere request arrival, since a stale client
# can reach the gateway and still get a 404.
#
# gateway_epoch is reassigned every time the gateway app is (re)built — on a
# daemon restart AND on a gateway crash-restart — so a recorded probe can be
# matched to the gateway generation that actually served it.

_gateway_epoch: str = ""
_probe_ledger: dict[str, dict] = {}
# Probe-*request* ledger (#663 phase-2). Records that the daemon ASKED an agent
# to prove its bind by calling mcp_probe at launch (a fresh launch_id+nonce
# injected into the wake context). Distinct from _probe_ledger, which only
# records SUCCESSES. A current-generation request with no matching success past
# a deadline is a *corroborating* wedge signal — used only to raise confidence /
# enrich audit inside the existing passive-unbound recovery guard, never as an
# independent kill switch (LLM non-compliance, prompt truncation, and
# post-builder delivery failure all mean a missed request alone isn't proof).
# Per-agent: {launch_id, nonce, gateway_epoch, requested_at}.
_probe_request_ledger: dict[str, dict] = {}
_ledger_lock = threading.Lock()


def bump_gateway_epoch() -> str:
    """Assign a fresh gateway epoch (called once per gateway app build)."""
    global _gateway_epoch
    _gateway_epoch = uuid.uuid4().hex[:12]
    # A new generation invalidates every prior bind AND any pending probe
    # request (it targeted a launch in the prior generation) — clear both so a
    # stale pre-restart entry can never be mistaken for current-gen state.
    with _ledger_lock:
        _probe_ledger.clear()
        _probe_request_ledger.clear()
    return _gateway_epoch


def get_gateway_epoch() -> str:
    """Current gateway generation id (empty until the app is first built)."""
    return _gateway_epoch


def record_probe_success(agent_name: str, nonce: str, launch_id: str = "") -> dict:
    """Record a successful mcp_probe round-trip for an agent.

    Called from inside the mcp_probe tool handler — i.e. only after the agent's
    MCP client successfully reached this gateway generation. Returns the
    recorded entry (with the live gateway_epoch and observed_at).
    """
    if not agent_name:
        return {}
    entry = {
        "nonce": nonce,
        "launch_id": launch_id,
        "gateway_epoch": _gateway_epoch,
        "observed_at": time.time(),
    }
    with _ledger_lock:
        _probe_ledger[agent_name] = entry
    return dict(entry)


def record_probe_request(
    agent_name: str, launch_id: str, nonce: str, requested_at: float | None = None
) -> dict:
    """Record that the daemon asked ``agent_name`` to prove its bind (#663 ph2).

    Called when a launch/resume wake context is *delivered* (commit path) with
    an injected ``mcp_probe`` directive — NOT on the eager pre-build, or we would
    log a request that never reaches the agent. Tagged with the live
    gateway_epoch so the watchdog can tell a current-generation request (the
    agent should have answered by now) from a stale one. Overwrites any prior
    request for the agent (one outstanding launch at a time).
    """
    if not agent_name:
        return {}
    entry = {
        "launch_id": launch_id,
        "nonce": nonce,
        "gateway_epoch": _gateway_epoch,
        "requested_at": requested_at if requested_at is not None else time.time(),
    }
    with _ledger_lock:
        _probe_request_ledger[agent_name] = entry
    return dict(entry)


def get_probe_request(agent_name: str) -> dict:
    """Return the outstanding probe-request entry for an agent (or {}).

    ``current`` is True when the request was made for the LIVE gateway
    generation (so a missing matching success is meaningful — a stale request
    from a prior generation tells us nothing). ``fulfilled`` is True when the
    success ledger holds a probe success for the SAME launch_id AND nonce under
    the current epoch, i.e. the agent answered THIS specific request (the nonce
    is part of the proof, not just telemetry). The watchdog treats a current +
    unfulfilled request older than its deadline as a *corroborating* wedge
    signal — and only ever when the passive signal already says bound=false, so
    a generic MCP success that flips bound=true makes the agent healthy
    regardless of whether the specific directive was answered.
    """
    with _ledger_lock:
        req = dict(_probe_request_ledger.get(agent_name, {}))
        success = dict(_probe_ledger.get(agent_name, {}))
    if not req:
        return {}
    current = bool(_gateway_epoch and req.get("gateway_epoch") == _gateway_epoch)
    fulfilled = bool(
        current
        and success.get("gateway_epoch") == _gateway_epoch
        and success.get("launch_id")
        and success.get("launch_id") == req.get("launch_id")
        and success.get("nonce") == req.get("nonce")
    )
    requested = req.get("requested_at")
    return {
        "agent": agent_name,
        "launch_id": req.get("launch_id", ""),
        "nonce": req.get("nonce", ""),
        "gateway_epoch": req.get("gateway_epoch", ""),
        "current_epoch": _gateway_epoch,
        "current": current,
        "fulfilled": fulfilled,
        "requested_at": requested,
        "age_sec": (time.time() - requested) if requested else None,
    }


def record_mcp_success(agent_name: str) -> None:
    """Record a generic successful MCP round-trip (e.g. a heartbeat).

    Lighter than ``record_probe_success`` — no nonce. Any tool *executing*
    proves the agent's MCP client reached this gateway generation, so this is
    the watchdog's passive liveness signal. Within the same gateway epoch it
    only refreshes ``observed_at`` (preserving any prior probe nonce/launch_id);
    across a new epoch it starts a fresh entry.
    """
    if not agent_name:
        return
    with _ledger_lock:
        prev = _probe_ledger.get(agent_name)
        if prev and prev.get("gateway_epoch") == _gateway_epoch:
            prev["observed_at"] = time.time()
        else:
            _probe_ledger[agent_name] = {
                "nonce": "",
                "launch_id": "",
                "gateway_epoch": _gateway_epoch,
                "observed_at": time.time(),
            }


def get_probe_status(agent_name: str) -> dict:
    """Return the last-success ledger entry for an agent plus the current epoch.

    `bound` (a success exists for the CURRENT gateway generation) is left for
    the caller to compute as ``current_epoch and current_epoch == bound_epoch``.
    """
    with _ledger_lock:
        entry = dict(_probe_ledger.get(agent_name, {}))
    observed = entry.get("observed_at")
    current = _gateway_epoch
    bound_epoch = entry.get("gateway_epoch", "")
    return {
        "agent": agent_name,
        "current_epoch": current,
        "bound_epoch": bound_epoch,
        "bound": bool(current and current == bound_epoch),
        "nonce": entry.get("nonce", ""),
        "launch_id": entry.get("launch_id", ""),
        "observed_at": observed,
        "age_sec": (time.time() - observed) if observed else None,
    }


# ── ASGI Middleware ───────────────────────────────────────────

def derive_mcp_bearer(signing_key: str) -> str:
    """Derive the shared-MCP bearer token from an agent's signing key.

    The bearer travels as a static plaintext header on every MCP call (HTTP,
    possibly across the container bridge), so it must NOT be the signing key
    itself — a captured bearer would otherwise also forge HMAC-signed daemon
    API requests. A one-way derivation scopes the exposure: leaking the bearer
    leaks MCP identity only, never the signing credential."""
    if not signing_key:
        return ""
    return hmac.new(
        signing_key.encode("utf-8"), b"pinky-shared-mcp-bearer-v1", "sha256"
    ).hexdigest()


def _require_auth_env() -> bool:
    """PINKY_SHARED_MCP_REQUIRE_AUTH parsed like the other operator toggles:
    unset/empty/"0"/"false"/"no" = off, anything else = on (a bare
    ``bool(value)`` would treat an operator's ``=0`` as ENABLED)."""
    return os.environ.get("PINKY_SHARED_MCP_REQUIRE_AUTH", "").strip().lower() not in (
        "", "0", "false", "no",
    )


def _is_loopback_peer(scope: dict) -> bool:
    """True when the ASGI client address is loopback (or an in-process client
    with no real socket, e.g. starlette's TestClient / a None client). Only a
    genuinely non-loopback IP counts as remote — real remote sockets always
    carry a real peer IP, so unparseable hosts stay in the legacy-trust class
    instead of breaking in-process callers."""
    client = scope.get("client")
    if not client:
        return True
    host = client[0] or ""
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return True


async def _send_401(send, detail: str) -> None:
    body = json.dumps({"detail": detail}).encode()
    await send(
        {
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
        }
    )
    await send({"type": "http.response.body", "body": body})


class AgentNameMiddleware:
    """ASGI middleware that authenticates the caller and exposes the agent
    name to MCP tools via ContextVar.

    Identity model (#623 / #638): historically the shared MCP trusted the
    ``X-Agent-Name`` header alone — acceptable only on a loopback bind. To
    serve container-isolated agents the server must bind beyond loopback
    (PINKY_SHARED_MCP_HOST), where a bare header would let ANY network peer
    act as ANY agent. So:

      - a request carrying ``Authorization: Bearer <token>`` is validated
        against the agent's per-agent signing key (constant-time compare)
        REGARDLESS of peer — a wrong/unknown token is rejected even from
        loopback (no downgrade path);
      - a request from a NON-loopback peer MUST carry a valid bearer;
      - a loopback request without a bearer keeps the legacy header-trust
        (existing local agents / sessions keep working unchanged), unless
        ``PINKY_SHARED_MCP_REQUIRE_AUTH`` is set (the future flip to
        bearer-only once every fleet agent carries a key).

    The bearer token is DERIVED from the per-agent signing key (#623, see
    :func:`derive_mcp_bearer` — the raw key never travels as a header) —
    minted at provision, delivered via the agent's own ``.mcp.json``,
    resolved here at request time via ``signing_key_resolver`` so
    daemon-side rotation takes effect immediately. Resolver errors fail
    CLOSED for bearer validation (reject) — never open.

    ``require_auth=True`` (set by the manager whenever the server is BOUND
    beyond loopback) removes the loopback escape hatch entirely. This is
    the load-bearing defense for containers: with rootless Podman
    (pasta/slirp), container→host traffic can arrive with a 127.0.0.1
    SOURCE address, so per-request peer classification alone cannot be
    trusted on an exposed bind — the bind itself is the decision point.
    """

    def __init__(
        self,
        app,
        signing_key_resolver: "Callable[[str], str | None] | None" = None,
        require_auth: bool = False,
    ):
        self.app = app
        self._signing_key_resolver = signing_key_resolver
        self._require_auth = require_auth

    def _resolve_bearer(self, agent_name: str) -> str:
        if not self._signing_key_resolver:
            return ""
        try:
            key = (self._signing_key_resolver(agent_name) or "").strip()
        except Exception:
            return ""
        return derive_mcp_bearer(key)

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = dict(scope.get("headers", []))
        agent_name = headers.get(b"x-agent-name", b"").decode()
        dream_correlation = headers.get(b"x-dream-correlation", b"").decode()
        valid_name = bool(agent_name and _AGENT_NAME_RE.match(agent_name))
        auth = headers.get(b"authorization", b"").decode()
        bearer = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        auth_required = (
            self._require_auth
            or _require_auth_env()
            or not _is_loopback_peer(scope)
        )

        if bearer:
            expected = self._resolve_bearer(agent_name) if valid_name else ""
            if not expected or not hmac.compare_digest(bearer, expected):
                await _send_401(send, "invalid agent credentials")
                return
            # Bearer-authenticated request: normalize the Host header to
            # loopback before the MCP SDK sees it. The SDK's SSE/HTTP
            # transports carry DNS-rebinding protection that 421s any
            # non-localhost Host — which rejects every container client
            # (Host: host.containers.internal:8890) before tools ever load
            # (live-caught on the Pi #638 rollout). That protection guards
            # browser-context attacks on unauthenticated localhost servers;
            # for a request that just proved possession of the per-agent
            # bearer it is redundant — while the legacy no-bearer loopback
            # path below keeps its Host untouched, so rebinding protection
            # stays fully intact exactly where it still matters.
            server = scope.get("server") or ("127.0.0.1", SHARED_MCP_PORT)
            loop_host = f"127.0.0.1:{server[1]}".encode()
            scope = dict(scope)
            scope["headers"] = [
                (k, v) for (k, v) in scope.get("headers", []) if k != b"host"
            ] + [(b"host", loop_host)]
        elif auth_required:
            await _send_401(send, "agent bearer token required")
            return

        if valid_name:
            agent_token = _current_agent.set(agent_name)
            correlation_token = _current_dream_correlation.set(dream_correlation)
            try:
                await self.app(scope, receive, send)
            finally:
                _current_dream_correlation.reset(correlation_token)
                _current_agent.reset(agent_token)
            return
        await self.app(scope, receive, send)


# ── Combined App Factory ─────────────────────────────────────

def create_shared_app(
    mcp_servers: dict,
    signing_key_resolver: "Callable[[str], str | None] | None" = None,
    require_auth: bool = False,
) -> object:
    """Create a combined Starlette app mounting multiple MCP servers.

    Args:
        mcp_servers: Dict of mount_name -> FastMCP instance.
                     e.g. {"memory": memory_mcp, "self": self_mcp, "messaging": msg_mcp}
        signing_key_resolver: agent_name -> per-agent signing key, used by
                     AgentNameMiddleware to validate inbound bearer tokens
                     (required for any non-loopback bind — see the middleware).
        require_auth: force bearer-only auth for every request (set whenever
                     the server is bound beyond loopback).

    Returns:
        ASGI app ready for uvicorn.
    """
    from contextlib import asynccontextmanager

    from starlette.applications import Starlette
    from starlette.routing import Mount

    # New gateway generation: bump the epoch (and clear the bind ledger) so a
    # resumed agent's pre-restart MCP binding is never counted as current. (#663)
    epoch = bump_gateway_epoch()
    _log(f"[shared-mcp] Gateway epoch: {epoch}")

    routes = []
    session_managers = []  # Track streamable HTTP session managers for lifespan

    for name, mcp_instance in mcp_servers.items():
        # Streamable HTTP FIRST — longer prefix must come before shorter
        # to prevent SSE mount from prefix-matching HTTP requests
        try:
            http_app = mcp_instance.streamable_http_app()
            routes.append(Mount(f"/mcp/{name}/http", app=http_app))
            # Track session manager so we can start it in the combined lifespan
            if mcp_instance._session_manager is not None:
                session_managers.append((name, mcp_instance._session_manager))
        except Exception as e:
            _log(f"[shared-mcp] Could not mount streamable HTTP for {name}: {e}")

        # SSE transport second (Claude Code SDK)
        sse_app = mcp_instance.sse_app(mount_path="/")
        routes.append(Mount(f"/mcp/{name}", app=sse_app))

    # Combined lifespan that starts all streamable HTTP session managers.
    # Each manager's run() is an async context manager that must stay open
    # for the lifetime of the server. We nest them using contextlib.AsyncExitStack.
    @asynccontextmanager
    async def _combined_lifespan(app):
        from contextlib import AsyncExitStack

        async with AsyncExitStack() as stack:
            for srv_name, mgr in session_managers:
                _log(f"[shared-mcp] Starting streamable HTTP session manager for {srv_name}")
                await stack.enter_async_context(mgr.run())
            _log(f"[shared-mcp] All {len(session_managers)} HTTP session managers started")
            yield

    inner_app = Starlette(routes=routes, lifespan=_combined_lifespan)
    app = AgentNameMiddleware(
        inner_app,
        signing_key_resolver=signing_key_resolver,
        require_auth=require_auth,
    )

    transports = ", ".join(f"/mcp/{n} (sse+http)" for n in mcp_servers)
    _log(f"[shared-mcp] Mounting {len(mcp_servers)} servers: {transports}")

    return app


# ── Shared Server Manager ─────────────────────────────────────

SHARED_MCP_PORT = 8890
# Bind address for the shared MCP server. Default loopback (host-only). Set
# PINKY_SHARED_MCP_HOST (e.g. "0.0.0.0") to expose it to container-isolated
# agents that reach it via host.containers.internal. NOTE: the shared MCP
# authenticates callers by the X-Agent-Name header alone, so binding beyond
# loopback widens the trust surface — only do so on a trusted network / behind
# a firewall until that path carries a signature.
SHARED_MCP_HOST = os.environ.get("PINKY_SHARED_MCP_HOST", "127.0.0.1")


class MemoryStorePool:
    """Thread-safe pool of per-agent ReflectionStore instances.

    Lazily creates and caches stores when an agent's memory is first accessed.
    Uses a callback to resolve agent_name -> db_path, so it doesn't need
    direct access to the agent registry.
    """

    def __init__(self, db_path_resolver: "Callable[[str], str]"):
        """
        Args:
            db_path_resolver: Callable that maps agent_name -> SQLite DB path.
                              Should raise ValueError for unknown agents.
        """
        self._db_path_resolver = db_path_resolver
        self._stores: dict[str, object] = {}  # agent_name -> ReflectionStore
        self._lock = threading.Lock()

    def get_store(self, agent_name: str) -> object:
        """Get or create the ReflectionStore for an agent (thread-safe)."""
        # Fast path: already cached
        store = self._stores.get(agent_name)
        if store is not None:
            return store

        # Slow path: create under lock
        with self._lock:
            # Double-check after acquiring lock
            store = self._stores.get(agent_name)
            if store is not None:
                return store

            from pinky_memory.store import ReflectionStore

            db_path = self._db_path_resolver(agent_name)
            _log(f"[shared-mcp] Opening memory store for agent '{agent_name}': {db_path}")
            store = ReflectionStore(db_path=db_path)
            self._stores[agent_name] = store
            return store

    def remove_store(self, agent_name: str) -> None:
        """Remove and close a cached store (e.g. when agent is deleted)."""
        with self._lock:
            store = self._stores.pop(agent_name, None)
            if store is not None:
                try:
                    store.close()  # type: ignore[union-attr]
                except Exception:
                    pass

    def close_all(self) -> None:
        """Close all cached stores (shutdown)."""
        with self._lock:
            for name, store in self._stores.items():
                try:
                    store.close()  # type: ignore[union-attr]
                except Exception:
                    pass
            self._stores.clear()


class SharedMcpManager:
    """Manages the shared MCP server lifecycle within the daemon process.

    Runs uvicorn in a separate daemon thread (not asyncio task) because
    the MCP tool functions use synchronous urllib.request.urlopen to call
    the daemon API. Running in the daemon's event loop would block it.
    The separate thread has its own event loop for uvicorn's async internals.
    """

    def __init__(
        self,
        *,
        host: str = SHARED_MCP_HOST,
        port: int = SHARED_MCP_PORT,
        api_url: str = "http://localhost:8888",
        memory_db_resolver: "Callable[[str], str] | None" = None,
        cross_agent_authorizer: "Callable[[str], bool] | None" = None,
        signing_key_resolver: "Callable[[str], str | None] | None" = None,
        openai_api_key: str = "",
    ):
        self._host = host
        self._port = port
        self._api_url = api_url
        self._memory_db_resolver = memory_db_resolver
        self._cross_agent_authorizer = cross_agent_authorizer
        # OPENAI_API_KEY for the shared pinky-memory embedder. The daemon
        # process env does NOT carry it (the key lives in system_settings), so
        # the daemon resolves it and passes it in here. Empty -> NoOp embedder
        # (keyword-only recall). See semantic-recall regression 2026-06-05.
        self._openai_api_key = openai_api_key
        # #623: agent_name -> per-agent signing key, so the shared self/messaging
        # servers sign each request with the resolved agent's key (the shared
        # process has no PINKY_AGENT_KEY env). None falls back to global secret.
        self._signing_key_resolver = signing_key_resolver
        self._memory_pool: MemoryStorePool | None = None
        self._server = None  # uvicorn.Server instance
        self._thread: threading.Thread | None = None
        self._running = False

    async def start(self) -> None:
        """Start the shared MCP server in a separate thread."""
        if self._running:
            _log("[shared-mcp] Already running")
            return

        self._running = True
        self._thread = threading.Thread(
            target=self._run_in_thread,
            name="shared-mcp-server",
            daemon=True,
        )
        self._thread.start()
        _log(f"[shared-mcp] Started on {self._host}:{self._port}")

    def _create_app(self):
        """Create MCP server instances and the combined ASGI app."""
        from pinky_messaging.server import create_server as create_messaging_server
        from pinky_self.server import create_server as create_self_server

        # Shared pinky-self: agent_name="" (resolved via ContextVar), ALL gates
        all_gates = [
            "extras", "kb", "research", "presentations", "triggers",
            "schedule", "skill-admin", "admin", "tasks-admin", "voice", "apps",
        ]
        self_mcp = create_self_server(
            agent_name="",
            api_url=self._api_url,
            tool_gates=all_gates,
            signing_key_resolver=self._signing_key_resolver,
        )

        # Shared pinky-messaging: agent_name="" (resolved via ContextVar)
        messaging_mcp = create_messaging_server(
            agent_name="",
            api_url=self._api_url,
            signing_key_resolver=self._signing_key_resolver,
        )

        mcp_servers = {
            "self": self_mcp,
            "messaging": messaging_mcp,
        }

        # Shared pinky-memory: per-agent store pool with lazy creation
        if self._memory_db_resolver:
            from pinky_memory.embeddings import build_embedding_client
            from pinky_memory.server import create_server as create_memory_server

            self._memory_pool = MemoryStorePool(self._memory_db_resolver)
            embedder = build_embedding_client(api_key=self._openai_api_key)

            memory_mcp = create_memory_server(
                store_factory=self._memory_pool.get_store,
                embedder=embedder,
                cross_agent_authorizer=self._cross_agent_authorizer,
            )
            mcp_servers["memory"] = memory_mcp
            _log("[shared-mcp] pinky-memory included (per-agent store pool)")

        # A non-loopback BIND forces bearer-only auth for all requests: with
        # rootless Podman the container->host source address can appear as
        # 127.0.0.1, so peer classification alone cannot gate an exposed bind.
        bind_exposed = self._host not in ("127.0.0.1", "::1", "localhost")
        if bind_exposed:
            _log(
                f"[shared-mcp] non-loopback bind {self._host!r} — bearer auth "
                f"REQUIRED for every request"
            )
        return create_shared_app(
            mcp_servers,
            signing_key_resolver=self._signing_key_resolver,
            require_auth=bind_exposed,
        )

    def _run_in_thread(self) -> None:
        """Run the shared MCP server in a dedicated thread with supervisor loop."""
        import uvicorn

        from pinky_daemon.routes.triggers import uvicorn_log_config

        backoff = 1
        max_backoff = 60

        while self._running:
            app = self._create_app()
            # log_config: this Config load (and every supervisor restart)
            # re-runs dictConfig process-wide; without the patched config it
            # would strip the /hooks token redaction off the access handler.
            config = uvicorn.Config(
                app,
                host=self._host,
                port=self._port,
                log_level="warning",
                log_config=uvicorn_log_config(),
            )
            self._server = uvicorn.Server(config)
            try:
                self._server.run()
            except Exception as e:
                if not self._running:
                    break  # Normal shutdown
                _log(f"[shared-mcp] Server crashed: {e} — restarting in {backoff}s")
                time.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            # Normal exit (stop() called)
            break

        self._running = False
        _log("[shared-mcp] Server thread exited")

    async def stop(self) -> None:
        """Gracefully stop the shared MCP server."""
        self._running = False
        if self._server:
            self._server.should_exit = True
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=5)
        if self._memory_pool:
            self._memory_pool.close_all()
            self._memory_pool = None
        _log("[shared-mcp] Stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"
