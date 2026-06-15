"""One-click containerize orchestration (#204 / specs/one-click-containerize.md).

Brad wants enabling/disabling Podman container isolation on an agent to be one
click from the Agents UI. Containerize couples a lot of invariants — runtime
preflight, a default/override image, tmux transport, ``.mcp.json`` rewrite,
provision + a real runnability probe, stop/unregister of every old session, and
a FRESH session class (sdk→tmux can't be reconnected, only rebuilt). Keeping
that choreography here (backend-owned + testable) is the whole point: the API
handlers are thin, and the failure-mode invariants are unit-tested with a fake
``ContainerOps`` and no real Podman.

The cardinal invariant (Murzik's review): **never persist ``isolation_mode=
container`` before the slow/risky provision + probe succeed.** We build a
DESIRED in-memory :class:`Agent` copy, provision + start + verify it against the
container runtime, and only THEN flip the DB + rewrite MCP + bounce the session.
A bad image / failed pull therefore leaves the agent exactly as it was, never
stranded in an unrunnable container mode.

Provisioning can take MINUTES (a cold ``podman pull``), so the API returns 202
immediately and the UI polls a daemon-local operation record (this module's
:class:`ContainerOpRegistry`) for ``queued|validating|pulling|probing|applying|
restarting|ready|error``. The record is in-memory: after a daemon restart it is
gone and the status endpoint reconstructs a coarse ``idle|ready|unknown`` from
the registry + ``is_provisioned()`` rather than faking continuity.
"""

from __future__ import annotations

import asyncio
import dataclasses
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any

# ── operation status vocabulary ──────────────────────────────────────────────
# In-flight phases (non-terminal) + the two terminal states, plus the coarse
# reconstructed states used when there is no live op record (e.g. after a daemon
# restart). Strings (not an enum) to keep the JSON payload trivially serialisable
# and to match the contract the frontend (PR #782) polls against.
QUEUED = "queued"
VALIDATING = "validating"
PULLING = "pulling"
CREATING = "creating"
PROBING = "probing"
APPLYING = "applying"
RESTARTING = "restarting"
READY = "ready"
ERROR = "error"
IDLE = "idle"  # no operation in flight, agent is local
UNKNOWN = "unknown"  # container agent, pre-restart op record lost

# Non-terminal phases: while the record sits in one of these an operation is
# still running, so a second containerize/decontainerize for the same agent is
# rejected (409) rather than racing it.
_IN_FLIGHT = frozenset(
    {QUEUED, VALIDATING, PULLING, CREATING, PROBING, APPLYING, RESTARTING}
)

OP_CONTAINERIZE = "containerize"
OP_DECONTAINERIZE = "decontainerize"

# Image-ref sanity bounds. subprocess argv already prevents shell injection, but
# logs/UI/podman still deserve sane input (no whitespace-spanning or control
# chars, bounded length).
_IMAGE_MAX_LEN = 512


def validate_container_image(ref: str) -> str:
    """Return the trimmed image ref, or raise ``ValueError`` if it is empty,
    over-long, or contains whitespace / control characters."""
    img = (ref or "").strip()
    if not img:
        raise ValueError("container image is empty")
    if len(img) > _IMAGE_MAX_LEN:
        raise ValueError(f"container image too long (>{_IMAGE_MAX_LEN} characters)")
    if any(c.isspace() for c in img):
        raise ValueError("container image must not contain whitespace")
    if any(ord(c) < 0x20 or ord(c) == 0x7F for c in img):
        raise ValueError("container image must not contain control characters")
    return img


@dataclass
class ContainerOpRecord:
    """Daemon-local record of an in-flight (or just-finished) containerize /
    decontainerize operation for one agent. Polled by the UI via
    ``GET /agents/{name}/container-status``."""

    agent: str
    operation: str
    status: str
    message: str = ""
    image: str = ""
    provisioned: bool = False
    runtime_ready: bool = False
    started_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "operation": self.operation,
            "status": self.status,
            "message": self.message,
            "image": self.image,
            "provisioned": self.provisioned,
            "runtime_ready": self.runtime_ready,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
        }


class ContainerOpRegistry:
    """In-memory map ``agent -> ContainerOpRecord``. Single event loop, so no
    locking; provisioning runs off-loop but only the async caller mutates the
    record. Records are intentionally NOT persisted: a daemon restart drops them
    and the status endpoint reconstructs a coarse state instead of pretending an
    interrupted pull is still running."""

    def __init__(self) -> None:
        self._records: dict[str, ContainerOpRecord] = {}

    def begin(self, agent: str, operation: str, image: str = "") -> ContainerOpRecord:
        now = time.time()
        rec = ContainerOpRecord(
            agent=agent,
            operation=operation,
            status=QUEUED,
            image=image,
            started_at=now,
            updated_at=now,
        )
        self._records[agent] = rec
        return rec

    def update(
        self,
        agent: str,
        status: str,
        message: str | None = None,
        *,
        provisioned: bool | None = None,
        runtime_ready: bool | None = None,
    ) -> ContainerOpRecord | None:
        rec = self._records.get(agent)
        if rec is None:
            return None
        rec.status = status
        if message is not None:
            rec.message = message
        if provisioned is not None:
            rec.provisioned = provisioned
        if runtime_ready is not None:
            rec.runtime_ready = runtime_ready
        rec.updated_at = time.time()
        return rec

    def get(self, agent: str) -> ContainerOpRecord | None:
        return self._records.get(agent)

    def in_flight(self, agent: str) -> bool:
        rec = self._records.get(agent)
        return bool(rec and rec.status in _IN_FLIGHT)


@dataclass
class ContainerLifecycleDeps:
    """Everything :class:`ContainerLifecycle` needs, injected so the orchestration
    is testable with fakes (the provisioner is the REAL ContainerProvisioner over
    a recording ``ContainerOps``; the session/registry hooks are fakes)."""

    registry: Any
    # () -> ContainerProvisioner | None  (None when the host runtime gate is off)
    provisioner_for: Callable[[], Any | None]
    # (agent_name) -> None  — regenerate .mcp.json from the PERSISTED row
    write_mcp_json: Callable[[str], None]
    # (agent_name) -> awaitable[int]  — disconnect + unregister ALL labels
    disconnect_sessions: Callable[[str], Awaitable[int]]
    # (agent_name) -> awaitable  — cold-start a FRESH main session (selects the
    # session class from the now-updated row; must NOT re-take the lifecycle lock)
    start_session: Callable[[str], Awaitable[Any]]
    # (agent_name) -> bool  — is there any live session right now?
    has_live_session: Callable[[str], bool]
    # (agent_name) -> asyncio.Lock  — per-agent lifecycle lock (shared with the
    # streaming-ensure slow path so a wake can't start a session mid-apply)
    lifecycle_lock: Callable[[str], asyncio.Lock]
    op_registry: ContainerOpRegistry
    # off-loop executor for blocking podman work; asyncio.to_thread in prod, a
    # synchronous shim in tests.
    runner: Callable[..., Awaitable[Any]] = field(default=asyncio.to_thread)
    log: Callable[[str], None] = field(default=lambda _m: None)


class ContainerLifecycle:
    """Orchestrates one-click containerize / decontainerize.

    Phase 1 (provision + start + verify) runs against a DESIRED, non-persisted
    Agent copy and is deliberately OUTSIDE the per-agent lifecycle lock — it is
    the minutes-long pull, and the agent stays fully live in its current mode
    throughout. Only phase 2 (persist + rewrite MCP + stop-all + start-fresh) is
    taken under the lock, and it is fast because the image is already pulled +
    verified."""

    def __init__(self, deps: ContainerLifecycleDeps) -> None:
        self.deps = deps

    # -- containerize ---------------------------------------------------------
    async def containerize(self, name: str, *, image: str, start: bool = True) -> None:
        d = self.deps
        reg = d.op_registry
        prov = d.provisioner_for()
        if prov is None:
            reg.update(name, ERROR, "container runtime is not enabled on this host")
            return
        agent = d.registry.get(name)
        if agent is None:
            reg.update(name, ERROR, f"agent {name!r} not found")
            return

        # Phase 1 — provision + start + verify a DESIRED copy (NO DB write).
        # isolated=True + transport=tmux are coupled to container mode (the
        # pydantic validators that normally enforce this are bypassed when we
        # build the Agent in-process, so we set them ourselves).
        desired = dataclasses.replace(
            agent,
            isolation_mode="container",
            container_image=image,
            isolated=True,
            transport="tmux",
        )
        reg.update(name, PULLING, f"provisioning container from {image}")

        result = await d.runner(prov.provision, desired)
        if not getattr(result, "ok", False):
            await self._safe_deprovision(prov, desired)
            reg.update(name, ERROR, getattr(result, "message", "provision failed"))
            return

        reg.update(name, PROBING, "starting + verifying container image")
        try:
            await d.runner(prov.start, desired)
        except Exception as e:  # noqa: BLE001 — surface as a clean op error
            await self._safe_deprovision(prov, desired)
            reg.update(name, ERROR, f"container failed to start: {e}")
            return

        verify = await d.runner(prov.verify_runnable, desired)
        if not getattr(verify, "ok", False):
            await self._safe_deprovision(prov, desired)
            reg.update(name, ERROR, getattr(verify, "message", "image not runnable"),
                       provisioned=False)
            return

        # Phase 2 — apply: persist → rewrite MCP → stop all → start fresh. Under
        # the per-agent lifecycle lock so a concurrent wake can't start a session
        # against a half-applied row.
        async with d.lifecycle_lock(name):
            reg.update(name, APPLYING, "persisting container configuration")
            d.registry.register(
                name,
                isolation_mode="container",
                container_image=image,
                isolated=True,
                transport="tmux",
            )
            d.write_mcp_json(name)
            had_live = d.has_live_session(name)
            await d.disconnect_sessions(name)
            if start and had_live:
                reg.update(name, RESTARTING, "starting containerized session")
                try:
                    await d.start_session(name)
                except Exception as e:  # noqa: BLE001
                    # The flip + MCP rewrite already landed; the agent will spawn
                    # containerized at its next wake. Report rather than fake ready.
                    reg.update(
                        name,
                        ERROR,
                        f"containerized, but session restart failed (will start "
                        f"on next wake): {e}",
                        provisioned=True,
                        runtime_ready=True,
                    )
                    return

        reg.update(
            name,
            READY,
            f"containerized on {image} (home volume created)",
            provisioned=True,
            runtime_ready=True,
        )

    # -- decontainerize -------------------------------------------------------
    async def decontainerize(self, name: str, *, start: bool = True) -> None:
        d = self.deps
        reg = d.op_registry
        agent = d.registry.get(name)
        if agent is None:
            reg.update(name, ERROR, f"agent {name!r} not found")
            return

        async with d.lifecycle_lock(name):
            reg.update(name, APPLYING, "reverting to local (home volume kept)")
            # Tear down container + key secret but KEEP the home volume (its CLI
            # logins survive a future re-containerize). Best-effort: a gate-off
            # host has nothing provisioned; either way we still flip the DB.
            prov = d.provisioner_for()
            if prov is not None:
                try:
                    await d.runner(prov.deprovision, agent)  # remove_volume=False
                except Exception as e:  # noqa: BLE001
                    d.log(f"container_ops: deprovision of {name!r} failed (ignored): {e}")
            # Lift isolation: local mode → isolated=False. transport is left as
            # tmux (local+tmux is valid); the operator can switch to sdk in the
            # Runtime tab if desired.
            d.registry.register(name, isolation_mode="local", isolated=False)
            d.write_mcp_json(name)
            had_live = d.has_live_session(name)
            await d.disconnect_sessions(name)
            if start and had_live:
                reg.update(name, RESTARTING, "starting local session")
                try:
                    await d.start_session(name)
                except Exception as e:  # noqa: BLE001
                    reg.update(
                        name,
                        ERROR,
                        f"reverted to local, but session restart failed (will start "
                        f"on next wake): {e}",
                    )
                    return

        reg.update(name, READY, "reverted to local (home volume preserved)",
                   provisioned=False)

    async def _safe_deprovision(self, prov: Any, agent: Any) -> None:
        """Best-effort cleanup of a failed containerize: remove the container +
        secret we just created, KEEP the home volume (never destroy a volume
        implicitly). Never raises — the DB was never mutated, so the agent is
        already intact; this just avoids orphaning the partial podman tenant."""
        try:
            await self.deps.runner(prov.deprovision, agent)
        except Exception as e:  # noqa: BLE001
            self.deps.log(
                f"container_ops: cleanup deprovision of {agent.name!r} failed "
                f"(ignored): {e}"
            )
