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

import asyncio
import re
import sys
from contextvars import ContextVar

# Valid agent name pattern — lowercase alphanumeric, hyphens, underscores
_AGENT_NAME_RE = re.compile(r"^[a-z0-9_-]+$")


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)

# ── Agent Identity ContextVar ─────────────────────────────────

# Set by ASGI middleware in shared mode, read by tool functions.
# In stdio mode this stays at default ("") and tools use their closure variable.
_current_agent: ContextVar[str] = ContextVar("current_agent", default="")


def get_current_agent() -> str:
    """Get the current agent name from ContextVar (shared mode)."""
    return _current_agent.get()


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


# ── ASGI Middleware ───────────────────────────────────────────

class AgentNameMiddleware:
    """ASGI middleware that extracts X-Agent-Name header into ContextVar.

    Wrap the combined Starlette app with this so all MCP tool calls
    within a request can read the agent name via get_current_agent()
    or make_agent_name_resolver().
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] == "http":
            headers = dict(scope.get("headers", []))
            agent_name = headers.get(b"x-agent-name", b"").decode()
            if agent_name and _AGENT_NAME_RE.match(agent_name):
                token = _current_agent.set(agent_name)
                try:
                    await self.app(scope, receive, send)
                finally:
                    _current_agent.reset(token)
                return
        await self.app(scope, receive, send)


# ── Combined App Factory ─────────────────────────────────────

def create_shared_app(
    mcp_servers: dict,
) -> object:
    """Create a combined Starlette app mounting multiple MCP servers.

    Args:
        mcp_servers: Dict of mount_name -> FastMCP instance.
                     e.g. {"memory": memory_mcp, "self": self_mcp, "messaging": msg_mcp}

    Returns:
        ASGI app ready for uvicorn.
    """
    from starlette.applications import Starlette
    from starlette.routing import Mount

    routes = []
    for name, mcp_instance in mcp_servers.items():
        sse_app = mcp_instance.sse_app(mount_path="/")
        routes.append(Mount(f"/mcp/{name}", app=sse_app))

    inner_app = Starlette(routes=routes)
    app = AgentNameMiddleware(inner_app)

    _log(f"[shared-mcp] Mounting {len(mcp_servers)} servers: "
         f"{', '.join(f'/mcp/{n}' for n in mcp_servers)}")

    return app


# ── Shared Server Manager ─────────────────────────────────────

SHARED_MCP_PORT = 8890
SHARED_MCP_HOST = "127.0.0.1"


class SharedMcpManager:
    """Manages the shared MCP server lifecycle within the daemon process.

    Starts a uvicorn server as an asyncio task, with health checks
    and auto-restart on failure.
    """

    def __init__(
        self,
        *,
        host: str = SHARED_MCP_HOST,
        port: int = SHARED_MCP_PORT,
        api_url: str = "http://localhost:8888",
    ):
        self._host = host
        self._port = port
        self._api_url = api_url
        self._server = None  # uvicorn.Server instance
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        """Start the shared MCP server with auto-restart on crash."""
        if self._running:
            _log("[shared-mcp] Already running")
            return

        self._running = True
        self._task = asyncio.create_task(self._supervisor_loop())
        _log(f"[shared-mcp] Started on {self._host}:{self._port}")

    def _create_app(self):
        """Create MCP server instances and the combined ASGI app."""
        from pinky_self.server import create_server as create_self_server
        from pinky_messaging.server import create_server as create_messaging_server

        # Shared pinky-self: agent_name="" (resolved via ContextVar), ALL gates
        all_gates = [
            "extras", "kb", "research", "presentations", "triggers",
            "schedule", "skill-admin", "admin", "tasks-admin",
        ]
        self_mcp = create_self_server(
            agent_name="",
            api_url=self._api_url,
            tool_gates=all_gates,
        )

        # Shared pinky-messaging: agent_name="" (resolved via ContextVar)
        messaging_mcp = create_messaging_server(
            agent_name="",
            api_url=self._api_url,
        )

        # Note: pinky-memory is NOT included yet — it needs a store object
        # per-agent, which requires a different approach. Phase 3.
        mcp_servers = {
            "self": self_mcp,
            "messaging": messaging_mcp,
        }

        return create_shared_app(mcp_servers)

    async def _supervisor_loop(self) -> None:
        """Supervisor that restarts the server on crash with exponential backoff."""
        backoff = 1  # seconds
        max_backoff = 60

        while self._running:
            app = self._create_app()
            try:
                await self._run_server(app)
            except asyncio.CancelledError:
                break
            except Exception as e:
                _log(f"[shared-mcp] Server crashed: {e} — restarting in {backoff}s")
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, max_backoff)
                continue
            # Normal exit (e.g. stop() called)
            break

        self._running = False
        _log("[shared-mcp] Supervisor exited")

    async def _run_server(self, app) -> None:
        """Run uvicorn serving the ASGI app. Raises on unexpected errors."""
        import uvicorn

        config = uvicorn.Config(
            app,
            host=self._host,
            port=self._port,
            log_level="warning",
        )
        self._server = uvicorn.Server(config)
        await self._server.serve()

    async def stop(self) -> None:
        """Gracefully stop the shared MCP server."""
        if self._server:
            self._server.should_exit = True
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except (asyncio.CancelledError, Exception):
                pass
        self._running = False
        _log("[shared-mcp] Stopped")

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def url(self) -> str:
        return f"http://{self._host}:{self._port}"
