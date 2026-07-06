"""Read-only QuickBooks Online REST client.

Security invariants:
  - Only GETs to allowlisted report/companyinfo paths.
  - Only POSTs to /query with a server-built SELECT (never model-authored SQL).
  - Method + path checked BEFORE any network call; PermissionError on mismatch.
  - Realm bound at construction; path realm must match.
  - minorversion=75 pinned on every call.
  - No mutation code exists in this module.
"""

from __future__ import annotations

import re
import sys
import uuid
from collections.abc import Callable
from typing import Any

# ── Allowlists ────────────────────────────────────────────────────────────────

ALLOWED_REPORTS: frozenset[str] = frozenset({
    "ProfitAndLoss",
    "BalanceSheet",
    "CashFlow",
    "AgedReceivables",
})

ALLOWED_ENTITIES: frozenset[str] = frozenset({
    "Account",
    "CompanyInfo",
})

_BASE_URLS = {
    "sandbox": "https://sandbox-quickbooks.api.intuit.com",
    "production": "https://quickbooks.api.intuit.com",
}

_MINOR_VERSION = "75"

# Pre-compiled path patterns for the allowlist check.
# Group 1 captures realm id; group 2 (where present) captures the report name.
_RE_REPORT = re.compile(
    r"^/v3/company/([^/]+)/reports/([^/?]+)$"
)
_RE_COMPANYINFO = re.compile(
    r"^/v3/company/([^/]+)/companyinfo/([^/?]+)$"
)
_RE_QUERY = re.compile(
    r"^/v3/company/([^/]+)/query$"
)


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class QboClient:
    """Read-only QBO REST client with a hard path/method allowlist."""

    def __init__(
        self,
        realm_id: str,
        env: str,
        token_provider: Callable[[], str],
        audit_hook: Callable[[str, str, str, str, str, int, str], None] | None = None,
    ) -> None:
        """Construct the client.

        Args:
            realm_id: The QBO company realm ID (bound at startup; immutable).
            env: "sandbox" or "production".
            token_provider: Zero-argument callable that returns a valid access token.
            audit_hook: Optional callable(tool, endpoint_family, date_range, request_id,
                        status, row_count, realm_hash) called after each request.
        """
        if env not in _BASE_URLS:
            raise ValueError(f"env must be 'sandbox' or 'production', got '{env}'")
        self._realm_id = realm_id
        self._base_url = _BASE_URLS[env]
        self._token_provider = token_provider
        self._audit_hook = audit_hook

    # ── Allowlist enforcement ─────────────────────────────────────────────────

    def _check_allowlist(self, method: str, path: str, body: Any = None) -> None:
        """Enforce the read-only path + method allowlist.

        Raises PermissionError if the request is not in the allowlist.
        Called BEFORE any network I/O.
        """
        method = method.upper()

        # Only GET and POST (query only) are ever permitted.
        if method not in ("GET", "POST"):
            raise PermissionError(
                f"QBO client is read-only. Method '{method}' is not permitted."
            )

        # Strip query string for path matching.
        path_only = path.split("?")[0]

        # Check GET /reports/{report}
        m = _RE_REPORT.match(path_only)
        if m:
            if method != "GET":
                raise PermissionError(
                    f"Report path requires GET, got '{method}'."
                )
            path_realm = m.group(1)
            report_name = m.group(2)
            if path_realm != self._realm_id:
                raise PermissionError(
                    f"Realm mismatch: path has '{path_realm}', bound realm is '{self._realm_id}'."
                )
            if report_name not in ALLOWED_REPORTS:
                raise PermissionError(
                    f"Report '{report_name}' is not in the allowlist {sorted(ALLOWED_REPORTS)}."
                )
            return

        # Check GET /companyinfo/{realm}
        m = _RE_COMPANYINFO.match(path_only)
        if m:
            if method != "GET":
                raise PermissionError(
                    f"CompanyInfo path requires GET, got '{method}'."
                )
            path_realm = m.group(1)
            if path_realm != self._realm_id:
                raise PermissionError(
                    f"Realm mismatch: path has '{path_realm}', bound realm is '{self._realm_id}'."
                )
            return

        # Check POST /query (server-built SELECT only)
        m = _RE_QUERY.match(path_only)
        if m:
            if method != "POST":
                raise PermissionError(
                    f"Query path requires POST, got '{method}'."
                )
            path_realm = m.group(1)
            if path_realm != self._realm_id:
                raise PermissionError(
                    f"Realm mismatch: path has '{path_realm}', bound realm is '{self._realm_id}'."
                )
            return

        # Nothing matched — reject.
        raise PermissionError(
            f"Path '{path_only}' is not in the QBO read-only allowlist."
        )

    # ── SELECT builder ────────────────────────────────────────────────────────

    def build_select(
        self,
        entity: str,
        *,
        where: str | None = None,
        order_by: str | None = None,
        max_results: int = 100,
        start_position: int = 1,
    ) -> str:
        """Build a validated SELECT statement for an allowed entity.

        Only entities in ALLOWED_ENTITIES are accepted. This is the ONLY way
        a query string is ever produced — no raw SELECT param exists in the
        public API of this client.

        Args:
            entity: Must be in ALLOWED_ENTITIES ("Account", "CompanyInfo").
            where: Optional WHERE clause (appended verbatim; caller must sanitize).
            order_by: Optional ORDER BY clause.
            max_results: Max rows (1–1000, default 100).
            start_position: Pagination start position (default 1).

        Returns:
            A complete QBO SQL SELECT string.

        Raises:
            PermissionError: If entity is not in ALLOWED_ENTITIES.
            ValueError: If max_results or start_position are out of range.
        """
        if entity not in ALLOWED_ENTITIES:
            raise PermissionError(
                f"Entity '{entity}' is not in the read-only allowlist "
                f"{sorted(ALLOWED_ENTITIES)}."
            )
        if not (1 <= max_results <= 1000):
            raise ValueError(f"max_results must be 1–1000, got {max_results}")
        if start_position < 1:
            raise ValueError(f"start_position must be >= 1, got {start_position}")

        parts = [f"SELECT * FROM {entity}"]
        if where:
            parts.append(f"WHERE {where}")
        if order_by:
            parts.append(f"ORDERBY {order_by}")
        parts.append(f"STARTPOSITION {start_position}")
        parts.append(f"MAXRESULTS {max_results}")
        return " ".join(parts)

    # ── Low-level transport ───────────────────────────────────────────────────

    def _send(
        self,
        method: str,
        url: str,
        headers: dict[str, str],
        params: dict[str, str] | None,
        body: str | None,
    ) -> dict[str, Any]:
        """Send the HTTP request. Monkeypatch this in tests.

        Returns the parsed JSON response dict.
        """
        import requests  # lazy import

        resp = requests.request(
            method,
            url,
            headers=headers,
            params=params,
            data=body,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        body: str | None = None,
        _tool: str = "",
        _date_range: str = "",
    ) -> dict[str, Any]:
        """Enforce the allowlist, then send the request.

        The allowlist check happens BEFORE any network call.
        minorversion=75 is pinned on every request.
        """
        # ── ALLOWLIST CHECK (before any I/O) ──────────────────────────────
        self._check_allowlist(method, path, body)

        request_id = str(uuid.uuid4())
        url = f"{self._base_url}{path}"
        all_params = {"minorversion": _MINOR_VERSION}
        if params:
            all_params.update(params)

        access_token = self._token_provider()
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Accept": "application/json",
            "Content-Type": "application/text",
        }

        status = "ok"
        row_count = -1
        try:
            result = self._send(method, url, headers, all_params, body)
            # Best-effort row count from QueryResponse
            if "QueryResponse" in result:
                for _entity, rows in result["QueryResponse"].items():
                    if isinstance(rows, list):
                        row_count = len(rows)
                        break
            return result
        except Exception as exc:
            status = f"error:{type(exc).__name__}"
            raise
        finally:
            if self._audit_hook:
                try:
                    from pinky_qbo.audit import hash_realm
                    self._audit_hook(
                        _tool,
                        _endpoint_family(path),
                        _date_range,
                        request_id,
                        status,
                        row_count,
                        hash_realm(self._realm_id),
                    )
                except Exception as audit_exc:
                    _log(f"[pinky-qbo] audit hook failed: {audit_exc}")

    # ── Public API ────────────────────────────────────────────────────────────

    def report(
        self,
        name: str,
        params: dict[str, str] | None = None,
        *,
        _tool: str = "",
        _date_range: str = "",
    ) -> dict[str, Any]:
        """Fetch a QBO report by name. Name must be in ALLOWED_REPORTS."""
        path = f"/v3/company/{self._realm_id}/reports/{name}"
        return self._request("GET", path, params=params, _tool=_tool, _date_range=_date_range)

    def companyinfo(self, *, _tool: str = "qbo_company_info") -> dict[str, Any]:
        """Fetch QBO company information."""
        path = f"/v3/company/{self._realm_id}/companyinfo/{self._realm_id}"
        return self._request("GET", path, _tool=_tool)

    def query(
        self,
        entity: str,
        *,
        where: str | None = None,
        order_by: str | None = None,
        max_results: int = 100,
        start_position: int = 1,
        _tool: str = "",
    ) -> dict[str, Any]:
        """Execute a server-built SELECT for an allowed entity."""
        select = self.build_select(
            entity,
            where=where,
            order_by=order_by,
            max_results=max_results,
            start_position=start_position,
        )
        path = f"/v3/company/{self._realm_id}/query"
        return self._request("POST", path, body=select, _tool=_tool)


def _endpoint_family(path: str) -> str:
    """Derive a short endpoint family label from a path (for audit)."""
    path_only = path.split("?")[0]
    if "/reports/" in path_only:
        parts = path_only.split("/reports/")
        return f"reports/{parts[-1]}" if len(parts) > 1 else "reports"
    if "/companyinfo/" in path_only:
        return "companyinfo"
    if path_only.endswith("/query"):
        return "query"
    return "unknown"
