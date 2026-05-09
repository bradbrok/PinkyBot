"""Ferry envelope + substrate entry dataclasses.

Mirrors:
- ferry PROTOCOL.md v0.1 §1 envelope schema
- substrate v0.1 §3 entry schema

These are wire-shapes — they describe what crosses the boundary, not
how PinkyBot stores things internally. Storage shapes (Reflection in
pinky-memory, Task in pinky-self) are separate; the substrate.py module
translates between the two.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

# -- Ferry PROTOCOL.md v0.1 §1 -------------------------------------------------


@dataclass
class TraversalRecord:
    """One broker hop on a ferry envelope's traversal path.

    Per PROTOCOL.md §12.3, brokers append (never modify) traversal records
    as the envelope crosses each broker. The receiving host-callback uses
    the array to populate substrate's port_history on inbound (§6.4).
    """

    broker: str  # broker identity (e.g. "ferry-broker:nova")
    at: int  # broker wall-clock millis when received
    via: str = ""  # link/transport identifier (optional)
    signature: str = ""  # broker signature over the prior envelope state


@dataclass
class FerryEnvelope:
    """A ferry envelope, per PROTOCOL.md v0.1 §1.

    Top-level fields are spec-defined; `headers` is the only place
    extension metadata may live.
    """

    v: str  # protocol version, e.g. "0.1"
    id: str  # uuid-v7, transport-stable, broker-deduped within 24h
    from_: str  # immediate sender address
    to: str  # recipient address
    ts: int  # sender wall-clock millis
    body: dict[str, Any]  # application payload — must contain "kind"

    priority: Literal["low", "normal", "high", "urgent"] = "normal"
    wake: Literal["none", "if-idle", "when-free", "interrupt"] = "if-idle"
    ack: Literal["none", "delivery", "processed"] = "delivery"

    ttl_ms: int | None = None
    correlation_id: str | None = None
    reply_to: str | None = None
    subject: str | None = None
    traversal: list[TraversalRecord] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def kind(self) -> str:
        """Payload kind — `body.kind` per PROTOCOL.md §1 + ferry overlay docs."""
        return str(self.body.get("kind", "")).strip()


# -- DeliveryResult — host-pinky's contract with the ferry broker --------------


@dataclass
class DeliveryResult:
    """Result of host-pinky's `deliver(envelope)` call.

    The ferry broker uses the status to decide ack semantics (PROTOCOL.md §6):
    - `delivered`: at-least-once delivery satisfied, processing started
    - `queued`: agent offline / asleep; envelope will be replayed on wake
    - `rejected`: do not retry — auth, ACL, or unsupported payload (terminal)
    - `transient_failure`: retry with backoff — operational failure (DB lock,
      schema mismatch during rolling deploy, etc.) on the host side. Distinct
      from `rejected` so the broker doesn't swallow a real ACL-allowed message
      because of an operator-side hiccup.
    """

    status: Literal["delivered", "queued", "rejected", "transient_failure"]
    reason: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


# -- AgentCardSelector — peer-fleet ACL primitive ------------------------------


@dataclass(frozen=True)
class AgentCardSelector:
    """Selector for a peer-fleet agent card.

    At-least-one field must be set. Wildcards via `agent_id="*"` (matches
    any agent on the fleet) and `fleet="*"` (matches any fleet — use sparingly).

    Examples:
        AgentCardSelector(fleet="sigil", agent_id="pulse@studio@sigil")
        AgentCardSelector(fleet="sigil", agent_id="*")  # fleet-wide allow
        AgentCardSelector(pinky_type="executor")  # capability-based
    """

    fleet: str | None = None
    agent_id: str | None = None
    pinky_type: str | None = None

    def __post_init__(self) -> None:
        if not (self.fleet or self.agent_id or self.pinky_type):
            raise ValueError(
                "AgentCardSelector requires at least one of fleet, agent_id, "
                "or pinky_type — empty selectors are not permitted."
            )

    def matches(self, card_fleet: str, card_agent_id: str, card_pinky_type: str = "") -> bool:
        """Test whether a peer's signed card matches this selector."""
        if self.fleet is not None and self.fleet != "*" and self.fleet != card_fleet:
            return False
        if self.agent_id is not None and self.agent_id != "*" and self.agent_id != card_agent_id:
            return False
        if self.pinky_type is not None and self.pinky_type != card_pinky_type:
            return False
        return True


# -- substrate v0.1 §3 entry schema --------------------------------------------


@dataclass
class SubstrateScope:
    """substrate v0.1 §3.3 scope — what the entry is *about*."""

    kind: Literal["user", "agent", "project", "global"]
    ref: str  # e.g. "brad", "misha@pinky.local", "ferry-v0.1"


@dataclass
class SubstrateSource:
    """substrate v0.1 §6 provenance — where the claim came from."""

    origin: Literal["user-stated", "agent-inferred", "agent-observed", "external"]
    by: str  # original author identity (e.g. "misha@pinky.local") — §6.1 immutable on port
    evidence: str = ""  # human-readable trace; quotes, refs, observations
    trust: float = 0.5  # [0.0, 1.0]; opaque to spec, host decides what it means


@dataclass
class SubstrateLifecycle:
    """substrate v0.1 §5 lifecycle state."""

    state: Literal["active", "pending", "decayed", "superseded"] = "active"
    expected_resolution_by: str | None = None  # ISO datetime
    resolved_by: str | None = None  # substrate id of resolver entry


@dataclass
class PortHistoryEntry:
    """substrate v0.1 §6.4 — one hop in the entry's cross-fleet history.

    Populated on inbound from ferry's traversal array. Substrate's
    port_history is canonical (§6.4); ferry's traversal is transport-only.

    Trust boundary on `at`:
      - `attested_by="broker"` — `at` mirrors a broker-stamped traversal
        record's wall-clock time. Witnessed by a transport intermediary.
      - `attested_by="receiver"` — `at` was fabricated by the receiver
        (synthetic hop, no broker traversal record). The receiver's clock
        is the only witness. Don't conflate with broker-attested `at`
        when reasoning about cross-fleet timing.
    """

    from_: str
    to: str
    at: str  # ISO datetime
    via: str = ""  # ferry msg_id (transport-stable id)
    re_grounded: bool = False  # whether receiver re-verified the claim
    attested_by: Literal["broker", "receiver"] = "broker"


@dataclass
class SubstrateEntry:
    """A substrate v0.1 §3 entry — wire shape, pre-storage.

    On inbound to host-pinky, this is what we get out of `envelope.body`
    (for `kind=substrate.entry`) or each item of `body.entries` (for
    `kind=substrate.batch`). The substrate.py module translates this
    into pinky-memory Reflection / pinky-self Task primitives.
    """

    id: str  # substrate-stable id (uuid) — distinct from ferry transport id
    type: Literal["fact", "feedback", "decision", "pattern", "event", "reference", "pending"]
    scope: SubstrateScope
    source: SubstrateSource
    created_at: str  # ISO datetime
    updated_at: str  # ISO datetime
    content: str  # free-text body (markdown allowed)
    links: list[str] = field(default_factory=list)  # substrate ids of linked entries
    lifecycle: SubstrateLifecycle = field(default_factory=SubstrateLifecycle)
    port_history: list[PortHistoryEntry] = field(default_factory=list)


# -- Ingest result -------------------------------------------------------------


@dataclass
class IngestResult:
    """Outcome of a single substrate-entry import into PinkyBot stores."""

    substrate_id: str
    target_store: Literal["pinky-memory", "pinky-self", "skipped"]
    target_id: str = ""  # reflection id / task id
    note: str = ""
