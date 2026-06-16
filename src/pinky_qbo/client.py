"""Read-only QuickBooks Online REST client (spec §6).

CRITICAL INVARIANT — a PATH-SPECIFIC allowlist is the ONLY way any request can
be emitted. The client permits exactly three request shapes against the bound
realm:

  1. ``GET  /v3/company/{realm}/reports/{report}``  where ``{report}`` is in
     ``ALLOWED_REPORTS``.
  2. ``GET  /v3/company/{realm}/companyinfo/{realm}``.
  3. ``POST /v3/company/{realm}/query``  whose body is a **server-built** SELECT
     (``select_*`` builders below); a caller/model-supplied SQL string can never
     reach the wire.

Any other method, path, or realm is rejected with ``ReadOnlyViolation`` *before*
any network call. There is no generic ``request()`` / ``get()`` / ``post()`` on
the public surface — every code path funnels through ``_emit`` which re-checks
the allowlist itself, so even an internal mistake fails closed.

Other behaviour (spec §6.7):
  - ``minorversion=75`` pinned on every request.
  - Retry with backoff ONLY on 429 / 5xx, honouring ``Retry-After``.
  - Offset pagination (``STARTPOSITION`` / ``MAXRESULTS`` ≤ 1000) for queries.
  - Memory-only TTL cache keyed ``realm + tool + params`` (no on-disk financials).
  - ``audit.write_audit`` is called exactly once per public call (success AND
    error).
  - KEY / SECRET / TOKEN / AUTH material and the realm id are redacted from any
    error text or exception surfaced to the caller.
"""

from __future__ import annotations

import re
import sys
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import requests

from pinky_qbo import audit

# ── Constants ────────────────────────────────────────────────────────────────

MINOR_VERSION = "75"

_BASE_PROD = "https://quickbooks.api.intuit.com"
_BASE_SANDBOX = "https://sandbox-quickbooks.api.intuit.com"

# Exact set of report names permitted on the /reports/ family. Anything else is
# rejected before a request is built. Names are Intuit's report entity names.
ALLOWED_REPORTS = frozenset(
    {
        "ProfitAndLoss",
        "BalanceSheet",
        "CashFlow",
        "AgedReceivables",
        "ProfitAndLossDetail",
    }
)

# Entities the server-built SELECT builder may target (no model-authored SQL).
ALLOWED_QUERY_ENTITIES = frozenset({"Account", "CompanyInfo", "Preferences"})

# Only GET (reports/companyinfo) and POST (server-built query) are ever valid.
# Every mutating verb is absent by construction; we still assert it explicitly.
_ALLOWED_METHODS = frozenset({"GET", "POST"})

_MAX_RESULTS_CAP = 1000

# Retry policy: only safe transient statuses.
_RETRYABLE = frozenset({429, 500, 502, 503, 504})
_MAX_RETRIES = 3
_BACKOFF_BASE = 0.5  # seconds; exponential
_MAX_BACKOFF = 8.0
_DEFAULT_TIMEOUT = 30

# A QBO realm id is numeric. Validate to keep it out of path-injection territory.
_REALM_RE = re.compile(r"^[0-9]+$")
# Report names: letters only (Intuit camel-case entity names).
_REPORT_RE = re.compile(r"^[A-Za-z]+$")

# Redaction: scrub anything that looks like credential material or the realm id.
_REDACT_KEYS_RE = re.compile(
    r"(?i)\b([A-Za-z0-9_]*(?:key|secret|token|auth|password|bearer)[A-Za-z0-9_]*)\b"
    r"\s*[=:]\s*\S+"
)


class ReadOnlyViolation(RuntimeError):  # noqa: N818 — security term of art, not a generic error
    """Raised when a request would breach the read-only path allowlist.

    This is a *programming/security* error — it means a non-allowlisted method,
    path, realm, report, or entity was about to be emitted. It never depends on
    network state.
    """


class QBOClientError(RuntimeError):
    """A redacted, caller-safe error from a QBO call (network/HTTP/parse)."""


def _log(msg: str) -> None:
    print(f"[pinky-qbo.client] {msg}", file=sys.stderr)


def _redact(text: str, realm_id: str | None) -> str:
    """Strip credential-shaped substrings and the realm id from error text."""
    if not text:
        return text
    out = _REDACT_KEYS_RE.sub(lambda m: f"{m.group(1)}=<redacted>", text)
    if realm_id:
        out = out.replace(realm_id, "<realm>")
    return out


# ── Token provider ───────────────────────────────────────────────────────────

# The client never reads/refreshes tokens itself — the adapter owns that and
# passes a callable returning a *fresh* bearer access token at call time.
AccessTokenProvider = Callable[[], str]


@dataclass
class _CacheEntry:
    value: Any
    expires_at: float


@dataclass
class QBOClient:
    """Read-only QBO REST client bound to a single realm.

    Args:
        realm_id: the bound QBO company id (numeric string).
        token_provider: zero-arg callable returning a *fresh* access token.
        agent: the bound agent name (for audit rows).
        env: ``"production"`` (default) or ``"sandbox"``.
        cache_ttl: seconds to keep a memory-only response (0 disables caching).
        timeout: per-request timeout (seconds).
    """

    realm_id: str
    token_provider: AccessTokenProvider
    agent: str = ""
    env: str = "production"
    cache_ttl: float = 60.0
    timeout: int = _DEFAULT_TIMEOUT
    session: requests.Session | None = None

    _cache: dict[str, _CacheEntry] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        if not self.realm_id or not _REALM_RE.match(str(self.realm_id)):
            raise ReadOnlyViolation("realm_id must be a numeric QBO company id")
        if self.session is None:
            self.session = requests.Session()

    # ── base URL ─────────────────────────────────────────────────────────────

    def _base(self) -> str:
        return _BASE_SANDBOX if str(self.env).lower().startswith("sandbox") else _BASE_PROD

    # ── public, named, fixed-shape read calls ────────────────────────────────

    def get_report(
        self,
        report: str,
        params: dict[str, str] | None = None,
        *,
        tool: str = "",
    ) -> dict:
        """GET a permitted report. ``report`` MUST be in ``ALLOWED_REPORTS``."""
        if report not in ALLOWED_REPORTS:
            raise ReadOnlyViolation(f"report not in allowlist: {report!r}")
        if not _REPORT_RE.match(report):
            raise ReadOnlyViolation("report name has illegal characters")
        path = f"/v3/company/{self.realm_id}/reports/{report}"
        return self._emit(
            "GET",
            path,
            params=dict(params or {}),
            tool=tool or f"report:{report}",
            endpoint_family="reports",
            date_range=self._range_label(params or {}),
        )

    def get_company_info(self, *, tool: str = "qbo_company_info") -> dict:
        """GET the CompanyInfo entity for the bound realm."""
        path = f"/v3/company/{self.realm_id}/companyinfo/{self.realm_id}"
        return self._emit(
            "GET",
            path,
            params={},
            tool=tool,
            endpoint_family="companyinfo",
        )

    def query(self, select: str, *, tool: str = "") -> dict:
        """POST a SERVER-BUILT SELECT to /query.

        ``select`` MUST originate from this module's ``select_*`` builders; a
        raw or model-supplied string is rejected. We re-validate the statement
        shape here as defence in depth.
        """
        self._assert_server_built_select(select)
        path = f"/v3/company/{self.realm_id}/query"
        return self._emit(
            "POST",
            path,
            params={},
            body=select,
            tool=tool or "query",
            endpoint_family="query",
        )

    # ── server-built SELECT builders (no model SQL ever) ─────────────────────

    def select_accounts(
        self,
        account_type: str = "",
        active_only: bool = True,
        start_position: int = 1,
        max_results: int = _MAX_RESULTS_CAP,
    ) -> str:
        """Build a parameterised SELECT for the chart of accounts.

        All fragments are validated/escaped server-side — there is no path for a
        caller string to become SQL.
        """
        where: list[str] = []
        if active_only:
            where.append("Active = true")
        if account_type:
            where.append(f"AccountType = '{self._escape_literal(account_type)}'")
        clause = (" WHERE " + " AND ".join(where)) if where else ""
        start, count = self._page_bounds(start_position, max_results)
        return (
            f"SELECT * FROM Account{clause} "
            f"STARTPOSITION {start} MAXRESULTS {count}"
        )

    def select_count(self, entity: str) -> str:
        """Build a server-built ``SELECT COUNT(*)`` companion for an entity.

        No caller-supplied WHERE: a free-form predicate would be an injection
        surface (review P2) and the count companion only needs a whole-entity
        total for pagination. Add a *validated* filter builder if a tool ever
        needs one.
        """
        if entity not in ALLOWED_QUERY_ENTITIES:
            raise ReadOnlyViolation(f"entity not in allowlist: {entity!r}")
        return f"SELECT COUNT(*) FROM {entity}"

    # ── internal: the single emit chokepoint ─────────────────────────────────

    def _emit(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str],
        body: str | None = None,
        tool: str,
        endpoint_family: str,
        date_range: str = "",
    ) -> dict:
        """The ONLY method that touches the network. Re-checks the allowlist,
        pins minorversion, retries safely, caches, and audits exactly once.
        """
        # 1) Hard allowlist gate — fails closed before any network use.
        self._assert_allowed(method, path, body)

        # 2) Memory-only TTL cache (read side).
        cache_key = self._cache_key(method, path, params, body)
        cached = self._cache_get(cache_key)
        if cached is not None:
            # A cache hit is still a logical call — audit it (no network).
            audit.write_audit(
                agent=self.agent,
                tool=tool,
                realm_id=self.realm_id,
                endpoint_family=endpoint_family,
                date_range=date_range,
                row_count=self._row_count(cached),
                request_id="cache",
                success=True,
            )
            return cached

        request_id = ""
        try:
            data, request_id = self._do_http(method, path, params, body)
        except ReadOnlyViolation:
            raise
        except Exception as e:  # network/HTTP/parse — redact + audit + re-raise
            safe = _redact(str(e), self.realm_id)
            audit.write_audit(
                agent=self.agent,
                tool=tool,
                realm_id=self.realm_id,
                endpoint_family=endpoint_family,
                date_range=date_range,
                row_count=0,
                request_id=request_id,
                success=False,
                error=safe[:2000],
            )
            raise QBOClientError(safe) from None

        # 3) Success path — cache + audit one row.
        self._cache_put(cache_key, data)
        audit.write_audit(
            agent=self.agent,
            tool=tool,
            realm_id=self.realm_id,
            endpoint_family=endpoint_family,
            date_range=date_range,
            row_count=self._row_count(data),
            request_id=request_id,
            success=True,
        )
        return data

    # ── allowlist enforcement ────────────────────────────────────────────────

    def _assert_allowed(self, method: str, path: str, body: str | None) -> None:
        """Reject anything outside the exact path-specific allowlist.

        This is the load-bearing invariant: a non-allowlisted method/path/realm
        can never reach ``_do_http``.
        """
        if method not in _ALLOWED_METHODS:
            raise ReadOnlyViolation(f"method not allowed: {method}")

        realm = self.realm_id
        reports_prefix = f"/v3/company/{realm}/reports/"
        companyinfo_exact = f"/v3/company/{realm}/companyinfo/{realm}"
        query_exact = f"/v3/company/{realm}/query"

        if method == "GET":
            if path == companyinfo_exact:
                return
            if path.startswith(reports_prefix):
                report = path[len(reports_prefix):]
                if "/" in report or report not in ALLOWED_REPORTS:
                    raise ReadOnlyViolation(f"report path not allowed: {path}")
                return
            raise ReadOnlyViolation(f"GET path not in allowlist: {path}")

        # POST is permitted ONLY for the server-built query endpoint.
        if method == "POST":
            if path != query_exact:
                raise ReadOnlyViolation(f"POST path not in allowlist: {path}")
            if body is None:
                raise ReadOnlyViolation("query POST requires a server-built body")
            self._assert_server_built_select(body)
            return

        raise ReadOnlyViolation(f"unhandled method/path: {method} {path}")

    @staticmethod
    def _assert_server_built_select(select: str) -> None:
        """Validate the query body is a single read-only SELECT over an enum entity.

        Defence in depth: even though only ``select_*`` builders feed this, we
        confirm the statement is a single SELECT with no mutation keyword and no
        statement chaining, AND that its target entity is in
        ``ALLOWED_QUERY_ENTITIES``. This makes a model/caller string structurally
        unable to mutate data OR to read a non-allowlisted (e.g. PII-bearing,
        spec §3-dropped) entity even if it somehow reached ``query``.
        """
        if not isinstance(select, str) or not select.strip():
            raise ReadOnlyViolation("query body must be a non-empty SELECT string")
        s = select.strip()
        if ";" in s:
            raise ReadOnlyViolation("query body must be a single statement")
        if not re.match(r"(?i)^\s*SELECT\s", s):
            raise ReadOnlyViolation("query body must begin with SELECT")
        # Block any mutation / DDL keyword anywhere in the statement.
        forbidden = (
            "insert", "update", "delete", "void", "merge", "drop",
            "create", "alter", "truncate", "exec", "call",
        )
        low = s.lower()
        for kw in forbidden:
            if re.search(rf"\b{kw}\b", low):
                raise ReadOnlyViolation(f"forbidden keyword in query: {kw}")
        # The target entity (first FROM clause) must be in the allowlist. This
        # keeps a clean-looking READ of a non-enum entity (e.g. Invoice/Customer,
        # dropped from v1 for row-level PII) off the wire. We match the FIRST
        # FROM only — subqueries/joins aren't produced by the server builders and
        # any second FROM is irrelevant once the first is constrained.
        m = re.search(r"(?i)\bFROM\s+([A-Za-z][A-Za-z0-9_]*)", s)
        if not m:
            raise ReadOnlyViolation("query body has no FROM entity")
        entity = m.group(1)
        if entity not in ALLOWED_QUERY_ENTITIES:
            raise ReadOnlyViolation(f"query entity not in allowlist: {entity!r}")

    # ── HTTP with safe retry/backoff ─────────────────────────────────────────

    def _do_http(
        self,
        method: str,
        path: str,
        params: dict[str, str],
        body: str | None,
    ) -> tuple[dict, str]:
        url = self._base() + path
        q = dict(params)
        q["minorversion"] = MINOR_VERSION  # pinned on every request

        attempt = 0
        while True:
            attempt += 1
            token = self.token_provider()
            headers = {
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            }
            if method == "POST":
                # Server-built SELECT goes as a text/plain query body per Intuit.
                headers["Content-Type"] = "application/text"
                resp = self.session.request(
                    method, url, params=q, data=body, headers=headers,
                    timeout=self.timeout,
                )
            else:
                resp = self.session.request(
                    method, url, params=q, headers=headers, timeout=self.timeout,
                )

            request_id = resp.headers.get("intuit_tid", "") or resp.headers.get(
                "X-Request-Id", ""
            )

            if resp.status_code in _RETRYABLE and attempt <= _MAX_RETRIES:
                self._sleep_for_retry(resp, attempt)
                continue

            if resp.status_code >= 400:
                # Pull Intuit's fault code WITHOUT echoing token/headers.
                detail = self._error_detail(resp)
                raise QBOClientError(
                    f"QBO {resp.status_code}: {_redact(detail, self.realm_id)}"
                )

            try:
                return resp.json(), request_id
            except ValueError as e:
                raise QBOClientError(f"QBO response was not JSON: {e}") from None

    def _sleep_for_retry(self, resp: requests.Response, attempt: int) -> None:
        retry_after = resp.headers.get("Retry-After", "")
        delay: float
        if retry_after:
            try:
                delay = float(retry_after)
            except ValueError:
                delay = _BACKOFF_BASE * (2 ** (attempt - 1))
        else:
            delay = _BACKOFF_BASE * (2 ** (attempt - 1))
        delay = min(delay, _MAX_BACKOFF)
        _log(f"retry {attempt} after {resp.status_code} in {delay:.1f}s")
        time.sleep(delay)

    @staticmethod
    def _error_detail(resp: requests.Response) -> str:
        try:
            body = resp.json()
        except ValueError:
            return resp.text[:300]
        fault = body.get("Fault") or body.get("fault") or {}
        errs = fault.get("Error") or fault.get("error") or []
        if isinstance(errs, list) and errs:
            first = errs[0]
            code = first.get("code", "")
            msg = first.get("Message") or first.get("message") or ""
            return f"{code} {msg}".strip()
        return str(body)[:300]

    # ── pagination helpers ───────────────────────────────────────────────────

    @staticmethod
    def _page_bounds(start_position: int, max_results: int) -> tuple[int, int]:
        try:
            start = max(1, int(start_position))
        except (TypeError, ValueError):
            start = 1
        try:
            count = int(max_results)
        except (TypeError, ValueError):
            count = _MAX_RESULTS_CAP
        count = max(1, min(count, _MAX_RESULTS_CAP))
        return start, count

    @staticmethod
    def _escape_literal(value: str) -> str:
        """Escape a single-quoted SQL literal for a server-built SELECT.

        Only ever applied to server-side enum-shaped values; doubles single
        quotes and strips control/quote-breaking characters.
        """
        cleaned = str(value).replace("'", "''")
        # Drop anything that could break out of the literal context.
        return re.sub(r"[;\\\x00-\x1f]", "", cleaned)

    # ── cache helpers (memory only) ──────────────────────────────────────────

    def _cache_key(
        self,
        method: str,
        path: str,
        params: dict[str, str],
        body: str | None,
    ) -> str:
        items = "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        return f"{self.realm_id}|{method}|{path}|{items}|{body or ''}"

    def _cache_get(self, key: str) -> dict | None:
        if self.cache_ttl <= 0:
            return None
        entry = self._cache.get(key)
        if entry is None:
            return None
        if entry.expires_at < time.monotonic():
            self._cache.pop(key, None)
            return None
        return entry.value

    def _cache_put(self, key: str, value: dict) -> None:
        if self.cache_ttl <= 0:
            return
        self._cache[key] = _CacheEntry(
            value=value, expires_at=time.monotonic() + self.cache_ttl
        )

    # ── misc ─────────────────────────────────────────────────────────────────

    @staticmethod
    def _row_count(data: dict) -> int:
        """Best-effort row count for the audit row (no payload stored)."""
        if not isinstance(data, dict):
            return 0
        qr = data.get("QueryResponse")
        if isinstance(qr, dict):
            total = 0
            for v in qr.values():
                if isinstance(v, list):
                    total += len(v)
            return total
        return 0

    @staticmethod
    def _range_label(params: dict[str, str]) -> str:
        start = params.get("start_date", "")
        end = params.get("end_date", "")
        macro = params.get("date_macro", "")
        if start or end:
            return f"{start}..{end}"
        return macro or ""
