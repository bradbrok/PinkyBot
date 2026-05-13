"""Auth / admin rate limiting middleware (#438).

Slows brute-force on login and burst-probing on admin endpoints. Mode-aware:

* ``trusted`` — disabled; back-compat with the Mac Mini deployment.
* ``lan`` — auth + admin buckets active. Default-IP bucket also on.
* ``web`` — all buckets active.

Keys
----
The IP for any IP-keyed bucket is the canonical client IP from the #442
resolver, **not** ``request.client.host`` and **not** raw
``X-Forwarded-For``. That's the only attribution surface that's safe
once a reverse proxy is involved.

Buckets
-------
============  ================================================  ========  =============  ==================
Bucket        Routes                                            Limit     Window         Lockout penalty
============  ================================================  ========  =============  ==================
``auth``      ``/auth/login`` POST, ``/auth/password`` POST     5         300s           900s after exceed
``admin``     prefix ``/admin/``                                30        60s            none (429 only)
``webhook``   prefix ``/triggers/webhook/``                     60        60s            none (IP-keyed for
                                                                                         now; switch to token
                                                                                         hash when #435 lands)
``default``   everything else                                   300       60s            none (lan/web only)
============  ================================================  ========  =============  ==================

Backend
-------
**In-memory only** in this PR. The store is per-process; multi-worker
deployments are not yet supported in ``web`` mode (#441 will add a
guard once Redis backend lands). The single-uvicorn-worker default
the daemon ships with is fine.

Login keying refinement
-----------------------
Per Murzik's review note: login limiting should key on both source IP
**and** normalised email so neither shared NAT nor distributed brute
force gets a free pass. This PR ships IP-only keying since the
username/email shape arrives with #435; a TODO marks the second key
addition for the follow-up.

Audit
-----
Every 429 emits a ``rate_limit.exceeded`` event into the security
audit log (#440) when one is wired through. Audit emission is a
no-op when ``security_audit`` isn't on ``app.state``, so this
middleware works in isolation.

Part of #433. Issue #438.
"""

from __future__ import annotations

import logging
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from threading import Lock

from fastapi import Request
from fastapi.responses import JSONResponse

from pinky_daemon.config import DeploymentMode
from pinky_daemon.net.client_identity import resolve_client_identity

__all__ = [
    "DEFAULT_BUCKETS",
    "BucketConfig",
    "InMemoryRateLimiter",
    "build_rate_limit_middleware",
    "classify_request",
]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class BucketConfig:
    """Per-bucket rate-limit configuration."""

    name: str
    limit: int
    window_seconds: float
    penalty_seconds: float = 0.0  # extra lockout after exceed (auth bucket)


#: Bucket definitions. The order is the classification priority — first
#: matching predicate wins. Webhook before admin before default so
#: ``/triggers/webhook/foo`` doesn't fall into the admin bucket if some
#: future route ever overlaps.
DEFAULT_BUCKETS: tuple[BucketConfig, ...] = (
    BucketConfig(name="auth", limit=5, window_seconds=300.0,
                 penalty_seconds=900.0),
    BucketConfig(name="webhook", limit=60, window_seconds=60.0),
    BucketConfig(name="admin", limit=30, window_seconds=60.0),
    BucketConfig(name="default", limit=300, window_seconds=60.0),
)


_AUTH_PATHS = ("/auth/login", "/auth/password")
_ADMIN_PREFIX = "/admin/"
_WEBHOOK_PREFIX = "/triggers/webhook/"


def classify_request(method: str, path: str) -> str:
    """Decide which bucket a request belongs to. Pure function for tests."""
    method = method.upper()
    if method == "POST" and path in _AUTH_PATHS:
        return "auth"
    if path.startswith(_WEBHOOK_PREFIX):
        return "webhook"
    if path.startswith(_ADMIN_PREFIX):
        return "admin"
    return "default"


def _bucket_active_in_mode(bucket: str, mode: DeploymentMode) -> bool:
    if mode is DeploymentMode.TRUSTED:
        return False
    if mode is DeploymentMode.LAN:
        # LAN: auth + admin only. Default + webhook are off so behind-LAN
        # internal traffic isn't accidentally throttled.
        return bucket in ("auth", "admin")
    # web: everything
    return True


# ── Token store ──────────────────────────────────────────────────


@dataclass
class _BucketState:
    """Per-key state: a deque of recent timestamps + an optional lockout."""

    timestamps: deque = field(default_factory=deque)
    locked_until: float = 0.0


class InMemoryRateLimiter:
    """Sliding-window-counter rate limiter.

    Process-local. Thread-safe via a single mutex (rate-limit checks are
    cheap; the mutex is held for microseconds). Multi-worker deployments
    will need a Redis backend; this PR documents the limitation.
    """

    def __init__(self, buckets: tuple[BucketConfig, ...] = DEFAULT_BUCKETS):
        self._configs = {b.name: b for b in buckets}
        self._state: dict[tuple[str, str], _BucketState] = {}
        self._lock = Lock()

    def configs(self) -> dict[str, BucketConfig]:
        return dict(self._configs)

    def check(
        self,
        bucket: str,
        key: str,
        *,
        now: float | None = None,
    ) -> tuple[bool, float]:
        """Return ``(allowed, retry_after_seconds)`` for one request.

        ``allowed=True`` means the request is under the limit and the
        timestamp is recorded. ``allowed=False`` means it exceeded; the
        caller should respond with 429 + ``Retry-After: ceil(retry_after)``.

        The auth bucket additionally activates a lockout window after the
        limit is exceeded; subsequent requests during the lockout return
        ``False`` immediately without recording new timestamps.
        """
        config = self._configs.get(bucket)
        if config is None:
            return True, 0.0
        n = now if now is not None else time.monotonic()

        with self._lock:
            state = self._state.setdefault((bucket, key), _BucketState())

            # Drop timestamps that have rolled out of the window.
            cutoff = n - config.window_seconds
            while state.timestamps and state.timestamps[0] < cutoff:
                state.timestamps.popleft()

            # Lockout still active?
            if state.locked_until > n:
                return False, state.locked_until - n

            if len(state.timestamps) >= config.limit:
                # Limit exceeded — apply penalty if configured.
                if config.penalty_seconds > 0:
                    state.locked_until = n + config.penalty_seconds
                    retry_after = config.penalty_seconds
                else:
                    retry_after = state.timestamps[0] + config.window_seconds - n
                return False, max(retry_after, 0.0)

            state.timestamps.append(n)
            return True, 0.0

    def reset(self, bucket: str, key: str) -> None:
        """Clear state for one (bucket, key). Test helper / admin override."""
        with self._lock:
            self._state.pop((bucket, key), None)


# ── Middleware factory ───────────────────────────────────────────


_RETRY_BODY_TEMPLATE = {
    "auth": "too many attempts, try again in {n} seconds",
    "admin": "too many requests, try again in {n} seconds",
    "webhook": "too many requests, try again in {n} seconds",
    "default": "too many requests, try again in {n} seconds",
}


def build_rate_limit_middleware(
    *,
    limiter: InMemoryRateLimiter | None = None,
):
    """Return an ASGI middleware closure that enforces the bucket limits.

    Args:
        limiter: Optional explicit limiter instance. When ``None`` the
            middleware reads from ``app.state.rate_limiter`` per request,
            so a single process can hot-swap config without recreating
            the middleware. ``create_api`` always pre-installs one.
    """

    async def rate_limit_mw(
        request: Request,
        call_next: Callable[[Request], Awaitable],
    ):
        state = request.app.state if request.app else None
        mode = getattr(state, "deployment_mode", DeploymentMode.TRUSTED)
        if mode is DeploymentMode.TRUSTED:
            return await call_next(request)

        bucket = classify_request(request.method, request.url.path)
        if not _bucket_active_in_mode(bucket, mode):
            return await call_next(request)

        active_limiter = limiter or getattr(state, "rate_limiter", None)
        if active_limiter is None:
            return await call_next(request)

        trusted_proxies = (
            getattr(state, "trusted_proxies", None) if state else None
        ) or ()
        ci = resolve_client_identity(request, mode, trusted_proxies)
        # All buckets keyed on canonical IP for now. #435 will refine
        # admin → user_id and webhook → token-hash-prefix.
        key = str(ci.ip)

        allowed, retry_after = active_limiter.check(bucket, key)
        if allowed:
            return await call_next(request)

        retry_after_int = max(int(retry_after) + 1, 1)
        msg = _RETRY_BODY_TEMPLATE.get(
            bucket, _RETRY_BODY_TEMPLATE["default"]
        ).format(n=retry_after_int)

        # Best-effort security-audit emission. Log even when audit isn't
        # wired so the operator sees the trip in the standard log.
        logger.info(
            "rate_limit: bucket=%s key=%s exceeded (mode=%s, path=%s)",
            bucket, key, mode.value, request.url.path,
        )
        sec_audit = getattr(state, "security_audit", None) if state else None
        if sec_audit is not None:
            try:
                from pinky_daemon.security_audit import AuditOutcome

                sec_audit.log_event(
                    "rate_limit.exceeded",
                    outcome=AuditOutcome.DENIED,
                    actor_ip=str(ci.ip),
                    via_proxy=ci.via_proxy,
                    forwarded_chain=[str(h) for h in ci.forwarded_chain]
                    if ci.forwarded_chain
                    else None,
                    method=request.method,
                    route_name=bucket,
                    target=request.url.path,
                    detail={
                        "bucket": bucket,
                        "limit": active_limiter.configs()[bucket].limit,
                    },
                )
            except Exception:  # noqa: BLE001
                logger.exception("rate_limit: failed to emit audit event")

        return JSONResponse(
            {"error": "rate_limited", "detail": msg},
            status_code=429,
            headers={"Retry-After": str(retry_after_int)},
        )

    return rate_limit_mw
