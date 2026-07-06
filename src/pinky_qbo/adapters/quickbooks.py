"""Concrete QuickBooks Online adapter.

Holds a QboClient + QboTokenStore + a threading.Lock for safe refresh.
The _maybe_refresh() method:
  1. Acquires the lock.
  2. Re-reads the LATEST refresh token from the store (another thread may have
     already rotated it).
  3. If the access token is still valid, releases the lock and returns.
  4. Calls the QBO token endpoint with HTTP Basic (client_id:client_secret).
  5. Persists the NEW rotated refresh token atomically before releasing the lock.
  6. Caches the access token in memory with a 60-second skew expiry.

Report / lookup methods are thin stubs in P1 — data mapping lands in P2.
"""

from __future__ import annotations

import sys
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING

from pinky_qbo.adapters.base import (
    AbstractQboAdapter,
    Account,
    CompanyInfo,
    ReportResult,
)

if TYPE_CHECKING:
    from pinky_qbo.client import QboClient
    from pinky_qbo.store import QboTokenStore


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


_TOKEN_ENDPOINT = "https://oauth.platform.intuit.com/oauth2/v1/tokens/bearer"
_SKEW_SECONDS = 60


class QuickBooksAdapter(AbstractQboAdapter):
    """Concrete QBO adapter with locked token refresh."""

    def __init__(self, client: "QboClient", store: "QboTokenStore") -> None:
        self._client = client
        self._store = store
        self._lock = threading.Lock()

        # In-memory access token cache (never persisted)
        self._access_token: str | None = None
        self._token_expiry: datetime | None = None

    # ── Token management ──────────────────────────────────────────────────────

    def _is_expired(self) -> bool:
        """True when the cached access token is missing or within SKEW_SECONDS of expiry."""
        if not self._access_token:
            return True
        if self._token_expiry is None:
            return False
        return datetime.now(tz=timezone.utc) >= self._token_expiry - timedelta(seconds=_SKEW_SECONDS)

    def _maybe_refresh(self) -> str:
        """Return a valid access token, refreshing under the lock if needed.

        Refresh protocol (Murzik P1 correctness path):
          1. Fast path: if cached token is still valid, return immediately.
          2. Acquire lock.
          3. Re-check expiry (another thread may have refreshed while we waited).
          4. Re-read the latest refresh token from the store (rotation may have occurred).
          5. POST to Intuit token endpoint with HTTP Basic.
          6. Persist the NEW rotated refresh token atomically.
          7. Update in-memory cache.
          8. Release lock.
        """
        # Fast path (no lock needed for a quick validity check)
        if not self._is_expired():
            return self._access_token  # type: ignore[return-value]

        with self._lock:
            # Re-check under the lock — another thread may have refreshed
            if not self._is_expired():
                return self._access_token  # type: ignore[return-value]

            _log("[pinky-qbo] access token expired — refreshing under lock")

            # Re-read latest refresh token from store (may have rotated)
            refresh_token = self._store.get_refresh_token()
            if not refresh_token:
                raise RuntimeError("No refresh token available in store. Re-authorise QBO.")

            client_id, client_secret = self._store.get_client_credentials()
            if not client_id or not client_secret:
                raise RuntimeError("QBO client credentials not configured in store.")

            # POST to Intuit token endpoint
            import requests

            resp = requests.post(
                _TOKEN_ENDPOINT,
                auth=(client_id, client_secret),
                data={
                    "grant_type": "refresh_token",
                    "refresh_token": refresh_token,
                },
                headers={"Accept": "application/json"},
                timeout=30,
            )
            resp.raise_for_status()
            token_data = resp.json()

            new_access_token: str = token_data["access_token"]
            new_refresh_token: str = token_data.get("refresh_token", refresh_token)
            expires_in: int = int(token_data.get("expires_in", 3600))

            # Persist rotated refresh token atomically (under the lock)
            self._store.save_refresh_token(new_refresh_token)
            _log("[pinky-qbo] rotated refresh token persisted atomically")

            # Update in-memory access token cache
            self._access_token = new_access_token
            self._token_expiry = datetime.now(tz=timezone.utc) + timedelta(seconds=expires_in)

        return self._access_token  # type: ignore[return-value]

    def _token_provider(self) -> str:
        """Token provider callable passed to QboClient."""
        return self._maybe_refresh()

    # ── AbstractQboAdapter ────────────────────────────────────────────────────

    def profit_and_loss(
        self,
        start_date: str,
        end_date: str,
        *,
        accounting_method: str = "",
        summarize_column_by: str = "",
    ) -> ReportResult:
        """Fetch a Profit & Loss report. (P1 stub — data mapping in P2.)"""
        params: dict[str, str] = {
            "start_date": start_date,
            "end_date": end_date,
        }
        if accounting_method:
            params["accounting_method"] = accounting_method
        if summarize_column_by:
            params["summarize_column_by"] = summarize_column_by
        raw = self._client.report("ProfitAndLoss", params, _tool="qbo_profit_and_loss")
        return ReportResult(report_name="ProfitAndLoss", raw=raw)

    def balance_sheet(
        self,
        end_date: str,
        *,
        accounting_method: str = "",
    ) -> ReportResult:
        """Fetch a Balance Sheet report. (P1 stub — data mapping in P2.)"""
        params: dict[str, str] = {"date": end_date}
        if accounting_method:
            params["accounting_method"] = accounting_method
        raw = self._client.report("BalanceSheet", params, _tool="qbo_balance_sheet")
        return ReportResult(report_name="BalanceSheet", raw=raw)

    def cash_flow(
        self,
        start_date: str,
        end_date: str,
    ) -> ReportResult:
        """Fetch a Cash Flow report. (P1 stub — data mapping in P2.)"""
        params = {"start_date": start_date, "end_date": end_date}
        raw = self._client.report("CashFlow", params, _tool="qbo_cash_flow")
        return ReportResult(report_name="CashFlow", raw=raw)

    def aged_receivables(
        self,
        report_date: str,
        *,
        aging_method: str = "ReportDate",
    ) -> ReportResult:
        """Fetch an Aged Receivables report. (P1 stub — data mapping in P2.)"""
        params = {"report_date": report_date, "aging_method": aging_method}
        raw = self._client.report("AgedReceivables", params, _tool="qbo_aged_receivables")
        return ReportResult(report_name="AgedReceivables", raw=raw)

    def company_info(self) -> CompanyInfo:
        """Fetch company information. (P1 stub — data mapping in P2.)"""
        raw = self._client.companyinfo(_tool="qbo_company_info")
        ci = raw.get("CompanyInfo", {})
        return CompanyInfo(
            company_name=ci.get("CompanyName", ""),
            legal_name=ci.get("LegalName", ""),
            country=ci.get("Country", ""),
            fiscal_year_start_month=int(ci.get("FiscalYearStartMonth", 1)),
            company_start_date=ci.get("CompanyStartDate", ""),
            currency=ci.get("PrimaryPhone", {}).get("CurrencyRef", {}).get("value", "USD"),
            report_basis=ci.get("ReportBasis", ""),
            raw=raw,
        )

    def list_accounts(
        self,
        *,
        account_type: str = "",
        active_only: bool = True,
    ) -> list[Account]:
        """List chart of accounts. (P1 stub — data mapping in P2.)"""
        where_parts = []
        if active_only:
            where_parts.append("Active = true")
        if account_type:
            where_parts.append(f"AccountType = '{account_type}'")
        where = " AND ".join(where_parts) if where_parts else None
        raw = self._client.query("Account", where=where, _tool="qbo_list_accounts")
        accounts: list[Account] = []
        for item in raw.get("QueryResponse", {}).get("Account", []):
            accounts.append(
                Account(
                    account_id=item.get("Id", ""),
                    name=item.get("Name", ""),
                    account_type=item.get("AccountType", ""),
                    account_sub_type=item.get("AccountSubType", ""),
                    active=bool(item.get("Active", False)),
                    current_balance=float(item.get("CurrentBalance", 0.0)),
                    currency=item.get("CurrencyRef", {}).get("value", "USD"),
                    classification=item.get("Classification", ""),
                )
            )
        return accounts
