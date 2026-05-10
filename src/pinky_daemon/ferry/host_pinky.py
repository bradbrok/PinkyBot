"""@ferry/host-pinky — PinkyBot host-callback for ferry.

Receives ferry envelopes addressed to a PinkyBot agent and routes them
either as inbound platform messages (via broker.handle_inbound) or as
substrate-spec memory imports (via pinky-memory + pinky-self).

The behavior contract is documented in PinkyBot issue #413 and mirrored
in ferry repo's packages/host-pinky/README.md.

This module is the **adapter** — it does no transport (NATS / leaf-link
plumbing is owned by ferry-core) and does no model invocation (broker +
streaming sessions handle that). host-pinky's job is to translate one
identity primitive (signed ferry agent cards) into another (PinkyBot
agent + chat_id), enforce the peer-fleet ACL on the way in, and keep
substrate's port_history canonicality invariant on inbound (§6.4).
"""

from __future__ import annotations

import json
import sqlite3
import sys
from collections.abc import Callable
from dataclasses import asdict
from typing import Any

from pinky_daemon.ferry.substrate import (
    classify_entry_destination,
    populate_port_history,
    to_reflection_input,
    to_task_input,
)
from pinky_daemon.ferry.types import (
    AgentCardSelector,
    DeliveryResult,
    FerryEnvelope,
    IngestResult,
    SubstrateEntry,
)


class _TransientACLLoadError(Exception):
    """Raised by ``_load_peer_fleet_acl`` when the registry read failed for
    operator-side reasons (DB lock, schema mismatch during rolling deploy,
    etc.). Distinct from "ACL is empty" so the broker can replay rather
    than treat the message as policy-denied. Internal to host_pinky.
    """


def _log(msg: str) -> None:
    print(f"host-pinky: {msg}", file=sys.stderr, flush=True)


# -- Address parsing -----------------------------------------------------------

_FERRY_PINKY_PREFIX = "ferry://pinkybot/"


def parse_pinkybot_address(addr: str) -> str | None:
    """Extract the agent name from a ferry://pinkybot/<name> address.

    Returns the agent name on success, or None if the address is not
    pointed at this fleet.
    """
    if not addr:
        return None
    if not addr.startswith(_FERRY_PINKY_PREFIX):
        return None
    name = addr[len(_FERRY_PINKY_PREFIX):].strip()
    return name or None


def parse_peer_card(addr: str) -> tuple[str, str]:
    """Parse a peer agent's address into (fleet, agent_id).

    Examples:
      pulse@studio@sigil → ("sigil", "pulse@studio@sigil")
      ferry://sigil/pulse → ("sigil", "ferry://sigil/pulse")
      misha@pinky.local  → ("pinky.local", "misha@pinky.local")
    """
    if addr.startswith("ferry://"):
        rest = addr[len("ferry://"):]
        parts = rest.split("/", 1)
        fleet = parts[0] if parts else ""
        return (fleet, addr)
    # at-form: name@(scope@)?fleet — rightmost segment is the fleet
    parts = addr.split("@")
    if len(parts) >= 2:
        return (parts[-1], addr)
    return ("", addr)


# -- HostPinky -----------------------------------------------------------------


class HostPinky:
    """Per-daemon host-callback. Constructed once at daemon startup.

    Required collaborators:
      registry       — AgentRegistry (for agent lookup + peer_fleet_acl read)
      broker         — MessageBroker (for handle_inbound)
      memory_store   — pinky-memory MemoryStore (for substrate→reflection insert)
      task_store     — pinky-self TaskStore (for substrate→pending→create_task)

    The collaborators are passed by reference; HostPinky does not own them.
    """

    def __init__(
        self,
        *,
        registry: Any,
        broker: Any,
        memory_store: Any | None = None,
        task_store: Any | None = None,
        mesh_store: Any | None = None,
        verify_signatures: bool = False,
        reflection_factory: Callable[[dict[str, Any]], Any] | None = None,
    ) -> None:
        """Construct a HostPinky.

        ``reflection_factory`` (optional): callable that builds a Reflection
        from a kwargs dict. Defaults to ``pinky_memory.types.Reflection`` when
        unset. Inject in tests to avoid the pinky_memory import cost and to
        substitute a stand-in shape; production callers leave it as ``None``.

        ``mesh_store`` (optional): a ``MeshStore`` for inbound envelope
        audit + federation peer auto-registration (see #419). When ``None``,
        delivery proceeds without persistence.
        """
        self._registry = registry
        self._broker = broker
        self._memory_store = memory_store
        self._task_store = task_store
        self._mesh_store = mesh_store
        self._verify_signatures = verify_signatures
        self._reflection_factory = reflection_factory
        self._stats: dict[str, int] = {
            "delivered": 0,
            "rejected": 0,
            "queued": 0,
            "transient_failures": 0,
            "substrate_imports": 0,
            "messages_routed": 0,
        }

    @property
    def stats(self) -> dict[str, int]:
        return dict(self._stats)

    # -- Main entry point ------------------------------------------------------

    async def deliver(self, envelope: FerryEnvelope) -> DeliveryResult:
        """Deliver a ferry envelope to a PinkyBot agent.

        Implements the four-step contract from the host-pinky design doc:
          1. Verify ferry envelope signatures
          2. Resolve target agent
          3. Peer-fleet ACL check
          4. Dispatch by body.kind

        Side effect: if a ``mesh_store`` was passed at construction time,
        every call here logs one ``mesh_messages`` row with the final
        disposition (success or rejection reason).
        """
        result = await self._deliver_core(envelope)
        self._log_inbound_safely(envelope, result)
        return result

    async def _deliver_core(self, envelope: FerryEnvelope) -> DeliveryResult:
        """The main deliver pipeline. ``deliver()`` wraps this with logging."""
        # 1. Signature verification
        if self._verify_signatures:
            verify_err = self._verify(envelope)
            if verify_err:
                return self._reject("auth_failed", verify_err)

        # 2. Resolve target agent
        agent_name = parse_pinkybot_address(envelope.to)
        if not agent_name:
            return self._reject(
                "unknown_agent",
                f"address {envelope.to!r} is not a PinkyBot ferry address",
            )
        if not self._agent_exists(agent_name):
            return self._reject("unknown_agent", f"no agent named {agent_name!r}")

        # 3. Peer-fleet ACL check
        try:
            allowed = self._acl_allows(agent_name, envelope.from_)
        except _TransientACLLoadError as e:
            # Operator-side hiccup (DB lock, schema mismatch during rolling
            # deploy). Not a policy denial — the broker should retry rather
            # than terminally drop a message that an ACL would have allowed.
            return self._transient(
                "acl_load_failed",
                f"could not load peer_fleet_acl for {agent_name}: {e}",
            )
        if not allowed:
            return self._reject(
                "acl_denied",
                f"peer {envelope.from_!r} not on {agent_name}'s peer_fleet_acl",
            )

        # 4. Dispatch by payload kind
        kind = envelope.kind
        if kind == "message":
            return await self._deliver_message(agent_name, envelope)
        if kind in ("substrate.entry", "substrate.batch"):
            return await self._deliver_substrate(agent_name, envelope)

        return self._reject("unsupported_payload_kind", f"kind={kind!r}")

    def _log_inbound_safely(
        self,
        envelope: FerryEnvelope,
        result: DeliveryResult,
    ) -> None:
        """Best-effort log + peer-touch. Never propagates persistence errors —
        delivery semantics must not depend on whether mesh persistence is up."""
        if self._mesh_store is None:
            return
        try:
            local_agent = parse_pinkybot_address(envelope.to) or ""
            peer_fleet, peer_agent_id = parse_peer_card(envelope.from_)
            error: str | None = None
            if result.status in ("rejected", "transient_failure"):
                error = f"{result.status}:{result.reason or ''}"
            self._mesh_store.log_message(
                direction="inbound",
                local_agent=local_agent,
                remote_fleet=peer_fleet,
                remote_agent=peer_agent_id,
                correlation_id=envelope.correlation_id or "",
                kind=envelope.kind,
                body=envelope.body if isinstance(envelope.body, dict) else {"_raw": envelope.body},
                envelope_ts=envelope.ts,
                reply_to=envelope.reply_to,
                error=error,
            )
        except Exception as e:  # noqa: BLE001 — defensive: persistence must not break delivery
            _log(f"mesh_store inbound log failed (non-fatal): {e}")

    # -- Signature verification (deferred for v0.1) ---------------------------

    def _verify(self, envelope: FerryEnvelope) -> str | None:
        """Verify envelope.from_ + each traversal record's signature.

        Returns None on success, or a human-readable error string.

        v0.1: stub — accept all. Real implementation lands with the
        signed-agent-card subsystem in v0.2/v0.3 (per Pulse §12.3 thread).
        """
        # TODO(v0.2): real Ed25519 signature verification.
        return None

    # -- Agent lookup ---------------------------------------------------------

    def _agent_exists(self, agent_name: str) -> bool:
        try:
            return self._registry.has_agent(agent_name)
        except AttributeError:
            # Fallback: most AgentRegistry impls expose either has_agent or list_agents.
            try:
                names = {a.get("name") for a in self._registry.list_agents()}
                return agent_name in names
            except Exception:
                return False

    # -- Peer-fleet ACL -------------------------------------------------------

    def _acl_allows(self, agent_name: str, peer_address: str) -> bool:
        """Check peer_fleet_acl for the receiving agent.

        Default-deny: empty/missing ACL means no peer-fleet inbound allowed.
        """
        selectors = self._load_peer_fleet_acl(agent_name)
        if not selectors:
            return False
        peer_fleet, peer_agent_id = parse_peer_card(peer_address)
        for sel in selectors:
            if sel.matches(peer_fleet, peer_agent_id, ""):
                return True
        return False

    def _load_peer_fleet_acl(self, agent_name: str) -> list[AgentCardSelector]:
        """Load the agent's peer_fleet_acl selectors.

        Returns [] when the ACL is unset (policy: empty list = deny all).

        Raises ``_TransientACLLoadError`` when the registry read fails for
        operator-side reasons (DB lock, schema mismatch). The caller in
        ``deliver()`` translates that into a ``transient_failure`` delivery
        result so the broker retries instead of treating the message as
        terminally rejected.

        Per-item parse errors (malformed selector dict, invalid field
        combinations) are skipped silently — they don't taint the rest of
        the ACL or trigger a transient.
        """
        try:
            raw = self._registry.get_peer_fleet_acl(agent_name)
        except AttributeError:
            # Registry implementation doesn't expose peer_fleet_acl — older
            # registry shape. Treat as empty (deny). Not transient.
            return []
        except sqlite3.Error as e:
            raise _TransientACLLoadError(str(e)) from e
        if not raw:
            return []
        out: list[AgentCardSelector] = []
        for item in raw:
            try:
                if isinstance(item, AgentCardSelector):
                    out.append(item)
                elif isinstance(item, dict):
                    out.append(AgentCardSelector(**item))
            except (TypeError, ValueError, json.JSONDecodeError) as e:
                _log(f"skipping invalid ACL entry for {agent_name}: {item} ({e})")
        return out

    # -- Dispatch: message → broker.handle_inbound ----------------------------

    async def _deliver_message(
        self,
        agent_name: str,
        envelope: FerryEnvelope,
    ) -> DeliveryResult:
        """Route a ferry message envelope to the agent's streaming session.

        Identity-primitive separation: peer-fleet ACL was enforced
        upstream in this module's ``_acl_allows`` step. The peer-fleet
        identity (``ferry:<peer_address>``) is **not** written into the
        receiving agent's human-platform ``approved_users`` table — the
        two identity primitives stay disjoint at the broker boundary.

        Concretely: we call ``broker.dispatch_pre_authorized``, which
        routes the message straight to the agent's streaming session,
        bypassing ``handle_inbound``'s human-onboarding flow
        (``get_user_status`` → ``add_pending_user`` → ``/approve_…``
        Telegram prompt). That bypass is what makes the dedicated-path
        claim load-bearing-true at the broker surface.
        """
        from pinky_daemon.broker import BrokerMessage  # local import to avoid cycles

        text = str(envelope.body.get("text") or envelope.body.get("content") or "").strip()
        if not text:
            return self._reject("empty_message_body", "body.text was empty")

        peer_address = envelope.from_
        broker_msg = BrokerMessage(
            platform="ferry",
            chat_id=peer_address,
            sender_name=peer_address,
            sender_id=f"ferry:{peer_address}",
            content=text,
            agent_name=agent_name,
            timestamp=envelope.ts / 1000.0,
            message_id=envelope.id,
            chat_title=envelope.subject or "",
            is_group=False,
            metadata={
                "ferry": {
                    "envelope_id": envelope.id,
                    "from": envelope.from_,
                    "to": envelope.to,
                    "ts": envelope.ts,
                    "wake": envelope.wake,
                    "ack": envelope.ack,
                    "correlation_id": envelope.correlation_id,
                    "reply_to": envelope.reply_to,
                    "headers": dict(envelope.headers),
                    "traversal": [asdict(t) for t in envelope.traversal],
                }
            },
        )

        try:
            await self._broker.dispatch_pre_authorized(agent_name, broker_msg)
        except Exception as e:
            _log(
                f"broker.dispatch_pre_authorized failed for ferry envelope "
                f"{envelope.id}: {e}"
            )
            return DeliveryResult(status="rejected", reason="broker_error", detail={"error": str(e)})

        self._stats["delivered"] += 1
        self._stats["messages_routed"] += 1
        return DeliveryResult(status="delivered")

    # -- Dispatch: substrate.entry / substrate.batch --------------------------

    async def _deliver_substrate(
        self,
        agent_name: str,
        envelope: FerryEnvelope,
    ) -> DeliveryResult:
        """Import substrate entries into the receiving agent's stores."""
        entries = self._extract_substrate_entries(envelope)
        if not entries:
            return self._reject("empty_substrate_payload", "no entries in body")

        receiving_address = f"ferry://pinkybot/{agent_name}"
        results: list[IngestResult] = []
        for entry in entries:
            populated = populate_port_history(entry, envelope, receiving_address)
            try:
                result = self._ingest_one(agent_name, populated)
            except Exception as e:
                _log(f"ingest failed for substrate {entry.id}: {e}")
                results.append(
                    IngestResult(
                        substrate_id=entry.id,
                        target_store="skipped",
                        note=f"error: {e}",
                    )
                )
                continue
            results.append(result)

        self._stats["delivered"] += 1
        self._stats["substrate_imports"] += sum(1 for r in results if r.target_store != "skipped")
        return DeliveryResult(
            status="delivered",
            detail={"ingested": [asdict(r) for r in results]},
        )

    def _ingest_one(self, agent_name: str, entry: SubstrateEntry) -> IngestResult:
        """Route a single entry to its destination store."""
        destination = classify_entry_destination(entry)

        if destination == "pinky-memory":
            if self._memory_store is None:
                return IngestResult(
                    substrate_id=entry.id,
                    target_store="skipped",
                    note="memory_store not configured",
                )
            ref_kwargs = to_reflection_input(entry)
            ref_kwargs["project"] = agent_name
            inserted = self._insert_reflection(ref_kwargs)
            return IngestResult(
                substrate_id=entry.id,
                target_store="pinky-memory",
                target_id=str(inserted),
            )

        if destination == "pinky-self":
            if self._task_store is None:
                return IngestResult(
                    substrate_id=entry.id,
                    target_store="skipped",
                    note="task_store not configured",
                )
            task_kwargs = to_task_input(entry)
            task_kwargs["assigned_agent"] = agent_name
            task_kwargs["created_by"] = entry.source.by
            inserted = self._insert_task(task_kwargs)
            return IngestResult(
                substrate_id=entry.id,
                target_store="pinky-self",
                target_id=str(inserted),
            )

        return IngestResult(
            substrate_id=entry.id,
            target_store="skipped",
            note=f"destination={destination}",
        )

    def _insert_reflection(self, ref_kwargs: dict[str, Any]) -> str:
        """Insert into pinky-memory. Returns the inserted reflection id.

        Uses the configured ``reflection_factory`` if provided (test-only
        injection point), otherwise falls back to ``pinky_memory.types.Reflection``.
        """
        if self._reflection_factory is not None:
            reflection = self._reflection_factory(ref_kwargs)
        else:
            # Local import: pinky_memory is an optional dep at call time so
            # ferry tests don't have to install it.
            from pinky_memory.types import Reflection
            reflection = Reflection(**ref_kwargs)
        result = self._memory_store.insert(reflection)
        return getattr(result, "id", "") or getattr(reflection, "id", "")

    def _insert_task(self, task_kwargs: dict[str, Any]) -> str:
        """Insert into pinky-self task store. Returns task id (as str).

        ``task_store.create_task`` may return either a dict (older signature)
        or an object with ``.id`` (newer signature). Handle both explicitly
        rather than via a ternary that becomes ambiguous when ``.id`` is
        empty (the prior implementation would stringify the entire object
        as a "task id" in that case — see PR #414 round 2 finding #2).
        """
        result = self._task_store.create_task(**task_kwargs)
        if isinstance(result, dict):
            return str(result.get("id", ""))
        return str(getattr(result, "id", "") or "")

    # -- substrate envelope unwrapping ----------------------------------------

    def _extract_substrate_entries(self, envelope: FerryEnvelope) -> list[SubstrateEntry]:
        """Pull SubstrateEntry objects out of envelope.body.

        Supports both kinds:
          body.kind == "substrate.entry" → body.entry is one entry-shaped dict
          body.kind == "substrate.batch" → body.entries is a list of entry-shaped dicts
        """
        from pinky_daemon.ferry.types import (
            PortHistoryEntry,
            SubstrateLifecycle,
            SubstrateScope,
            SubstrateSource,
        )

        kind = envelope.kind
        raw_entries: list[dict[str, Any]] = []
        if kind == "substrate.entry":
            entry = envelope.body.get("entry")
            if isinstance(entry, dict):
                raw_entries.append(entry)
        elif kind == "substrate.batch":
            entries = envelope.body.get("entries")
            if isinstance(entries, list):
                raw_entries = [e for e in entries if isinstance(e, dict)]

        out: list[SubstrateEntry] = []
        for raw in raw_entries:
            try:
                out.append(
                    SubstrateEntry(
                        id=str(raw.get("id", "")),
                        type=raw.get("type", "fact"),
                        scope=SubstrateScope(**raw["scope"]),
                        source=SubstrateSource(**raw["source"]),
                        created_at=str(raw.get("created_at", "")),
                        updated_at=str(raw.get("updated_at", "")),
                        content=str(raw.get("content", "")),
                        links=list(raw.get("links") or []),
                        lifecycle=SubstrateLifecycle(**(raw.get("lifecycle") or {})),
                        port_history=[
                            PortHistoryEntry(**ph) if isinstance(ph, dict) else ph
                            for ph in (raw.get("port_history") or [])
                        ],
                    )
                )
            except Exception as e:
                _log(f"skipping malformed substrate entry: {e}")
        return out

    # -- Helpers --------------------------------------------------------------

    def _reject(self, reason: str, detail_text: str) -> DeliveryResult:
        self._stats["rejected"] += 1
        _log(f"reject reason={reason}: {detail_text}")
        return DeliveryResult(
            status="rejected",
            reason=reason,
            detail={"detail": detail_text},
        )

    def _transient(self, reason: str, detail_text: str) -> DeliveryResult:
        """Return a ``transient_failure`` result. Broker should retry."""
        self._stats["transient_failures"] += 1
        _log(f"transient_failure reason={reason}: {detail_text}")
        return DeliveryResult(
            status="transient_failure",
            reason=reason,
            detail={"detail": detail_text},
        )
