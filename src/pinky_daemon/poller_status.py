"""Shared status contract for legacy and broker platform pollers."""

from typing import Protocol


class PollerStatus(Protocol):
    """Required status surface; platform-specific health fields are optional.

    Polling transports count completed polls. Socket transports count processed
    envelopes. A quiet socket is healthy, so it need not expose last_poll_ok.
    """

    @property
    def agent_name(self) -> str:
        """Agent identity, or 'legacy' for the single-handler poller."""
        ...

    @property
    def poll_count(self) -> int:
        """Number of polls or socket envelopes processed since construction."""
        ...

    @property
    def is_running(self) -> bool:
        """Whether the poller is running."""
        ...
