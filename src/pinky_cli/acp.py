"""ACP stdio proxy backed by PinkyBot daemon sessions.

Stdout belongs exclusively to the Agent Client Protocol transport. Diagnostics
from this module must always go to stderr.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import re
import sys
from pathlib import Path
from typing import Any
from uuid import uuid4

import httpx

from pinky_daemon.auth import build_internal_auth_headers

try:
    import acp
except ModuleNotFoundError as exc:  # Optional dependency; base installs stay usable.
    acp = None  # type: ignore[assignment]
    _ACP_IMPORT_ERROR: ModuleNotFoundError | None = exc
else:
    _ACP_IMPORT_ERROR = None


DEFAULT_KEEPALIVE_INTERVAL = 60.0
DEFAULT_BUSY_RETRIES = 10
DEFAULT_BUSY_RETRY_DELAY = 1.0
ACP_DEFAULT_ALLOWED_TOOLS = [
    "Bash",
    "Read",
    "Glob",
    "Grep",
    "Edit",
    "Write",
    "mcp__pinky-memory__*",
    "mcp__pinky-messaging__*",
    "mcp__pinky-self__*",
]
_AGENT_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_AgentBase = acp.Agent if acp is not None else object


class AcpDependencyError(RuntimeError):
    """Raised when ``pinky acp`` is used without its optional dependency."""


class DaemonBusyError(RuntimeError):
    """Raised after the bounded daemon busy retry window is exhausted."""


def _require_acp() -> None:
    if acp is None:
        raise AcpDependencyError(
            "pinky acp requires the optional ACP dependencies; "
            "install with `pip install 'pinky-ai[acp]'`"
        ) from _ACP_IMPORT_ERROR


def _stderr(message: str) -> None:
    print(f"[pinky-acp] {message}", file=sys.stderr, flush=True)


def _effective_permission_mode(agent_config: dict[str, Any]) -> str:
    """Choose an ACP-specific daemon policy without inheriting unsafe bypass."""
    agent_name = str(agent_config.get("name", "unknown"))
    override = os.environ.get("PINKY_ACP_PERMISSION_MODE", "")
    if override == "bypassPermissions":
        _stderr(
            f"agent '{agent_name}' bypassPermissions is not permitted on the ACP "
            "surface (owner policy); using dontAsk"
        )
        return "dontAsk"
    if override:
        _stderr(
            f"agent '{agent_name}' ACP permission mode is '{override}' from "
            "PINKY_ACP_PERMISSION_MODE"
        )
        return override

    configured = agent_config.get("permission_mode", "")
    configured = configured if isinstance(configured, str) else ""
    if configured == "bypassPermissions":
        _stderr(
            f"agent '{agent_name}' is configured bypassPermissions; ACP surface "
            "downgrades to dontAsk"
        )
        return "dontAsk"
    if not configured:
        _stderr(
            f"agent '{agent_name}' has no configured permission mode; ACP surface "
            "uses dontAsk"
        )
        return "dontAsk"

    _stderr(
        f"agent '{agent_name}' ACP permission mode is '{configured}' from agent config"
    )
    return configured


def _project_root() -> Path:
    """Resolve the PinkyBot checkout independently of the ACP client's cwd."""
    configured = os.environ.get("PINKY_PROJECT_ROOT", "").strip()
    candidates = []
    if configured:
        candidates.append(Path(configured))
    candidates.extend(
        [
            Path(sys.prefix).resolve().parent,
            Path(__file__).resolve().parents[2],
        ]
    )
    for candidate in candidates:
        if (candidate / "pyproject.toml").is_file() or (candidate / ".env").is_file():
            return candidate.resolve()
    return Path(__file__).resolve().parents[2]


def _read_session_secret(project_root: Path) -> str:
    """Read the daemon signing secret from the environment or checkout .env."""
    secret = os.environ.get("PINKY_SESSION_SECRET", "").strip()
    if secret:
        return secret

    env_path = project_root / ".env"
    if not env_path.is_file():
        return ""
    for raw_line in env_path.read_text().splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        if key.strip() == "PINKY_SESSION_SECRET":
            return value.strip().strip("\"'")
    return ""


class DaemonClient:
    """Small signed-httpx client for the daemon's manager session API."""

    def __init__(
        self,
        *,
        daemon_url: str,
        agent_name: str,
        secret: str,
        busy_retries: int = DEFAULT_BUSY_RETRIES,
        busy_retry_delay: float = DEFAULT_BUSY_RETRY_DELAY,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self.agent_name = agent_name
        self.secret = secret
        self.busy_retries = busy_retries
        self.busy_retry_delay = busy_retry_delay
        self._client = httpx.AsyncClient(
            base_url=daemon_url.rstrip("/"),
            timeout=httpx.Timeout(7200.0, connect=10.0),
            transport=transport,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        headers = build_internal_auth_headers(
            self.secret,
            agent_name=self.agent_name,
            method=method,
            path=path,
        )
        return await self._client.request(method, path, json=json, headers=headers)

    async def get_agent(self) -> dict[str, Any]:
        path = f"/agents/{self.agent_name}"
        response = await self._request("GET", path)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise ValueError("daemon returned an invalid agent configuration")
        return payload

    async def create_session(
        self,
        *,
        config: dict[str, Any],
        working_dir: Path,
    ) -> str:
        requested_id = f"acp-{self.agent_name}-{uuid4().hex[:12]}"
        permission_mode = _effective_permission_mode(config)
        configured_allowed_tools = config.get("allowed_tools", [])
        if not isinstance(configured_allowed_tools, list):
            configured_allowed_tools = []
        allowed_tools = configured_allowed_tools
        if permission_mode == "dontAsk":
            source = "agent-config"
            if not allowed_tools:
                allowed_tools = list(ACP_DEFAULT_ALLOWED_TOOLS)
                source = "acp-default"
            _stderr(
                f"agent '{self.agent_name}' ACP session uses dontAsk with {source} "
                f"allowed_tools: {', '.join(allowed_tools)}"
            )
            if "Bash" not in allowed_tools:
                _stderr(
                    f"warning: agent '{self.agent_name}' ACP allowlist lacks Bash; "
                    "the Buzz CLI reply path will be denied"
                )
        body = {
            "session_id": requested_id,
            "model": config.get("model", ""),
            "working_dir": str(working_dir),
            "allowed_tools": allowed_tools,
            "disallowed_tools": config.get("disallowed_tools", []),
            "max_turns": config.get("max_turns", 0),
            "timeout": config.get("timeout", 300.0),
            "restart_threshold_pct": config.get("restart_threshold_pct", 80.0),
            "auto_restart": config.get("auto_restart", True),
            "permission_mode": permission_mode,
        }
        response = await self._request("POST", "/sessions", json=body)
        response.raise_for_status()
        payload = response.json()
        session_id = payload.get("id") if isinstance(payload, dict) else None
        if not isinstance(session_id, str) or not session_id:
            raise ValueError("daemon did not return a session id")
        return session_id

    async def send_message(self, session_id: str, content: str) -> str:
        path = f"/sessions/{session_id}/message"
        for retry in range(self.busy_retries + 1):
            response = await self._request("POST", path, json={"content": content})
            if response.status_code != 409:
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise ValueError("daemon returned an invalid message response")
                error = payload.get("error")
                if error:
                    raise RuntimeError(f"daemon turn failed: {error}")
                result = payload.get("content", "")
                if not isinstance(result, str):
                    raise ValueError("daemon returned non-text message content")
                return result
            if retry == self.busy_retries:
                break
            await asyncio.sleep(self.busy_retry_delay)
        raise DaemonBusyError(
            f"daemon session remained busy after {self.busy_retries + 1} attempts"
        )


class PinkyAcpAgent(_AgentBase):
    """ACP agent backed by dedicated PinkyBot daemon sessions."""

    def __init__(
        self,
        *,
        agent_name: str,
        daemon: DaemonClient,
        project_root: Path,
        keepalive_interval: float = DEFAULT_KEEPALIVE_INTERVAL,
    ) -> None:
        _require_acp()
        if not _AGENT_NAME_RE.fullmatch(agent_name):
            raise ValueError("agent name must be lowercase alphanumeric, underscore, or hyphen")
        self.agent_name = agent_name
        self.daemon = daemon
        self.project_root = project_root.resolve()
        self.working_dir = self.project_root / "data" / "agents" / agent_name
        self.keepalive_interval = keepalive_interval
        self._conn: Any = None
        self._sessions: set[str] = set()
        self._cancel_events: dict[str, set[asyncio.Event]] = {}
        self._detached_tasks: set[asyncio.Task[Any]] = set()

    def on_connect(self, conn: Any) -> None:
        self._conn = conn

    async def close(self) -> None:
        for task in tuple(self._detached_tasks):
            task.cancel()
        if self._detached_tasks:
            await asyncio.gather(*self._detached_tasks, return_exceptions=True)
        await self.daemon.close()

    async def initialize(self, protocol_version: int, **kwargs: Any) -> Any:
        del protocol_version, kwargs
        return acp.InitializeResponse(
            protocolVersion=1,
            agentCapabilities=acp.schema.AgentCapabilities(
                loadSession=False,
                promptCapabilities=acp.schema.PromptCapabilities(embeddedContext=True),
                mcpCapabilities=acp.schema.McpCapabilities(http=False, sse=False),
            ),
            authMethods=[],
        )

    async def new_session(
        self,
        cwd: str,
        additional_directories: list[str] | None = None,
        mcp_servers: list[Any] | None = None,
        **kwargs: Any,
    ) -> Any:
        del cwd, additional_directories, kwargs
        if mcp_servers:
            _stderr(
                f"warning: ignoring {len(mcp_servers)} client MCP server(s); "
                "daemon-managed .mcp.json remains authoritative"
            )
        config = await self.daemon.get_agent()
        session_id = await self.daemon.create_session(
            config=config,
            working_dir=self.working_dir,
        )
        self._sessions.add(session_id)
        self._cancel_events.setdefault(session_id, set())
        return acp.NewSessionResponse(sessionId=session_id)

    async def prompt(
        self,
        session_id: str,
        prompt: list[Any],
        **kwargs: Any,
    ) -> Any:
        del kwargs
        if session_id not in self._sessions:
            await self._message(session_id, "Unknown ACP session; create a new session first.")
            return acp.PromptResponse(stopReason="refusal")

        content = flatten_content_blocks(prompt)
        cancel_event = asyncio.Event()
        self._cancel_events.setdefault(session_id, set()).add(cancel_event)
        send_task = asyncio.create_task(
            self.daemon.send_message(session_id, content),
            name=f"pinky-acp.send.{session_id}",
        )
        cancel_task = asyncio.create_task(
            cancel_event.wait(),
            name=f"pinky-acp.cancel-wait.{session_id}",
        )
        keepalive_task = asyncio.create_task(
            self._keepalive(session_id, cancel_event),
            name=f"pinky-acp.keepalive.{session_id}",
        )
        try:
            done, _ = await asyncio.wait(
                {send_task, cancel_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            if cancel_task in done or cancel_event.is_set():
                self._detach(send_task)
                return acp.PromptResponse(stopReason="cancelled")

            cancel_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await cancel_task
            try:
                response_text = await send_task
            except Exception as exc:
                _stderr(f"prompt failed for {session_id}: {exc}")
                await self._message(
                    session_id,
                    "PinkyBot could not complete this turn through the daemon. "
                    f"{exc}",
                )
                return acp.PromptResponse(stopReason="refusal")

            if cancel_event.is_set():
                return acp.PromptResponse(stopReason="cancelled")
            await self._message(session_id, response_text)
            return acp.PromptResponse(stopReason="end_turn")
        except asyncio.CancelledError:
            cancel_task.cancel()
            self._detach(send_task)
            raise
        finally:
            keepalive_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                _ = await keepalive_task
            active = self._cancel_events.get(session_id)
            if active is not None:
                active.discard(cancel_event)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        del kwargs
        for event in tuple(self._cancel_events.get(session_id, ())):
            event.set()

    async def _message(self, session_id: str, text: str) -> None:
        await self._conn.session_update(
            session_id=session_id,
            update=acp.update_agent_message(acp.text_block(text)),
        )

    async def _keepalive(self, session_id: str, cancel_event: asyncio.Event) -> None:
        try:
            while True:
                await asyncio.sleep(self.keepalive_interval)
                if cancel_event.is_set():
                    return
                await self._conn.session_update(
                    session_id=session_id,
                    update=acp.update_agent_thought(acp.text_block("Still working…")),
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            _stderr(f"keepalive stopped for {session_id}: {exc}")

    def _detach(self, task: asyncio.Task[Any]) -> None:
        if task.done():
            with contextlib.suppress(Exception, asyncio.CancelledError):
                task.result()
            return
        self._detached_tasks.add(task)

        def _finished(done: asyncio.Task[Any]) -> None:
            self._detached_tasks.discard(done)
            with contextlib.suppress(Exception, asyncio.CancelledError):
                done.result()

        task.add_done_callback(_finished)


def flatten_content_blocks(blocks: list[Any]) -> str:
    """Flatten ACP prompt content into the daemon's text-only request shape."""
    parts: list[str] = []
    for block in blocks:
        block_type = getattr(block, "type", "")
        if block_type == "text":
            parts.append(block.text)
        elif block_type == "resource":
            resource = block.resource
            text = getattr(resource, "text", None)
            if isinstance(text, str):
                parts.append(text)
            else:
                parts.append(f"[embedded resource: {resource.uri}]")
        elif block_type == "resource_link":
            parts.append(f"[{block.name}: {block.uri}]")
        elif block_type in {"image", "audio"}:
            uri = getattr(block, "uri", None)
            detail = uri or getattr(block, "mime_type", "unknown")
            parts.append(f"[{block_type}: {detail}]")
    return "\n".join(parts)


async def run_acp(agent_name: str, daemon_url: str) -> None:
    """Run the ACP JSON-RPC server over stdin/stdout."""
    _require_acp()
    project_root = _project_root()
    secret = _read_session_secret(project_root)
    if not secret:
        raise RuntimeError(
            "PINKY_SESSION_SECRET is required in the environment or PinkyBot .env"
        )
    daemon = DaemonClient(
        daemon_url=daemon_url,
        agent_name=agent_name,
        secret=secret,
    )
    agent = PinkyAcpAgent(
        agent_name=agent_name,
        daemon=daemon,
        project_root=project_root,
    )
    try:
        await acp.run_agent(agent)
    finally:
        await agent.close()
