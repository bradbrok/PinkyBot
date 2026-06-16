"""pinky-qbo MCP server — read-only QuickBooks Online tools (spec §3).

Per-agent stdio FastMCP, mirroring ``pinky_calendar``. Bound at startup to ONE
agent + ONE realm + ONE env; the 7 tools take NO ``agent`` / ``realm`` args
(spec §5/§6.4). Each tool returns a JSON string.

In the stdio process there is no live ``AgentRegistry``, so the TokenStore reads
its agent-scoped settings directly from the agents DB (``PINKY_AGENTS_DB``) using
the registry's exact on-disk key convention ``agent:<agent>:<key>``.
"""

from __future__ import annotations

import json
import os
import sqlite3
import sys

from mcp.server.fastmcp import FastMCP

from pinky_qbo.adapters.quickbooks import QuickBooksAdapter
from pinky_qbo.client import QBOClientError, ReadOnlyViolation
from pinky_qbo.crypto import CryptoNotConfigured
from pinky_qbo.store import TokenStore

_ENV_DB = "PINKY_AGENTS_DB"


def _log(msg: str) -> None:
    print(f"[pinky-qbo] {msg}", file=sys.stderr, flush=True)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


# ── DB-backed agent-setting accessors (mirror AgentRegistry's on-disk keys) ────


def _db_path() -> str:
    return (os.environ.get(_ENV_DB, "") or "").strip()


def _set_agent_setting(agent: str, key: str, value: str) -> None:
    """Write an agent-scoped setting to system_settings as ``agent:<agent>:<key>``."""
    path = _db_path()
    if not path:
        raise RuntimeError(f"{_ENV_DB} not set")
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS system_settings (key TEXT PRIMARY KEY, value TEXT)"
        )
        conn.execute(
            "INSERT INTO system_settings (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=?",
            (f"agent:{agent}:{key}", value, value),
        )
        conn.commit()
    finally:
        conn.close()


def _get_agent_setting(agent: str, key: str, default: str = "") -> str:
    path = _db_path()
    if not path:
        return default
    conn = sqlite3.connect(path, timeout=10.0)
    try:
        row = conn.execute(
            "SELECT value FROM system_settings WHERE key=?",
            (f"agent:{agent}:{key}",),
        ).fetchone()
    except sqlite3.OperationalError:
        return default
    finally:
        conn.close()
    return row[0] if row and row[0] is not None else default


def create_server(
    agent: str,
    realm_id: str,
    env: str = "production",
    *,
    host: str = "127.0.0.1",
    port: int = 8106,
) -> FastMCP:
    """Build the per-agent QBO MCP server bound to ``agent`` + ``realm_id``."""
    mcp = FastMCP("pinky-qbo", host=host, port=port)

    store = TokenStore(
        agent=agent,
        set_fn=_set_agent_setting,
        get_fn=_get_agent_setting,
    )

    adapter: QuickBooksAdapter | None = None

    def _get_adapter() -> QuickBooksAdapter | None:
        """Lazily build the adapter, or None if not configured."""
        nonlocal adapter
        if adapter is not None:
            return adapter
        if not agent or not realm_id:
            return None
        if not store.is_configured() or not store.is_connected():
            return None
        # The store is authoritative for the realm (written on OAuth connect);
        # the argv --qbo-realm-id is the expected/fallback. Prefer the stored
        # realm so a re-auth to a different company can't desync the adapter from
        # a stale .mcp.json argv (review P2).
        effective_realm = store.get_realm_id() or realm_id
        adapter = QuickBooksAdapter(
            store=store, realm_id=effective_realm, agent=agent, env=env
        )
        return adapter

    def _not_configured() -> str:
        return _err(
            "QBO not configured for this agent. A client id/secret + a connected "
            "refresh token + realm must be present in the agent-scoped store."
        )

    def _run(fn, tool: str):
        """Run an adapter call, mapping errors to redacted JSON (never raises)."""
        a = _get_adapter()
        if not a:
            return _not_configured()
        try:
            return json.dumps(fn(a))
        except ReadOnlyViolation as e:
            # Should never happen via these tools, but surface as a hard error.
            return _err(f"read-only violation: {e}")
        except CryptoNotConfigured as e:
            return _err(f"crypto not configured: {e}")
        except QBOClientError as e:
            return _err(str(e))
        except Exception as e:  # already redacted at the client boundary
            return _err(f"{tool} failed: {e}")

    # ── helpers for report params ────────────────────────────────────────────

    def _report_params(
        start_date: str = "",
        end_date: str = "",
        date_macro: str = "",
        accounting_method: str = "",
        summarize_column_by: str = "",
        report_date: str = "",
        aging_method: str = "",
    ) -> dict[str, str]:
        p: dict[str, str] = {}
        if start_date:
            p["start_date"] = start_date
        if end_date:
            p["end_date"] = end_date
        if date_macro:
            p["date_macro"] = date_macro
        if accounting_method:
            p["accounting_method"] = accounting_method
        if summarize_column_by:
            p["summarize_column_by"] = summarize_column_by
        if report_date:
            p["report_date"] = report_date
        if aging_method:
            p["aging_method"] = aging_method
        return p

    # ── Tools (7, read-only, fixed-shape — spec §3) ──────────────────────────

    @mcp.tool()
    def qbo_profit_and_loss(
        start_date: str = "",
        end_date: str = "",
        date_macro: str = "",
        accounting_method: str = "",
        summarize_column_by: str = "",
    ) -> str:
        """Profit & Loss (income/expense summary).

        Args:
            start_date: ISO start date (YYYY-MM-DD). Use with end_date.
            end_date: ISO end date (YYYY-MM-DD).
            date_macro: OR a QBO macro (e.g. "This Fiscal Year-to-date").
            accounting_method: "Accrual" or "Cash" (default: company ReportBasis).
            summarize_column_by: e.g. "Month" to break out by period.

        Returns the raw QBO report JSON as a string.
        """
        params = _report_params(
            start_date=start_date,
            end_date=end_date,
            date_macro=date_macro,
            accounting_method=accounting_method,
            summarize_column_by=summarize_column_by,
        )
        return _run(
            lambda a: a.get_report("ProfitAndLoss", params, tool="qbo_profit_and_loss"),
            "qbo_profit_and_loss",
        )

    @mcp.tool()
    def qbo_balance_sheet(
        end_date: str = "",
        report_date: str = "",
        accounting_method: str = "",
    ) -> str:
        """Balance Sheet (assets/liabilities/equity = cash position).

        Args:
            end_date: ISO as-of date (YYYY-MM-DD).
            report_date: alias for the as-of date if end_date not given.
            accounting_method: "Accrual" or "Cash".

        Returns the raw QBO report JSON as a string.
        """
        params = _report_params(
            end_date=end_date,
            report_date=report_date,
            accounting_method=accounting_method,
        )
        return _run(
            lambda a: a.get_report("BalanceSheet", params, tool="qbo_balance_sheet"),
            "qbo_balance_sheet",
        )

    @mcp.tool()
    def qbo_cash_flow(
        start_date: str = "",
        end_date: str = "",
        date_macro: str = "",
    ) -> str:
        """Statement of Cash Flows (inflows/outflows = burn/runway).

        Args:
            start_date: ISO start date (YYYY-MM-DD).
            end_date: ISO end date (YYYY-MM-DD).
            date_macro: OR a QBO date macro.

        Returns the raw QBO report JSON as a string.
        """
        params = _report_params(
            start_date=start_date, end_date=end_date, date_macro=date_macro
        )
        return _run(
            lambda a: a.get_report("CashFlow", params, tool="qbo_cash_flow"),
            "qbo_cash_flow",
        )

    @mcp.tool()
    def qbo_aged_receivables(
        report_date: str = "",
        aging_method: str = "",
    ) -> str:
        """Aged Receivables (A/R aging by customer — "who owes us").

        Args:
            report_date: ISO as-of date (YYYY-MM-DD).
            aging_method: e.g. "Report_Date" or "Current".

        Returns the raw QBO report JSON as a string.
        """
        params = _report_params(report_date=report_date, aging_method=aging_method)
        return _run(
            lambda a: a.get_report(
                "AgedReceivables", params, tool="qbo_aged_receivables"
            ),
            "qbo_aged_receivables",
        )

    @mcp.tool()
    def qbo_expense_breakdown(
        start_date: str = "",
        end_date: str = "",
    ) -> str:
        """Top expense categories by month (P&L-by-month wrapper).

        Args:
            start_date: ISO start date (YYYY-MM-DD).
            end_date: ISO end date (YYYY-MM-DD).

        Returns the raw QBO report JSON as a string (ProfitAndLoss summarized
        by Month).
        """
        params = _report_params(
            start_date=start_date, end_date=end_date, summarize_column_by="Month"
        )
        return _run(
            lambda a: a.get_report(
                "ProfitAndLoss", params, tool="qbo_expense_breakdown"
            ),
            "qbo_expense_breakdown",
        )

    @mcp.tool()
    def qbo_company_info() -> str:
        """Company info + preferences (supplies report defaults).

        Returns the raw QBO CompanyInfo JSON as a string.
        """
        return _run(lambda a: a.get_company_info(), "qbo_company_info")

    @mcp.tool()
    def qbo_list_accounts(
        account_type: str = "",
        active_only: bool = True,
    ) -> str:
        """Chart of accounts with type/subtype/current balance.

        Args:
            account_type: optional QBO AccountType filter (e.g. "Bank", "Expense").
            active_only: only active accounts (default True).

        Returns a JSON list of accounts.
        """

        def _do(a: QuickBooksAdapter):
            rows = a.list_accounts(
                account_type=account_type, active_only=active_only
            )
            return [
                {
                    "id": r.account_id,
                    "name": r.name,
                    "type": r.account_type,
                    "subtype": r.account_subtype,
                    "current_balance": r.current_balance,
                    "active": r.active,
                }
                for r in rows
            ]

        return _run(_do, "qbo_list_accounts")

    _log(
        f"server ready (agent={agent}, realm=<bound>, env={env}, "
        f"configured={store.is_configured()}, connected={store.is_connected()})"
    )
    return mcp
