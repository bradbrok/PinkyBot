"""Abstract base class and dataclasses for QBO adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ReportResult:
    """Normalised container for a QBO report response."""

    report_name: str
    raw: dict  # The full API JSON response
    rows: list[dict] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)


@dataclass
class Account:
    """A single QBO chart-of-accounts entry."""

    account_id: str
    name: str
    account_type: str
    account_sub_type: str
    active: bool
    current_balance: float = 0.0
    currency: str = "USD"
    classification: str = ""


@dataclass
class CompanyInfo:
    """QBO company information."""

    company_name: str
    legal_name: str
    country: str
    fiscal_year_start_month: int
    company_start_date: str
    currency: str
    report_basis: str
    raw: dict = field(default_factory=dict)


class AbstractQboAdapter(ABC):
    """Common interface all QBO adapters must implement."""

    @abstractmethod
    def profit_and_loss(
        self,
        start_date: str,
        end_date: str,
        *,
        accounting_method: str = "",
        summarize_column_by: str = "",
    ) -> ReportResult:
        """Fetch a Profit & Loss report."""
        ...

    @abstractmethod
    def balance_sheet(
        self,
        end_date: str,
        *,
        accounting_method: str = "",
    ) -> ReportResult:
        """Fetch a Balance Sheet report."""
        ...

    @abstractmethod
    def cash_flow(
        self,
        start_date: str,
        end_date: str,
    ) -> ReportResult:
        """Fetch a Cash Flow report."""
        ...

    @abstractmethod
    def aged_receivables(
        self,
        report_date: str,
        *,
        aging_method: str = "ReportDate",
    ) -> ReportResult:
        """Fetch an Aged Receivables report."""
        ...

    @abstractmethod
    def company_info(self) -> CompanyInfo:
        """Fetch company information."""
        ...

    @abstractmethod
    def list_accounts(
        self,
        *,
        account_type: str = "",
        active_only: bool = True,
    ) -> list[Account]:
        """List chart of accounts."""
        ...
