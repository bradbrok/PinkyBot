"""substrate v0.1 → PinkyBot stores translation.

Implements the five host-side unwrap steps from substrate v0.1 §12.5:
  1. Verify ferry envelope (deferred — see host_pinky.deliver())
  2. Populate port_history from envelope.traversal (§6.4 canonicality)
  3. Preserve source.by verbatim (§6.1 — never overwrite with immediate sender)
  4. Type-map per §11 reference impl notes (host-implementation-defined)
  5. Index rebuild (handled by pinky-memory's normal insert path)

Type mapping (host-pinky's choice, not the spec's):
  fact / event / reference  → pinky-memory `fact`
  feedback / decision       → pinky-memory `insight`
  pattern                   → pinky-memory `interaction_pattern`
  pending                   → pinky-self `create_task`

Trust → salience: salience = 1 + round(trust * 4) clamped [1, 5]
  (one valid mapping; not the mapping. Documented in host-pinky README.)

Links: pinky-memory has no native cross-entry edge surface, so substrate
`links` are preserved in `context` JSON sidecar. v0.2 §3.2.1 "no-link-surface
pattern" reference recipe will formalize the recall-time fan-out helper.
"""

from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from typing import Any

from pinky_daemon.ferry.types import (
    FerryEnvelope,
    PortHistoryEntry,
    SubstrateEntry,
)


def _log(msg: str) -> None:
    print(f"host-pinky/substrate: {msg}", file=sys.stderr, flush=True)


# -- §11 type mapping ----------------------------------------------------------

# substrate.type → pinky-memory ReflectionType.value
_PINKY_MEMORY_TYPE_MAP: dict[str, str] = {
    "fact": "fact",
    "event": "fact",
    "reference": "fact",
    "feedback": "insight",
    "decision": "insight",
    "pattern": "interaction_pattern",
    # "pending" is special-cased — routes to pinky-self, not pinky-memory
}


def map_substrate_type_to_reflection(substrate_type: str) -> str | None:
    """Return the pinky-memory ReflectionType.value for a substrate type, or None."""
    return _PINKY_MEMORY_TYPE_MAP.get(substrate_type)


def trust_to_salience(trust: float) -> int:
    """Trust [0.0, 1.0] → pinky-memory salience [1, 5].

    Formula: 1 + round(trust * 4), clamped [1, 5].

    This is host-pinky's mapping, not substrate's. Substrate stays
    trust-opaque per §6 — hosts decide how to discretize.
    """
    if trust <= 0:
        return 1
    if trust >= 1:
        return 5
    return max(1, min(5, 1 + round(trust * 4)))


# -- port_history population (§6.4) -------------------------------------------


def populate_port_history(
    entry: SubstrateEntry,
    envelope: FerryEnvelope,
    receiving_agent_address: str,
) -> SubstrateEntry:
    """Append one port_history entry per ferry traversal hop.

    Per substrate v0.1 §6.4: substrate's port_history is canonical;
    ferry's traversal is transport metadata. The receiving host walks
    the traversal array on inbound and writes one port_history record
    per hop, preserving from/to/at/via.

    Mutates and returns the entry.
    """
    if not envelope.traversal:
        # First port — no traversal recorded. Emit a single hop from
        # envelope.from_ → receiving agent so port_history reflects this
        # crossing even when traversal was empty (single-broker delivery).
        entry.port_history.append(
            PortHistoryEntry(
                from_=envelope.from_,
                to=receiving_agent_address,
                at=_iso_now(),
                via=envelope.id,
                re_grounded=False,
            )
        )
        return entry

    prior_address = envelope.from_
    for hop in envelope.traversal:
        entry.port_history.append(
            PortHistoryEntry(
                from_=prior_address,
                to=hop.broker,
                at=_ms_to_iso(hop.at),
                via=hop.via or envelope.id,
                re_grounded=False,
            )
        )
        prior_address = hop.broker
    # Final hop: last broker → receiving agent
    entry.port_history.append(
        PortHistoryEntry(
            from_=prior_address,
            to=receiving_agent_address,
            at=_iso_now(),
            via=envelope.id,
            re_grounded=False,
        )
    )
    return entry


# -- pinky-memory storage shape -----------------------------------------------


def to_reflection_input(entry: SubstrateEntry) -> dict[str, Any]:
    """Build a pinky-memory Reflection-shaped dict for a substrate entry.

    Returned dict is suitable for `MemoryStore.insert(Reflection(**d))`.
    Caller is responsible for resolving the project (typically the agent name).

    Keys produced:
      - type: pinky-memory ReflectionType.value
      - content: substrate entry content (markdown allowed)
      - context: JSON-encoded substrate sidecar (provenance, links, port_history)
      - salience: derived from trust
      - entities: [scope.ref] when scope.kind in {user, agent}
      - source_session_id: derived from source.by (for traceability)
    """
    reflection_type = map_substrate_type_to_reflection(entry.type)
    if reflection_type is None:
        raise ValueError(
            f"Substrate type {entry.type!r} does not map to a pinky-memory "
            "ReflectionType. (Did you mean to route 'pending' to pinky-self?)"
        )

    sidecar = {
        "substrate_id": entry.id,
        "substrate_type": entry.type,
        "scope": asdict(entry.scope),
        "source": asdict(entry.source),
        "lifecycle": asdict(entry.lifecycle),
        "links": list(entry.links),
        "port_history": [asdict(p) for p in entry.port_history],
        "created_at": entry.created_at,
        "updated_at": entry.updated_at,
    }

    entities: list[str] = []
    if entry.scope.kind in ("user", "agent") and entry.scope.ref:
        entities.append(entry.scope.ref)

    return {
        "type": reflection_type,
        "content": entry.content,
        "context": json.dumps(sidecar, ensure_ascii=False, sort_keys=True),
        "salience": trust_to_salience(entry.source.trust),
        "entities": entities,
        "source_channel": "ferry",
        "source_session_id": f"ferry:{entry.source.by}",
    }


# -- pinky-self task shape (for `pending` entries) ----------------------------


def to_task_input(entry: SubstrateEntry) -> dict[str, Any]:
    """Build a pinky-self task-shaped dict for a substrate `pending` entry.

    Lifecycle mapping (substrate → pinky-self task):
      pending  + expected_resolution_by → pending (with deps if known)
      pending  + no resolution date     → pending
      pending  + state=active(?)        → in_progress (rare on import)
    """
    if entry.type != "pending":
        raise ValueError(
            f"to_task_input called on non-pending entry (type={entry.type!r})"
        )

    title = entry.content.split("\n", 1)[0].strip()[:120] or "(unnamed substrate pending)"
    description_parts = [
        entry.content,
        "",
        "---",
        f"Imported from substrate (id: {entry.id})",
        f"Author: {entry.source.by} (origin: {entry.source.origin}, trust: {entry.source.trust})",
    ]
    if entry.lifecycle.expected_resolution_by:
        description_parts.append(
            f"Expected resolution: {entry.lifecycle.expected_resolution_by}"
        )
    if entry.source.evidence:
        description_parts.append(f"Evidence: {entry.source.evidence}")

    return {
        "title": title,
        "description": "\n".join(description_parts),
        "priority": _priority_from_trust(entry.source.trust),
        "tags": ["substrate", "substrate-pending", f"trust:{entry.source.trust:.2f}"],
        # Caller adds project / assigned_agent based on receiving agent.
    }


def _priority_from_trust(trust: float) -> str:
    """Map trust → task priority. Higher trust = higher priority."""
    if trust >= 0.85:
        return "high"
    if trust >= 0.5:
        return "normal"
    return "low"


# -- Top-level ingest dispatcher ----------------------------------------------


def classify_entry_destination(entry: SubstrateEntry) -> str:
    """Return one of: 'pinky-memory', 'pinky-self', 'skipped'.

    `pending` → pinky-self (cross-MCP integration, lifecycle-shaped).
    `decayed` lifecycle → skipped (per §5.1, the decay event imports
    as its own `event`-type entry, not via the decayed entry itself).
    Everything else → pinky-memory.
    """
    if entry.lifecycle.state == "decayed":
        return "skipped"
    if entry.type == "pending":
        return "pinky-self"
    if map_substrate_type_to_reflection(entry.type) is not None:
        return "pinky-memory"
    return "skipped"


# -- Time helpers --------------------------------------------------------------


def _iso_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _ms_to_iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000.0, tz=timezone.utc).isoformat()
