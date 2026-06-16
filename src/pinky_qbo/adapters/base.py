"""Abstract base for QBO accounting adapters (spec §7).

An adapter owns the OAuth lifecycle (token freshness + rotation) and hands the
read-only :class:`~pinky_qbo.client.QBOClient` a fresh access-token provider. The
ABC keeps the server decoupled from QuickBooks specifics so a second provider
could be slotted in later without touching ``server.py``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass


@dataclass(repr=False)
class TokenBundle:
    """In-memory OAuth token state for one realm.

    ``refresh_token`` rotates on every use — the adapter persists the newest one
    atomically under a lock (see ``QuickBooksAdapter._maybe_refresh``). Never log
    these fields; ``__repr__`` is redacted so an accidental log/exception line
    can't leak token material.
    """

    access_token: str = ""
    refresh_token: str = ""
    # Monotonic-independent absolute expiry (epoch seconds) of the access token.
    expires_at: float = 0.0

    def __repr__(self) -> str:  # redacted — never leak token material
        a = "set" if self.access_token else "unset"
        r = "set" if self.refresh_token else "unset"
        return (
            f"TokenBundle(access_token=<{a}>, refresh_token=<{r}>, "
            f"expires_at={self.expires_at})"
        )


@dataclass
class AccountSummary:
    """Normalised chart-of-accounts row (read-only)."""

    account_id: str
    name: str
    account_type: str = ""
    account_subtype: str = ""
    current_balance: float = 0.0
    active: bool = True


class AbstractAccountingAdapter(ABC):
    """Common interface every accounting adapter must implement.

    All methods are READ-ONLY. There is intentionally no create/update/delete on
    this interface — the read-only guarantee is structural (spec §6).
    """

    @abstractmethod
    def get_report(self, report: str, params: dict[str, str], *, tool: str = "") -> dict:
        """Return a permitted report as a raw provider JSON dict."""
        ...

    @abstractmethod
    def get_company_info(self) -> dict:
        """Return the CompanyInfo (+ preferences) entity as a raw dict."""
        ...

    @abstractmethod
    def list_accounts(
        self,
        account_type: str = "",
        active_only: bool = True,
    ) -> list[AccountSummary]:
        """Return the chart of accounts (server-built SELECT)."""
        ...
