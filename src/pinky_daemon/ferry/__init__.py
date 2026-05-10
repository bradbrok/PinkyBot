"""Ferry — federated agent messaging integration.

`@ferry/host-pinky` Python implementation. Receives ferry envelopes addressed
to a PinkyBot agent and routes them either as inbound platform messages
(via broker.handle_inbound) or as substrate-spec memory imports (via
pinky-memory + pinky-self).

References:
- ferry PROTOCOL.md v0.1 — envelope shape
- substrate v0.1 — memory interchange spec, §12 worked example
- packages/host-pinky/README.md — design doc

See PinkyBot issue #413 for the behavior contract and acceptance test plan.
"""

from pinky_daemon.ferry.types import (
    AgentCardSelector,
    DeliveryResult,
    FerryEnvelope,
    PortHistoryEntry,
    SubstrateEntry,
    SubstrateLifecycle,
    SubstrateScope,
    SubstrateSource,
)

__all__ = [
    "AgentCardSelector",
    "DeliveryResult",
    "FerryEnvelope",
    "SubstrateEntry",
    "SubstrateScope",
    "SubstrateSource",
    "SubstrateLifecycle",
    "PortHistoryEntry",
]
