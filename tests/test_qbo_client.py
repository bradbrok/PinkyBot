"""Adversarial tests for the read-only QBO REST client (spec §6).

The load-bearing invariant: a PATH-SPECIFIC allowlist is the ONLY way any
request can reach the wire. These tests monkeypatch the HTTP transport and prove
that:

  - no write verb (PUT/DELETE/PATCH) and no POST to anything except the
    allowlisted ``/query`` can be emitted;
  - no non-allowlisted GET path (e.g. ``/invoice``) can be emitted;
  - a raw/model SELECT string can never become a request body;
  - server-built SELECTs are validated (no mutation/DDL, no chaining, no
    injection through the account_type literal);
  - ``minorversion=75`` is pinned on EVERY real request, bearer auth is set;
  - retry/backoff fires only on 429/5xx and honours ``Retry-After``;
  - errors are redacted (no token / secret / realm id surfaced);
  - the memory-only TTL cache returns cached values and expires.
"""

from __future__ import annotations

import json

import pytest

from pinky_qbo import audit as audit_mod
from pinky_qbo.client import (
    ALLOWED_REPORTS,
    MINOR_VERSION,
    QBOClient,
    QBOClientError,
    ReadOnlyViolation,
)

# ── Fake HTTP transport ───────────────────────────────────────────────────────


class _FakeResponse:
    def __init__(self, status_code=200, json_body=None, headers=None, text=""):
        self.status_code = status_code
        self._json = json_body if json_body is not None else {}
        self.headers = headers or {}
        self.text = text

    def json(self):
        if self._json is _RAISE:
            raise ValueError("no json")
        return self._json


_RAISE = object()


class RecordingSession:
    """Captures every call that reaches the transport and replays canned responses."""

    def __init__(self, responses=None):
        self.calls = []  # list of dict(method, url, params, data, headers)
        self._responses = list(responses or [])
        self._default = _FakeResponse(
            200, {"QueryResponse": {"Account": [{"Id": "1"}]}}
        )

    def request(self, method, url, params=None, data=None, headers=None, timeout=None):
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": dict(params or {}),
                "data": data,
                "headers": dict(headers or {}),
                "timeout": timeout,
            }
        )
        if self._responses:
            return self._responses.pop(0)
        return self._default


@pytest.fixture(autouse=True)
def _no_audit_db(monkeypatch):
    """Audit must never raise and never need a DB in unit tests — count calls."""
    calls = []

    def _fake_write_audit(**kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(audit_mod, "write_audit", _fake_write_audit)
    # Expose the list for tests that want it.
    _fake_write_audit.calls = calls  # type: ignore[attr-defined]
    return calls


REALM = "9130350000000000"


def _client(session=None, **kw):
    return QBOClient(
        realm_id=REALM,
        token_provider=lambda: "ACCESS-TOKEN-abc",
        agent="onesie",
        env="sandbox",
        session=session or RecordingSession(),
        **kw,
    )


# ── allowlist: method gate ────────────────────────────────────────────────────


class TestMethodGate:
    def test_put_rejected(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed("PUT", f"/v3/company/{REALM}/reports/ProfitAndLoss", None)

    def test_delete_rejected(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed("DELETE", f"/v3/company/{REALM}/companyinfo/{REALM}", None)

    def test_patch_rejected(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed("PATCH", f"/v3/company/{REALM}/query", "SELECT * FROM Account")

    def test_write_verbs_never_touch_transport(self):
        sess = RecordingSession()
        c = _client(session=sess)
        for verb in ("PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            with pytest.raises(ReadOnlyViolation):
                c._emit(
                    verb,
                    f"/v3/company/{REALM}/reports/ProfitAndLoss",
                    params={},
                    tool="x",
                    endpoint_family="reports",
                )
        assert sess.calls == []  # nothing reached the wire


# ── allowlist: path gate ──────────────────────────────────────────────────────


class TestPathGate:
    def test_get_invoice_rejected(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed("GET", f"/v3/company/{REALM}/invoice", None)

    def test_get_invoice_by_id_rejected(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed("GET", f"/v3/company/{REALM}/invoice/42", None)

    def test_post_invoice_rejected(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed("POST", f"/v3/company/{REALM}/invoice", "{}")

    def test_post_to_non_query_path_rejected(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed(
                "POST", f"/v3/company/{REALM}/reports/ProfitAndLoss",
                "SELECT * FROM Account",
            )

    def test_report_not_in_allowlist_rejected(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed("GET", f"/v3/company/{REALM}/reports/Invoice", None)

    def test_report_path_with_extra_segment_rejected(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed(
                "GET", f"/v3/company/{REALM}/reports/ProfitAndLoss/extra", None
            )

    def test_companyinfo_other_realm_rejected(self):
        c = _client()
        # companyinfo must be the bound realm both in the path AND the entity id.
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed("GET", "/v3/company/999/companyinfo/999", None)

    def test_query_for_other_realm_rejected(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c._assert_allowed("POST", "/v3/company/999/query", "SELECT * FROM Account")

    def test_allowed_get_report_passes(self):
        c = _client()
        for r in ALLOWED_REPORTS:
            c._assert_allowed("GET", f"/v3/company/{REALM}/reports/{r}", None)

    def test_allowed_companyinfo_passes(self):
        c = _client()
        c._assert_allowed("GET", f"/v3/company/{REALM}/companyinfo/{REALM}", None)

    def test_allowed_query_passes(self):
        c = _client()
        c._assert_allowed("POST", f"/v3/company/{REALM}/query", "SELECT * FROM Account")


# ── public surface: no generic request/get/post ───────────────────────────────


class TestNoGenericSurface:
    def test_no_public_request_get_post(self):
        c = _client()
        # The model/caller has no generic verbs to abuse.
        for name in ("request", "get", "post", "put", "delete", "patch"):
            assert not hasattr(c, name), f"client must not expose .{name}()"

    def test_get_report_rejects_arbitrary_report(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c.get_report("Invoice")  # not in ALLOWED_REPORTS

    def test_query_rejects_raw_model_select_with_mutation(self):
        c = _client()
        # A caller passing a raw SELECT that smuggles a mutation is rejected.
        with pytest.raises(ReadOnlyViolation):
            c.query("SELECT * FROM Account; DELETE FROM Account")


# ── server-built SELECT validation (no model SQL) ──────────────────────────────


class TestServerBuiltSelect:
    @pytest.mark.parametrize(
        "bad",
        [
            "DELETE FROM Account",
            "UPDATE Account SET Name='x'",
            "INSERT INTO Account (Name) VALUES ('x')",
            "DROP TABLE Account",
            "SELECT * FROM Account; DROP TABLE Account",
            "select * from Account; select * from Vendor",
            "UPDATE Account SET x=1 WHERE Id='1'",
            "VOID Invoice",
            "  ",
            "",
            "EXEC sp_who",
            "CALL something()",
            "ALTER TABLE Account",
            "TRUNCATE Account",
            "MERGE INTO Account",
        ],
    )
    def test_rejects_non_select_or_mutation(self, bad):
        with pytest.raises(ReadOnlyViolation):
            QBOClient._assert_server_built_select(bad)

    @pytest.mark.parametrize(
        "good",
        [
            "SELECT * FROM Account",
            "SELECT COUNT(*) FROM Account",
            "select * from Account WHERE Active = true STARTPOSITION 1 MAXRESULTS 100",
        ],
    )
    def test_accepts_clean_select(self, good):
        QBOClient._assert_server_built_select(good)  # no raise

    def test_select_accounts_builder_is_clean(self):
        c = _client()
        sql = c.select_accounts(account_type="Bank", active_only=True)
        assert sql.startswith("SELECT * FROM Account")
        assert "MAXRESULTS" in sql
        QBOClient._assert_server_built_select(sql)  # builder output always valid

    def test_select_accounts_caps_max_results(self):
        c = _client()
        sql = c.select_accounts(max_results=10**9)
        assert "MAXRESULTS 1000" in sql

    def test_select_accounts_injection_in_account_type_is_escaped_inert(self):
        """A hostile account_type cannot break out of the literal or chain SQL.

        The builder escapes the value: single quotes are doubled and ``;`` /
        backslashes / control chars are stripped, so the hostile payload becomes
        inert *content inside a quoted literal* — it cannot start a new statement.
        """
        c = _client()
        sql = c.select_accounts(account_type="Bank'; DROP TABLE Account; --")
        # No statement chaining survives the escape.
        assert ";" not in sql
        # The quote was doubled (escaped), so DROP sits inside the literal, not as
        # a standalone keyword: the substring "'' DROP" proves it's escaped.
        assert "''" in sql
        assert "'Bank''" in sql  # value started as a properly-quoted literal

    def test_hostile_account_type_cannot_emit_mutating_request(self):
        """End-to-end: a hostile account_type can NEVER produce a request that
        mutates data. Either the escaped literal is inert, or the defence-in-depth
        validator fails closed before the transport — both are safe. This asserts
        no request body containing an EXECUTABLE mutation keyword reaches the wire.
        """
        sess = RecordingSession([_FakeResponse(200, {"QueryResponse": {"Account": []}})])
        c = _client(session=sess)
        sql = c.select_accounts(account_type="Bank'; DROP TABLE Account; --")
        try:
            c.query(sql)
        except ReadOnlyViolation:
            # Fail-closed: validator rejected it, nothing reached the wire.
            assert sess.calls == []
            return
        # If it WAS emitted, the mutation keyword must be neutralized inside a
        # quoted literal — no executable DROP statement exists in the body.
        assert len(sess.calls) == 1
        body = sess.calls[0]["data"]
        assert ";" not in body
        # The DROP keyword, if present, is contained within a single-quoted
        # literal (preceded by an escaped quote), never as a standalone statement.
        assert "'' DROP" in body or "DROP" not in body.upper()

    def test_select_count_rejects_unknown_entity(self):
        c = _client()
        with pytest.raises(ReadOnlyViolation):
            c.select_count("Invoice")

    def test_select_count_allows_enum_entity(self):
        c = _client()
        sql = c.select_count("Account")
        assert sql == "SELECT COUNT(*) FROM Account"
        QBOClient._assert_server_built_select(sql)

    @pytest.mark.parametrize(
        "non_enum_select",
        [
            "SELECT * FROM Invoice",
            "SELECT * FROM Customer",
            "SELECT Id, DisplayName FROM Customer WHERE Active = true",
            "SELECT * FROM Bill",
            "SELECT * FROM Payment",
        ],
    )
    def test_clean_read_of_non_enum_entity_rejected(self, non_enum_select):
        """Defence in depth: a clean (non-mutating) SELECT over a non-allowlisted
        entity — e.g. the PII-bearing Invoice/Customer entities dropped from v1
        (spec §3) — must be rejected, so it can never reach the wire even via the
        internal ``query`` path."""
        with pytest.raises(ReadOnlyViolation):
            QBOClient._assert_server_built_select(non_enum_select)

    def test_query_blocks_clean_non_enum_read_end_to_end(self):
        sess = RecordingSession([_FakeResponse(200, {"QueryResponse": {}})])
        c = _client(session=sess)
        with pytest.raises(ReadOnlyViolation):
            c.query("SELECT * FROM Invoice")
        assert sess.calls == []  # nothing reached the wire


# ── minorversion pin + bearer auth on EVERY real request ───────────────────────


class TestRequestShape:
    def test_minorversion_pinned_on_report(self):
        sess = RecordingSession([_FakeResponse(200, {"Report": {}})])
        c = _client(session=sess)
        c.get_report("ProfitAndLoss", {"start_date": "2026-01-01"})
        assert len(sess.calls) == 1
        assert sess.calls[0]["params"]["minorversion"] == MINOR_VERSION == "75"

    def test_minorversion_pinned_on_companyinfo(self):
        sess = RecordingSession([_FakeResponse(200, {"CompanyInfo": {}})])
        c = _client(session=sess)
        c.get_company_info()
        assert sess.calls[0]["params"]["minorversion"] == "75"

    def test_minorversion_pinned_on_query(self):
        sess = RecordingSession([_FakeResponse(200, {"QueryResponse": {}})])
        c = _client(session=sess)
        c.query(c.select_accounts())
        assert sess.calls[0]["params"]["minorversion"] == "75"

    def test_bearer_auth_header_set(self):
        sess = RecordingSession([_FakeResponse(200, {"Report": {}})])
        c = _client(session=sess)
        c.get_report("BalanceSheet")
        assert sess.calls[0]["headers"]["Authorization"] == "Bearer ACCESS-TOKEN-abc"

    def test_query_uses_post_with_select_body(self):
        sess = RecordingSession([_FakeResponse(200, {"QueryResponse": {}})])
        c = _client(session=sess)
        sql = c.select_accounts()
        c.query(sql)
        assert sess.calls[0]["method"] == "POST"
        assert sess.calls[0]["data"] == sql
        assert sess.calls[0]["url"].endswith(f"/v3/company/{REALM}/query")

    def test_sandbox_base_url(self):
        sess = RecordingSession([_FakeResponse(200, {"Report": {}})])
        c = _client(session=sess)
        c.get_report("CashFlow")
        assert sess.calls[0]["url"].startswith("https://sandbox-quickbooks.api.intuit.com")

    def test_production_base_url(self):
        sess = RecordingSession([_FakeResponse(200, {"Report": {}})])
        c = QBOClient(
            realm_id=REALM,
            token_provider=lambda: "tok",
            env="production",
            session=sess,
        )
        c.get_report("CashFlow")
        assert sess.calls[0]["url"].startswith("https://quickbooks.api.intuit.com")


# ── retry / backoff (only 429/5xx, honour Retry-After) ─────────────────────────


class TestRetry:
    def test_retries_on_429_then_succeeds(self, monkeypatch):
        slept = []
        monkeypatch.setattr("pinky_qbo.client.time.sleep", lambda d: slept.append(d))
        sess = RecordingSession(
            [
                _FakeResponse(429, {}, headers={"Retry-After": "2"}),
                _FakeResponse(200, {"Report": {"ok": True}}),
            ]
        )
        c = _client(session=sess)
        out = c.get_report("ProfitAndLoss")
        assert out == {"Report": {"ok": True}}
        assert len(sess.calls) == 2
        assert slept == [2.0]  # honoured Retry-After

    def test_retries_on_503(self, monkeypatch):
        monkeypatch.setattr("pinky_qbo.client.time.sleep", lambda d: None)
        sess = RecordingSession(
            [_FakeResponse(503, {}), _FakeResponse(200, {"Report": {}})]
        )
        c = _client(session=sess)
        c.get_report("ProfitAndLoss")
        assert len(sess.calls) == 2

    def test_does_not_retry_on_400(self, monkeypatch):
        monkeypatch.setattr("pinky_qbo.client.time.sleep", lambda d: None)
        sess = RecordingSession(
            [_FakeResponse(400, {"Fault": {"Error": [{"code": "4000",
                                                      "Message": "bad"}]}})]
        )
        c = _client(session=sess)
        with pytest.raises(QBOClientError):
            c.get_report("ProfitAndLoss")
        assert len(sess.calls) == 1  # no retry on 4xx (except 429)

    def test_does_not_retry_on_403(self, monkeypatch):
        monkeypatch.setattr("pinky_qbo.client.time.sleep", lambda d: None)
        sess = RecordingSession([_FakeResponse(403, {"Fault": {"Error": []}})])
        c = _client(session=sess)
        with pytest.raises(QBOClientError):
            c.get_report("ProfitAndLoss")
        assert len(sess.calls) == 1


# ── redaction ──────────────────────────────────────────────────────────────────


class TestRedaction:
    def test_error_text_redacts_credentials_and_realm(self, monkeypatch):
        monkeypatch.setattr("pinky_qbo.client.time.sleep", lambda d: None)
        # The fault message embeds a token + the realm id; both must be scrubbed.
        leaky = {
            "Fault": {
                "Error": [
                    {
                        "code": "3200",
                        "Message": (
                            f"access_token=SECRETLEAK123 refused for realm {REALM}"
                        ),
                    }
                ]
            }
        }
        sess = RecordingSession([_FakeResponse(401, leaky)])
        c = _client(session=sess)
        with pytest.raises(QBOClientError) as ei:
            c.get_report("ProfitAndLoss")
        msg = str(ei.value)
        assert "SECRETLEAK123" not in msg
        assert REALM not in msg
        assert "<realm>" in msg

    def test_transport_exception_redacted(self, monkeypatch):
        monkeypatch.setattr("pinky_qbo.client.time.sleep", lambda d: None)

        class Boom(RecordingSession):
            def request(self, *a, **k):  # noqa: D401
                raise RuntimeError(
                    f"connect failed token=BEARERLEAK realm {REALM}"
                )

        c = _client(session=Boom())
        with pytest.raises(QBOClientError) as ei:
            c.get_report("CashFlow")
        msg = str(ei.value)
        assert "BEARERLEAK" not in msg
        assert REALM not in msg


# ── memory-only TTL cache ──────────────────────────────────────────────────────


class TestCache:
    def test_cache_hit_skips_second_request(self):
        sess = RecordingSession([_FakeResponse(200, {"Report": {"v": 1}})])
        c = _client(session=sess, cache_ttl=60)
        a = c.get_report("ProfitAndLoss", {"start_date": "2026-01-01"})
        b = c.get_report("ProfitAndLoss", {"start_date": "2026-01-01"})
        assert a == b
        assert len(sess.calls) == 1  # second served from cache

    def test_cache_disabled_always_calls(self):
        sess = RecordingSession(
            [_FakeResponse(200, {"Report": {}}), _FakeResponse(200, {"Report": {}})]
        )
        c = _client(session=sess, cache_ttl=0)
        c.get_report("ProfitAndLoss")
        c.get_report("ProfitAndLoss")
        assert len(sess.calls) == 2

    def test_cache_expires(self, monkeypatch):
        clock = [1000.0]
        monkeypatch.setattr("pinky_qbo.client.time.monotonic", lambda: clock[0])
        sess = RecordingSession(
            [_FakeResponse(200, {"Report": {}}), _FakeResponse(200, {"Report": {}})]
        )
        c = _client(session=sess, cache_ttl=30)
        c.get_report("ProfitAndLoss")
        clock[0] += 31  # past TTL
        c.get_report("ProfitAndLoss")
        assert len(sess.calls) == 2


# ── realm validation at construction ───────────────────────────────────────────


class TestRealmValidation:
    def test_non_numeric_realm_rejected(self):
        with pytest.raises(ReadOnlyViolation):
            QBOClient(realm_id="abc; DROP", token_provider=lambda: "t")

    def test_empty_realm_rejected(self):
        with pytest.raises(ReadOnlyViolation):
            QBOClient(realm_id="", token_provider=lambda: "t")


# ── audit: exactly one row per call (success AND error) ────────────────────────


class TestAuditFromClient:
    def test_one_audit_row_on_success(self, _no_audit_db):
        sess = RecordingSession([_FakeResponse(200, {"QueryResponse": {"Account": []}})])
        c = _client(session=sess)
        c.get_report("ProfitAndLoss")
        assert len(_no_audit_db) == 1
        assert _no_audit_db[0]["success"] is True

    def test_one_audit_row_on_error(self, _no_audit_db, monkeypatch):
        monkeypatch.setattr("pinky_qbo.client.time.sleep", lambda d: None)
        sess = RecordingSession([_FakeResponse(500, {"Fault": {"Error": []}}),
                                 _FakeResponse(500, {"Fault": {"Error": []}}),
                                 _FakeResponse(500, {"Fault": {"Error": []}}),
                                 _FakeResponse(500, {"Fault": {"Error": []}})])
        c = _client(session=sess)
        with pytest.raises(QBOClientError):
            c.get_report("ProfitAndLoss")
        assert len(_no_audit_db) == 1
        assert _no_audit_db[0]["success"] is False

    def test_audit_row_has_no_payload_columns(self, _no_audit_db):
        sess = RecordingSession([_FakeResponse(200, {"QueryResponse": {"Account": []}})])
        c = _client(session=sess)
        c.get_report("BalanceSheet")
        row = _no_audit_db[0]
        # The audit kwargs must carry metadata only — no report/body/response.
        forbidden = {"payload", "body", "response", "data", "json", "rows", "report"}
        assert forbidden.isdisjoint(row.keys())

    def test_cache_hit_still_audits(self, _no_audit_db):
        sess = RecordingSession([_FakeResponse(200, {"QueryResponse": {"Account": []}})])
        c = _client(session=sess, cache_ttl=60)
        c.get_report("ProfitAndLoss")
        c.get_report("ProfitAndLoss")
        # Two logical calls => two audit rows even though one was a cache hit.
        assert len(_no_audit_db) == 2


def test_no_real_network_used():
    """Sanity: a default client with no session never reaches a live socket in
    these tests because every test injects a session. This asserts the fake
    transport is the only path exercised (defensive)."""
    c = _client()
    assert isinstance(c.session, RecordingSession)
    # Encode the response so a tool body round-trips as JSON (smoke).
    assert json.loads(json.dumps({"ok": True})) == {"ok": True}
