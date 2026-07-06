"""pinky-qbo MCP Server — read-only QuickBooks Online tools for onesie.

Exposes 7 v1 tools:
  qbo_profit_and_loss, qbo_balance_sheet, qbo_cash_flow,
  qbo_aged_receivables, qbo_expense_breakdown, qbo_company_info,
  qbo_list_accounts.

Tools do NOT accept agent or realm args — both are bound at startup.
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

from mcp.server.fastmcp import FastMCP

if TYPE_CHECKING:
    from pinky_qbo.store import QboTokenStore


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


_NOT_CONFIGURED = (
    "QBO not configured. Ensure PINKY_AGENTS_DB is set and client credentials "
    "and refresh token are seeded in the agent-scoped store."
)


def create_server(
    *,
    agent: str,
    realm_id: str,
    env: str,
    store: "QboTokenStore | None",
    host: str = "127.0.0.1",
    port: int = 8110,
) -> FastMCP:
    """Build and return the FastMCP server instance.

    Args:
        agent:    Agent name (must be "onesie"; enforced in __main__).
        realm_id: QBO company realm ID (bound at startup).
        env:      "sandbox" or "production".
        store:    Agent-scoped QboTokenStore, or None if PINKY_AGENTS_DB unset.
        host:     SSE bind host (stdio ignores this).
        port:     SSE bind port.
    """
    mcp = FastMCP("pinky-qbo", host=host, port=port)

    # Lazy adapter — only built if the store is configured
    _adapter = None

    def _get_adapter():
        nonlocal _adapter
        if _adapter is not None:
            return _adapter
        if store is None or not store.is_configured():
            return None
        from pinky_qbo.adapters.quickbooks import QuickBooksAdapter
        from pinky_qbo.client import QboClient

        def _token_provider() -> str:
            return _adapter._token_provider()  # type: ignore[union-attr]

        # Build the client with a forward reference to _adapter for token provision.
        # We use a lambda closure so the adapter can set itself after construction.
        _client = QboClient(
            realm_id=realm_id,
            env=env,
            token_provider=lambda: _adapter._maybe_refresh(),  # type: ignore[union-attr]
        )
        _adapter = QuickBooksAdapter(client=_client, store=store)
        return _adapter

    # ── Tools ─────────────────────────────────────────────────────────────────

    @mcp.tool()
    def qbo_profit_and_loss(
        start_date: str,
        end_date: str,
        accounting_method: str = "",
        summarize_column_by: str = "",
    ) -> str:
        """Fetch a QuickBooks Online Profit & Loss report.

        Returns an income/expense summary for the specified date range.
        Date range must be 6 months or less per call.

        Args:
            start_date: Report start date in YYYY-MM-DD format.
            end_date: Report end date in YYYY-MM-DD format.
            accounting_method: "Accrual" or "Cash". Defaults to company setting.
            summarize_column_by: Optional grouping (e.g. "Month", "Quarter").

        Returns JSON with the full P&L report or an error.
        """
        a = _get_adapter()
        if not a:
            return _err(_NOT_CONFIGURED)
        try:
            result = a.profit_and_loss(
                start_date,
                end_date,
                accounting_method=accounting_method,
                summarize_column_by=summarize_column_by,
            )
            return json.dumps(result.raw)
        except Exception as exc:
            return _err(f"qbo_profit_and_loss failed: {exc}")

    @mcp.tool()
    def qbo_balance_sheet(
        end_date: str,
        accounting_method: str = "",
    ) -> str:
        """Fetch a QuickBooks Online Balance Sheet report.

        Returns assets, liabilities, and equity — the company's net cash position
        as of the specified date.

        Args:
            end_date: Report date in YYYY-MM-DD format (e.g. "2024-12-31").
            accounting_method: "Accrual" or "Cash". Defaults to company setting.

        Returns JSON with the full Balance Sheet or an error.
        """
        a = _get_adapter()
        if not a:
            return _err(_NOT_CONFIGURED)
        try:
            result = a.balance_sheet(end_date, accounting_method=accounting_method)
            return json.dumps(result.raw)
        except Exception as exc:
            return _err(f"qbo_balance_sheet failed: {exc}")

    @mcp.tool()
    def qbo_cash_flow(
        start_date: str,
        end_date: str,
    ) -> str:
        """Fetch a QuickBooks Online Cash Flow Statement.

        Returns inflows and outflows — useful for burn rate and runway analysis.
        Date range must be 6 months or less per call.

        Args:
            start_date: Report start date in YYYY-MM-DD format.
            end_date: Report end date in YYYY-MM-DD format.

        Returns JSON with the full Cash Flow report or an error.
        """
        a = _get_adapter()
        if not a:
            return _err(_NOT_CONFIGURED)
        try:
            result = a.cash_flow(start_date, end_date)
            return json.dumps(result.raw)
        except Exception as exc:
            return _err(f"qbo_cash_flow failed: {exc}")

    @mcp.tool()
    def qbo_aged_receivables(
        report_date: str,
        aging_method: str = "ReportDate",
    ) -> str:
        """Fetch a QuickBooks Online Aged Receivables report.

        Returns accounts receivable aging by customer — who owes money and for
        how long. Covers the 'who owes us' question without raw invoice rows.

        Args:
            report_date: Report date in YYYY-MM-DD format (e.g. "2024-12-31").
            aging_method: "ReportDate" (default) or "CurrentDate".

        Returns JSON with the full AR Aging report or an error.
        """
        a = _get_adapter()
        if not a:
            return _err(_NOT_CONFIGURED)
        try:
            result = a.aged_receivables(report_date, aging_method=aging_method)
            return json.dumps(result.raw)
        except Exception as exc:
            return _err(f"qbo_aged_receivables failed: {exc}")

    @mcp.tool()
    def qbo_expense_breakdown(
        start_date: str,
        end_date: str,
    ) -> str:
        """Fetch a QuickBooks Online expense breakdown by category.

        Returns top expense categories for the period using a P&L-by-month
        wrapper. Date range must be 6 months or less per call.

        Args:
            start_date: Report start date in YYYY-MM-DD format.
            end_date: Report end date in YYYY-MM-DD format.

        Returns JSON with P&L-by-month data focused on expenses, or an error.
        """
        a = _get_adapter()
        if not a:
            return _err(_NOT_CONFIGURED)
        try:
            # Expense breakdown uses ProfitAndLoss summarised by month
            result = a.profit_and_loss(
                start_date,
                end_date,
                summarize_column_by="Month",
            )
            return json.dumps(result.raw)
        except Exception as exc:
            return _err(f"qbo_expense_breakdown failed: {exc}")

    @mcp.tool()
    def qbo_company_info() -> str:
        """Fetch QuickBooks Online company information and preferences.

        Returns company name, legal name, country, fiscal year start, currency,
        and report basis defaults. Useful as a first call to get context for
        interpreting other reports.

        Returns JSON with CompanyInfo or an error.
        """
        a = _get_adapter()
        if not a:
            return _err(_NOT_CONFIGURED)
        try:
            info = a.company_info()
            return json.dumps({
                "company_name": info.company_name,
                "legal_name": info.legal_name,
                "country": info.country,
                "fiscal_year_start_month": info.fiscal_year_start_month,
                "company_start_date": info.company_start_date,
                "currency": info.currency,
                "report_basis": info.report_basis,
            })
        except Exception as exc:
            return _err(f"qbo_company_info failed: {exc}")

    @mcp.tool()
    def qbo_list_accounts(
        account_type: str = "",
        active_only: bool = True,
    ) -> str:
        """List QuickBooks Online chart of accounts.

        Returns account names, types, subtypes, and current balances.
        Useful for understanding the account structure before pulling reports.

        Args:
            account_type: Filter by account type (e.g. "Expense", "Income",
                "Bank", "Accounts Receivable"). Empty returns all types.
            active_only: If True (default), returns only active accounts.

        Returns JSON array of accounts or an error.
        """
        a = _get_adapter()
        if not a:
            return _err(_NOT_CONFIGURED)
        try:
            accounts = a.list_accounts(account_type=account_type, active_only=active_only)
            return json.dumps([
                {
                    "id": acc.account_id,
                    "name": acc.name,
                    "type": acc.account_type,
                    "sub_type": acc.account_sub_type,
                    "active": acc.active,
                    "current_balance": acc.current_balance,
                    "currency": acc.currency,
                    "classification": acc.classification,
                }
                for acc in accounts
            ])
        except Exception as exc:
            return _err(f"qbo_list_accounts failed: {exc}")

    _log(
        f"pinky-qbo: server ready (agent={agent}, realm={realm_id or 'not set'}, "
        f"env={env}, configured={store.is_configured() if store else False})"
    )
    return mcp
