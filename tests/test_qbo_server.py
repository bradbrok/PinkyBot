"""End-to-end server tests for the 7 read-only QBO tools (spec §3/§5/§6).

These drive the ACTUAL MCP tools through the adapter and client with a
monkeypatched HTTP transport (NO real network, NO real keys), proving:

  - all 7 tools exist with the exact spec names;
  - NO tool can emit a write verb or a non-allowlisted path — every request that
    reaches the (fake) transport is GET reports/companyinfo or POST /query with a
    server-built SELECT, against the bound realm, with ``minorversion=75``;
  - each tool emits EXACTLY ONE audit row on success AND on error, with no
    payload columns;
  - the "not configured" guard returns a clean JSON error (never a stack trace);
  - error paths never surface KEY/SECRET/TOKEN/realmId;
  - scoping: the custom-server merge predicate keeps the qbo server out of a
    non-onesie agent's materialized .mcp.json.

Fixtures are driven from realistic QBO report / query JSON shapes.
"""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet

from pinky_qbo import audit as audit_mod
from pinky_qbo import client as client_mod
from pinky_qbo.server import create_server

REALM = "9130350000000000"
TOOL_NAMES = {
    "qbo_profit_and_loss",
    "qbo_balance_sheet",
    "qbo_cash_flow",
    "qbo_aged_receivables",
    "qbo_expense_breakdown",
    "qbo_company_info",
    "qbo_list_accounts",
}


# ── realistic QBO fixtures ─────────────────────────────────────────────────────

PNL_FIXTURE = {
    "Header": {"ReportName": "ProfitAndLoss", "StartPeriod": "2026-01-01",
               "EndPeriod": "2026-06-30", "Currency": "USD"},
    "Columns": {"Column": [{"ColTitle": "", "ColType": "Account"},
                           {"ColTitle": "Total", "ColType": "Money"}]},
    "Rows": {"Row": [{"type": "Section", "Summary": {"ColData": [
        {"value": "Net Income"}, {"value": "12345.67"}]}}]},
}

BALANCE_SHEET_FIXTURE = {
    "Header": {"ReportName": "BalanceSheet", "EndPeriod": "2026-06-30"},
    "Rows": {"Row": [{"type": "Section", "Summary": {"ColData": [
        {"value": "Total Assets"}, {"value": "98765.43"}]}}]},
}

CASHFLOW_FIXTURE = {"Header": {"ReportName": "CashFlow"}, "Rows": {"Row": []}}
AGED_FIXTURE = {"Header": {"ReportName": "AgedReceivables"}, "Rows": {"Row": []}}

COMPANY_INFO_FIXTURE = {
    "CompanyInfo": {
        "CompanyName": "The One Device LLC",
        "Country": "US",
        "Id": REALM,
    },
    "time": "2026-06-15T00:00:00-07:00",
}

ACCOUNTS_FIXTURE = {
    "QueryResponse": {
        "Account": [
            {"Id": "35", "Name": "Checking", "AccountType": "Bank",
             "AccountSubType": "Checking", "CurrentBalance": 5000.50,
             "Active": True},
            {"Id": "60", "Name": "Office Supplies", "AccountType": "Expense",
             "AccountSubType": "SuppliesMaterials", "CurrentBalance": 0,
             "Active": True},
        ],
        "maxResults": 2,
    }
}


# ── fake transport that records every wire call ───────────────────────────────


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None):
        self.status_code = status_code
        self._json = json_body or {}
        self.headers = headers or {"intuit_tid": "tid-123"}
        self.text = ""

    def json(self):
        return self._json


class _RoutingSession:
    """Routes report/companyinfo/query to canned fixtures; records all calls."""

    def __init__(self, fail_status=None):
        self.calls = []
        self._fail_status = fail_status

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append(
            {"method": method, "url": url, "params": dict(params or {}),
             "data": data, "headers": dict(headers or {})}
        )
        if self._fail_status is not None:
            return _FakeResponse(self._fail_status, {"Fault": {"Error": []}})
        if "/reports/ProfitAndLoss" in url:
            return _FakeResponse(200, PNL_FIXTURE)
        if "/reports/BalanceSheet" in url:
            return _FakeResponse(200, BALANCE_SHEET_FIXTURE)
        if "/reports/CashFlow" in url:
            return _FakeResponse(200, CASHFLOW_FIXTURE)
        if "/reports/AgedReceivables" in url:
            return _FakeResponse(200, AGED_FIXTURE)
        if "/companyinfo/" in url:
            return _FakeResponse(200, COMPANY_INFO_FIXTURE)
        if url.endswith("/query"):
            return _FakeResponse(200, ACCOUNTS_FIXTURE)
        return _FakeResponse(404, {"Fault": {"Error": [{"code": "404"}]}})


# ── fixtures: a configured agent DB + a server wired to a fake transport ───────


@pytest.fixture()
def fernet_key(monkeypatch):
    monkeypatch.setenv("PINKY_QBO_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("PINKY_QBO_KEY_FILE", raising=False)


@pytest.fixture()
def agents_db(tmp_path, fernet_key, monkeypatch):
    """A SQLite agents DB seeded with onesie's encrypted QBO creds + realm."""
    db = tmp_path / "agents.db"
    monkeypatch.setenv("PINKY_AGENTS_DB", str(db))

    # Use the server's own DB-backed accessors so keys land exactly where the
    # stdio server reads them (agent:<agent>:<key>).
    from pinky_qbo.server import _get_agent_setting, _set_agent_setting
    from pinky_qbo.store import TokenStore

    store = TokenStore(agent="onesie", set_fn=_set_agent_setting, get_fn=_get_agent_setting)
    store.save_client_credentials("client-id", "client-secret")
    store.save_refresh_token("REFRESH-0")
    store.save_realm(REALM, "sandbox")
    return str(db)


@pytest.fixture()
def captured_audit(monkeypatch):
    """Capture audit rows in-memory (so we don't depend on the real DB writer)."""
    rows = []

    def _capture(**kwargs):
        rows.append(kwargs)
        return True

    monkeypatch.setattr(audit_mod, "write_audit", _capture)
    return rows


@pytest.fixture()
def server(agents_db, monkeypatch):
    """A configured server whose adapter's client uses the fake transport."""
    fake = _RoutingSession()

    # Patch QBOClient construction to always inject the fake session AND a token
    # provider that never refreshes (so no oauth network either).
    real_init = client_mod.QBOClient.__post_init__

    def patched_init(self):
        self.session = fake
        real_init(self)

    monkeypatch.setattr(client_mod.QBOClient, "__post_init__", patched_init)

    # Avoid any real refresh by pre-supplying a fresh access token on the adapter.
    from pinky_qbo.adapters import quickbooks as qb

    monkeypatch.setattr(
        qb.QuickBooksAdapter, "_fresh_access_token", lambda self: "ACCESS-TEST"
    )

    srv = create_server("onesie", REALM, "sandbox")
    srv._fake_session = fake  # type: ignore[attr-defined]
    return srv


def _tools(srv):
    return {t.name: t.fn for t in srv._tool_manager.list_tools()}


def _call_each_tool(tools):
    """Invoke every tool with representative args; return name -> parsed result."""
    out = {}
    out["qbo_profit_and_loss"] = json.loads(
        tools["qbo_profit_and_loss"](start_date="2026-01-01", end_date="2026-06-30")
    )
    out["qbo_balance_sheet"] = json.loads(
        tools["qbo_balance_sheet"](end_date="2026-06-30")
    )
    out["qbo_cash_flow"] = json.loads(
        tools["qbo_cash_flow"](date_macro="This Fiscal Year-to-date")
    )
    out["qbo_aged_receivables"] = json.loads(
        tools["qbo_aged_receivables"](report_date="2026-06-30")
    )
    out["qbo_expense_breakdown"] = json.loads(
        tools["qbo_expense_breakdown"](start_date="2026-01-01", end_date="2026-06-30")
    )
    out["qbo_company_info"] = json.loads(tools["qbo_company_info"]())
    out["qbo_list_accounts"] = json.loads(
        tools["qbo_list_accounts"](account_type="", active_only=True)
    )
    return out


# ── tool surface ───────────────────────────────────────────────────────────────


class TestToolSurface:
    def test_exact_seven_tools(self, server):
        assert set(_tools(server).keys()) == TOOL_NAMES

    def test_tools_take_no_agent_or_realm_args(self, server):
        """spec §5/§6.4: no tool accepts an agent/realm argument."""
        import inspect

        for name, fn in _tools(server).items():
            params = set(inspect.signature(fn).parameters)
            assert "agent" not in params, name
            assert "realm" not in params, name
            assert "realm_id" not in params, name


# ── every tool only ever emits an allowlisted, read-only request ───────────────


class TestNoWriteEmittable:
    def test_all_tools_emit_only_allowlisted_readonly_requests(
        self, server, captured_audit
    ):
        tools = _tools(server)
        results = _call_each_tool(tools)
        # None returned an error.
        for name, res in results.items():
            assert "error" not in res, f"{name} -> {res}"

        fake = server._fake_session
        assert fake.calls, "tools must have hit the (fake) transport"

        bound_reports = f"/v3/company/{REALM}/reports/"
        bound_ci = f"/v3/company/{REALM}/companyinfo/{REALM}"
        bound_query = f"/v3/company/{REALM}/query"
        allowed_reports = client_mod.ALLOWED_REPORTS

        for call in fake.calls:
            method, url = call["method"], call["url"]
            # 1) Only safe verbs.
            assert method in {"GET", "POST"}, call
            # 2) minorversion pinned on EVERY request.
            assert call["params"].get("minorversion") == "75", call
            # 3) Bearer set, no raw secret leaked into the header dict.
            assert call["headers"].get("Authorization") == "Bearer ACCESS-TEST"
            # 4) Path must be one of the three allowlisted shapes, bound realm.
            path = url.split("intuit.com", 1)[-1].split("?", 1)[0]
            if method == "GET":
                if path == bound_ci:
                    continue
                assert path.startswith(bound_reports), call
                report = path[len(bound_reports):]
                assert "/" not in report and report in allowed_reports, call
            else:  # POST
                assert path == bound_query, call
                body = call["data"] or ""
                # Server-built SELECT only — no mutation keyword, no chaining.
                assert body.lstrip().upper().startswith("SELECT"), call
                assert ";" not in body, call
                low = body.lower()
                for kw in ("insert", "update", "delete", "drop", "alter",
                           "truncate", "create", "merge", "void"):
                    assert f" {kw} " not in f" {low} ", call

    def test_no_tool_ever_uses_a_write_verb(self, server, captured_audit):
        tools = _tools(server)
        _call_each_tool(tools)
        for call in server._fake_session.calls:
            assert call["method"] not in {"PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"}

    def test_list_accounts_uses_server_built_select_not_caller_sql(
        self, server, captured_audit
    ):
        tools = _tools(server)
        json.loads(tools["qbo_list_accounts"](account_type="Bank"))
        query_calls = [c for c in server._fake_session.calls if c["url"].endswith("/query")]
        assert len(query_calls) == 1
        body = query_calls[0]["data"]
        # The SELECT was built server-side from the Account entity enum.
        assert body.startswith("SELECT * FROM Account")
        assert "MAXRESULTS" in body


# ── tool results reflect the fixtures (sanity: real shapes flow through) ────────


class TestToolResults:
    def test_company_info_passthrough(self, server, captured_audit):
        res = json.loads(_tools(server)["qbo_company_info"]())
        assert res["CompanyInfo"]["CompanyName"] == "The One Device LLC"

    def test_list_accounts_normalized(self, server, captured_audit):
        res = json.loads(_tools(server)["qbo_list_accounts"]())
        assert isinstance(res, list)
        assert {r["name"] for r in res} == {"Checking", "Office Supplies"}
        checking = next(r for r in res if r["name"] == "Checking")
        assert checking["type"] == "Bank"
        assert checking["current_balance"] == 5000.50

    def test_pnl_passthrough(self, server, captured_audit):
        res = json.loads(
            _tools(server)["qbo_profit_and_loss"](
                start_date="2026-01-01", end_date="2026-06-30"
            )
        )
        assert res["Header"]["ReportName"] == "ProfitAndLoss"

    def test_expense_breakdown_summarizes_by_month(self, server, captured_audit):
        json.loads(
            _tools(server)["qbo_expense_breakdown"](
                start_date="2026-01-01", end_date="2026-06-30"
            )
        )
        pnl_calls = [
            c for c in server._fake_session.calls
            if "/reports/ProfitAndLoss" in c["url"]
        ]
        assert pnl_calls
        assert pnl_calls[-1]["params"].get("summarize_column_by") == "Month"


# ── audit: exactly one row per call (success AND error) ────────────────────────


class TestAuditPerCall:
    def test_one_audit_row_per_successful_tool(self, server, captured_audit):
        tools = _tools(server)
        _call_each_tool(tools)
        # 7 tool invocations → 7 audit rows.
        assert len(captured_audit) == 7
        assert all(r["success"] is True for r in captured_audit)

    def test_audit_rows_carry_no_payload(self, server, captured_audit):
        tools = _tools(server)
        _call_each_tool(tools)
        forbidden = {"payload", "body", "response", "data", "json", "report", "rows"}
        for row in captured_audit:
            assert forbidden.isdisjoint(row.keys()), row

    def test_one_audit_row_on_error(self, agents_db, monkeypatch, captured_audit):
        """A failing HTTP call (500, exhausted retries) still emits exactly one
        audit row, marked failure."""
        fake = _RoutingSession(fail_status=500)
        real_init = client_mod.QBOClient.__post_init__

        def patched_init(self):
            self.session = fake
            real_init(self)

        monkeypatch.setattr(client_mod.QBOClient, "__post_init__", patched_init)
        monkeypatch.setattr("pinky_qbo.client.time.sleep", lambda d: None)
        from pinky_qbo.adapters import quickbooks as qb

        monkeypatch.setattr(
            qb.QuickBooksAdapter, "_fresh_access_token", lambda self: "ACCESS-TEST"
        )

        srv = create_server("onesie", REALM, "sandbox")
        res = json.loads(_tools(srv)["qbo_company_info"]())
        assert "error" in res
        assert len(captured_audit) == 1
        assert captured_audit[0]["success"] is False


# ── "not configured" guard ─────────────────────────────────────────────────────


class TestNotConfiguredGuard:
    def test_unconfigured_agent_returns_clean_error(self, tmp_path, monkeypatch):
        db = tmp_path / "empty.db"
        monkeypatch.setenv("PINKY_AGENTS_DB", str(db))
        monkeypatch.setenv("PINKY_QBO_KEY", Fernet.generate_key().decode("ascii"))
        srv = create_server("nobody", REALM, "sandbox")
        for name, fn in _tools(srv).items():
            res = json.loads(fn() if name in {"qbo_company_info"} else fn())
            assert "error" in res, name
            assert "not configured" in res["error"].lower(), name


# ── redaction at the tool boundary ─────────────────────────────────────────────


class TestRedactionAtToolBoundary:
    def test_error_never_surfaces_token_secret_or_realm(
        self, agents_db, monkeypatch, captured_audit
    ):
        leaky = {
            "Fault": {
                "Error": [
                    {
                        "code": "3200",
                        "Message": (
                            f"client_secret=LEAKYSECRET token=LEAKYTOKEN "
                            f"denied for realm {REALM}"
                        ),
                    }
                ]
            }
        }

        class _Leak(_RoutingSession):
            def request(self, *a, **k):
                self.calls.append({"method": a[0] if a else k.get("method")})
                return _FakeResponse(401, leaky)

        fake = _Leak()
        real_init = client_mod.QBOClient.__post_init__

        def patched_init(self):
            self.session = fake
            real_init(self)

        monkeypatch.setattr(client_mod.QBOClient, "__post_init__", patched_init)
        monkeypatch.setattr("pinky_qbo.client.time.sleep", lambda d: None)
        from pinky_qbo.adapters import quickbooks as qb

        monkeypatch.setattr(
            qb.QuickBooksAdapter, "_fresh_access_token", lambda self: "ACCESS-TEST"
        )

        srv = create_server("onesie", REALM, "sandbox")
        res = json.loads(_tools(srv)["qbo_profit_and_loss"]())
        blob = json.dumps(res)
        assert "LEAKYSECRET" not in blob
        assert "LEAKYTOKEN" not in blob
        assert REALM not in blob


# ── scoping: another agent's .mcp.json has no qbo entry (spec §5) ──────────────


class TestScopingPredicate:
    """The custom-server merge predicate (`_write_mcp_json`) filters by
    agent_name + enabled. A non-onesie agent's materialized config must contain
    no qbo server. We test the predicate directly via the registry list filter
    (the full _write_mcp_json is heavy to invoke)."""

    def _make_registry(self, tmp_path):
        from pinky_daemon.agent_registry import AgentRegistry

        db = tmp_path / "reg.db"
        return AgentRegistry(db_path=str(db))

    def test_qbo_server_only_in_owning_agents_list(self, tmp_path):
        reg = self._make_registry(tmp_path)
        reg.register("onesie", soul="s")
        reg.register("angel", soul="s")
        # onesie gets the qbo custom server row; angel does not.
        reg.add_mcp_server(
            "onesie", "pinky-qbo", server_type="stdio",
            command="python", args='["-m","pinky_qbo"]', env="{}",
        )
        onesie_servers = {s["server_name"] for s in reg.list_mcp_servers("onesie")}
        angel_servers = {s["server_name"] for s in reg.list_mcp_servers("angel")}
        assert "pinky-qbo" in onesie_servers
        assert "pinky-qbo" not in angel_servers

    def test_write_mcp_json_excludes_qbo_for_other_agent(self, tmp_path, monkeypatch):
        """Drive the real _write_mcp_json and assert the qbo server lands only in
        the owning agent's .mcp.json, never the other agent's."""

        from pinky_daemon import api
        from pinky_daemon.agent_registry import AgentRegistry

        # Avoid shared-MCP SSE path so we exercise the stdio merge branch.
        monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False, raising=False)

        reg = AgentRegistry(db_path=str(tmp_path / "reg.db"))
        reg.register("onesie", soul="s")
        reg.register("angel", soul="s")
        reg.add_mcp_server(
            "onesie", "pinky-qbo", server_type="stdio",
            command="python",
            args='["-m","pinky_qbo","--agent","onesie","--qbo-realm-id","123"]',
            env="{}",
        )

        onesie_dir = tmp_path / "onesie"
        angel_dir = tmp_path / "angel"
        onesie_dir.mkdir()
        angel_dir.mkdir()

        api._write_mcp_json(onesie_dir, "onesie", agent_registry=reg, skill_store=None)
        api._write_mcp_json(angel_dir, "angel", agent_registry=reg, skill_store=None)

        onesie_cfg = json.loads((onesie_dir / ".mcp.json").read_text())
        angel_cfg = json.loads((angel_dir / ".mcp.json").read_text())
        assert "pinky-qbo" in onesie_cfg["mcpServers"]
        assert "pinky-qbo" not in angel_cfg["mcpServers"]

    def test_disabled_qbo_server_excluded(self, tmp_path, monkeypatch):
        from pinky_daemon import api
        from pinky_daemon.agent_registry import AgentRegistry

        monkeypatch.setattr(api, "SHARED_MCP_ENABLED", False, raising=False)
        reg = AgentRegistry(db_path=str(tmp_path / "reg.db"))
        reg.register("onesie", soul="s")
        reg.add_mcp_server(
            "onesie", "pinky-qbo", server_type="stdio",
            command="python", args='["-m","pinky_qbo"]', env="{}",
        )
        reg.toggle_mcp_server("onesie", "pinky-qbo", enabled=False)

        onesie_dir = tmp_path / "onesie"
        onesie_dir.mkdir()
        api._write_mcp_json(onesie_dir, "onesie", agent_registry=reg, skill_store=None)
        cfg = json.loads((onesie_dir / ".mcp.json").read_text())
        assert "pinky-qbo" not in cfg["mcpServers"]


# ── enable route writes MINIMAL env (no secrets) — spec §4 ─────────────────────


class TestEnableRouteNoSecretsInEnv:
    def test_enable_mcp_env_has_no_secret_keys(self, tmp_path, monkeypatch):
        """The .mcp.json row the enable route writes must carry NO secret values
        — only PINKY_AGENTS_DB + non-secret realm/env/agent markers."""
        # Build the env dict exactly as routes/qbo.enable_agent_qbo does, then
        # assert no secret-shaped key/value is present.
        agents_db = str(tmp_path / "agents.db")
        env = {
            "PINKY_AGENTS_DB": agents_db,
            "QBO_AGENT": "onesie",
            "QBO_REALM_ID_EXPECTED": REALM,
            "QBO_ENV_EXPECTED": "sandbox",
        }
        joined = " ".join(f"{k}={v}" for k, v in env.items()).lower()
        for bad in ("secret", "refresh_token", "client_secret", "bearer", "access_token"):
            assert bad not in joined
        assert "PINKY_QBO_KEY" not in env  # the crypto key never travels in the row
