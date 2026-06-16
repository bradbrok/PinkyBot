"""QuickBooks Online adapter — owns the OAuth lifecycle (spec §4/§7).

Responsibilities:
  - Lazily construct the read-only :class:`~pinky_qbo.client.QBOClient`.
  - Keep the access token fresh (``_is_expired`` with a 60s skew).
  - Refresh under an in-process lock. THE MOST IMPORTANT CORRECTNESS PATH:
    QBO refresh tokens rotate on every use, so under the lock we
    **re-read the latest stored refresh token, refresh, then persist the
    rotated token atomically** before releasing. This prevents two concurrent
    tools in one onesie turn from both spending the same (single-use) refresh
    token and triggering ``invalid_grant`` fleet de-auth.

Never logs token material.
"""

from __future__ import annotations

import contextlib
import os
import sys
import threading
import time

try:
    import fcntl  # POSIX file locking; the fleet is all Linux/macOS.
except ImportError:  # pragma: no cover - non-POSIX fallback
    fcntl = None  # type: ignore[assignment]

from pinky_qbo import oauth
from pinky_qbo.adapters.base import (
    AbstractAccountingAdapter,
    AccountSummary,
    TokenBundle,
)
from pinky_qbo.client import QBOClient
from pinky_qbo.store import TokenStore

# Refresh the access token this many seconds BEFORE its nominal expiry.
_EXPIRY_SKEW_SEC = 60


def _log(msg: str) -> None:
    print(f"[pinky-qbo.qbo] {msg}", file=sys.stderr)


class QuickBooksAdapter(AbstractAccountingAdapter):
    """Read-only QBO adapter bound to one agent + realm.

    Args:
        store: agent-scoped :class:`TokenStore` (client creds + rotating refresh).
        realm_id: bound QBO company id.
        agent: bound agent name (for audit rows).
        env: ``"production"`` (default) or ``"sandbox"``.
    """

    def __init__(
        self,
        store: TokenStore,
        realm_id: str,
        agent: str = "",
        env: str = "production",
    ) -> None:
        self._store = store
        self._realm_id = realm_id
        self._agent = agent
        self._env = env or "production"

        # In-memory token state. The access token is never persisted (only the
        # rotating refresh token is). expires_at=0 forces a refresh on first use.
        self._tokens = TokenBundle()
        self._refresh_lock = threading.Lock()

        # Lazy client — built once, fed a fresh-token provider.
        self._client: QBOClient | None = None

    # ── client (lazy) ────────────────────────────────────────────────────────

    def _get_client(self) -> QBOClient:
        if self._client is None:
            self._client = QBOClient(
                realm_id=self._realm_id,
                token_provider=self._fresh_access_token,
                agent=self._agent,
                env=self._env,
            )
        return self._client

    # ── token freshness ──────────────────────────────────────────────────────

    def _is_expired(self) -> bool:
        """True if the access token is missing or within the skew of expiry."""
        if not self._tokens.access_token:
            return True
        return (self._tokens.expires_at - _EXPIRY_SKEW_SEC) <= time.time()

    def _fresh_access_token(self) -> str:
        """Return a valid access token, refreshing if needed. Called per request."""
        if self._is_expired():
            self._maybe_refresh()
        return self._tokens.access_token

    def _maybe_refresh(self) -> None:
        """Refresh the access token, rotating the refresh token atomically.

        CRITICAL: under the lock we RE-READ the latest stored refresh token
        (a peer may have rotated it while we waited), refresh, then persist the
        new refresh token before any peer can observe a half-updated state.
        Double-checked: if a peer in THIS process already refreshed while we
        waited, we skip the network call entirely.

        Two locks: an in-process :class:`threading.Lock` (concurrent tools in one
        onesie turn) AND a cross-process advisory file lock (a second stdio
        process for the same agent — e.g. a cold-start overlap — must not spend
        the same single-use refresh token concurrently; that race is the classic
        ``invalid_grant`` de-auth, see the fleet refresh-race history).
        """
        with self._refresh_lock:
            # Double-check inside the in-process lock — a peer thread may have
            # just refreshed.
            if not self._is_expired():
                return
            # Hold the cross-process lock across re-read → refresh → persist.
            with self._xproc_refresh_lock():
                self._do_refresh_locked()

    def _do_refresh_locked(self) -> None:
        """The refresh critical section. Caller MUST hold both refresh locks."""
        client_id, client_secret = self._store.get_client_credentials()
        if not client_id or not client_secret:
            raise RuntimeError("QBO client credentials not configured")

        # RE-READ the latest stored refresh token under both locks — another
        # PROCESS may have rotated it while we waited on the file lock.
        current_refresh = self._store.get_refresh_token()
        if not current_refresh:
            raise RuntimeError("QBO refresh token not configured")

        result = oauth.refresh(client_id, client_secret, current_refresh)

        new_refresh = result.get("refresh_token") or current_refresh
        access = result.get("access_token") or ""
        if not access:
            raise RuntimeError("QBO refresh returned no access token")

        # Persist the rotated refresh token FIRST (atomic, encrypted) so a
        # crash after this point never reuses the spent token.
        if new_refresh != current_refresh:
            self._store.save_refresh_token(new_refresh)

        # Then publish the new in-memory state.
        try:
            ttl = int(result.get("expires_in") or 3600)
        except (TypeError, ValueError):
            ttl = 3600
        self._tokens = TokenBundle(
            access_token=access,
            refresh_token=new_refresh,
            expires_at=time.time() + ttl,
        )

    @contextlib.contextmanager
    def _xproc_refresh_lock(self):
        """Best-effort cross-process exclusive lock for the refresh section.

        Advisory POSIX ``flock``; degrades to a no-op where ``fcntl`` is absent
        (non-POSIX). Serialises the single-use refresh-token spend across
        separate stdio processes for the same agent.
        """
        if fcntl is None:  # pragma: no cover - non-POSIX fallback
            yield
            return
        path = self._refresh_lock_path()
        lf = open(path, "w")
        try:
            fcntl.flock(lf.fileno(), fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(lf.fileno(), fcntl.LOCK_UN)
            finally:
                lf.close()

    def _refresh_lock_path(self) -> str:
        """Per-agent lock file, co-located with the agents DB (or TMPDIR)."""
        db = os.environ.get("PINKY_AGENTS_DB", "")
        base = os.path.dirname(db) if db else os.environ.get("TMPDIR", "/tmp")
        safe = "".join(c for c in (self._agent or "default") if c.isalnum() or c in "-_")
        return os.path.join(base, f".qbo_refresh_{safe}.lock")

    # ── read-only operations ─────────────────────────────────────────────────

    def get_report(self, report: str, params: dict[str, str], *, tool: str = "") -> dict:
        return self._get_client().get_report(report, params, tool=tool)

    def get_company_info(self) -> dict:
        return self._get_client().get_company_info()

    def list_accounts(
        self,
        account_type: str = "",
        active_only: bool = True,
    ) -> list[AccountSummary]:
        client = self._get_client()
        select = client.select_accounts(
            account_type=account_type, active_only=active_only
        )
        data = client.query(select, tool="qbo_list_accounts")
        rows = (data.get("QueryResponse") or {}).get("Account") or []
        out: list[AccountSummary] = []
        for r in rows:
            out.append(
                AccountSummary(
                    account_id=str(r.get("Id", "")),
                    name=r.get("Name", ""),
                    account_type=r.get("AccountType", ""),
                    account_subtype=r.get("AccountSubType", ""),
                    current_balance=float(r.get("CurrentBalance", 0) or 0),
                    active=bool(r.get("Active", True)),
                )
            )
        return out
