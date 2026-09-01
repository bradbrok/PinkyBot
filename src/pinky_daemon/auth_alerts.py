"""Auth-failure tracking and operator alerts.

When the Claude SDK returns ``error='authentication_failed'`` for an agent's
streaming session, the user-facing chat sees nothing — the daemon suppresses
the response so raw error JSON never reaches end users. The downside is the
operator (the person who can re-auth) has no idea Claude credentials are
broken until someone complains.

This module provides:

- ``AuthFailureTracker`` — in-memory counter that records auth failures across
  all agents on a host, decides when an alert should fire, and enforces a
  cooldown so we don't spam the operator.
- ``resolve_operator_chat()`` — best-effort lookup of the operator's
  chat_id/platform. Reads ``operator_chat_id`` / ``operator_platform`` from
  system_settings if set; otherwise picks the chat_id appearing across the
  most ``approved_users`` rows (heuristic: the person with broadest access
  is the operator).

The streaming session calls into the tracker on each auth failure; when the
tracker says "alert", the daemon's alert wiring sends a Telegram DM via the
existing broker. Both are documented inline so the next Brad wedge debug is
faster.
"""
from __future__ import annotations

import logging
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Iterable

_log = logging.getLogger("pinky.auth_alerts").info
_warn = logging.getLogger("pinky.auth_alerts").warning


# ── Defaults ────────────────────────────────────────────────────────

DEFAULT_FAIL_WINDOW_SECONDS = 300        # 5 min
DEFAULT_FAIL_THRESHOLD = 3               # within window → alert
DEFAULT_ALERT_COOLDOWN_SECONDS = 1800    # 30 min between alerts


# ── Remedy text per failure class ───────────────────────────────────
#
# Each operator alert ends with a class-specific remedy. Auth keeps its
# original "re-auth" instructions verbatim; billing and rate_limit need
# different actions (re-auth wouldn't fix either).

DEFAULT_AUTH_REMEDY = (
    "Re-auth needed: SSH to the host, run 'claude' to log back in, "
    "then 'sudo systemctl restart pinkybot' (or equivalent)."
)
BILLING_REMEDY = (
    "Billing/credits issue — the agent is blocked until it's resolved. "
    "Check the provider account's payment/credit status "
    "(e.g. console.anthropic.com) and top up or fix the payment method. "
    "Re-auth will NOT fix this."
)
RATE_LIMIT_REMEDY = (
    "Sustained rate-limiting — requests are being throttled repeatedly. "
    "Often transient, but if it persists, check the account's usage/plan "
    "limits; turns will keep failing until capacity frees up."
)


def auth_alert_copy_for_provider(provider_url: str) -> tuple[str, str]:
    """Return the default auth-alert copy for any provider URL."""
    return "Claude auth broken", DEFAULT_AUTH_REMEDY


@dataclass(frozen=True)
class FailureAlertPolicy:
    """Alert tuning + messaging for one non-auth StopFailure class (#104).

    Each policy gets its own ``AuthFailureTracker`` instance (the tracker is
    failure-class-agnostic) so counts never comingle across classes — a
    billing failure must not count toward the rate_limit threshold.
    """

    error_type: str
    problem: str  # headline phrase passed to format_alert_message(problem=...)
    remedy: str
    fail_window_seconds: int
    fail_threshold: int
    alert_cooldown_seconds: int


# Non-auth StopFailure classes that warrant a proactive operator alert (#104).
# Auth (authentication_failed / oauth_org_not_allowed) has its own dedicated
# path (AuthFailureTracker + _on_auth_failure) and is intentionally NOT here.
#
#   billing_error → persistent + operator-actionable (only a human can fix
#       payment/credits, and it won't self-resolve). Low threshold so the
#       operator hears about it fast.
#   rate_limit    → usually transient. Only alert when SUSTAINED — a higher
#       threshold + longer window/cooldown so a normal burst never pages
#       anyone, but a genuine capacity outage still surfaces.
#
# server_error / overloaded / invalid_request / unknown are deliberately
# absent: Anthropic-side or non-actionable, self-resolving — log-only.
#
# AGING — why these classes use window-eviction only and have NO
# clear-on-success hook (intentional asymmetry with the auth path):
#   The auth tracker clears on a successful turn because auth is BINARY — one
#   success proves the credential works, so stale failures should drop. These
#   classes are CONTINUOUS-DEGRADATION signals where intermittent successes
#   are *expected during the very failure mode we're detecting*:
#     - rate_limit: a clear-on-success would defeat "only if sustained" — every
#       200 between two 429s would reset the count, so 5-in-10min could never
#       accumulate. The semantics require wall-clock window counting.
#     - billing_error: a success between failures usually means partial
#       degradation (fallback cred, race against billing-fix propagation), none
#       of which argue for resetting; a genuine fix is absorbed by the cooldown.
#   So failures age out purely by sliding-window eviction. (Reviewed: #104/PR599.)
TRANSPORT_FAILURE_POLICIES: dict[str, "FailureAlertPolicy"] = {
    "billing_error": FailureAlertPolicy(
        error_type="billing_error",
        problem="Claude billing blocked",
        remedy=BILLING_REMEDY,
        fail_window_seconds=600,        # 10 min
        fail_threshold=2,               # low — billing won't self-resolve
        alert_cooldown_seconds=1800,    # 30 min
    ),
    "rate_limit": FailureAlertPolicy(
        error_type="rate_limit",
        problem="Claude rate-limited",
        remedy=RATE_LIMIT_REMEDY,
        fail_window_seconds=600,        # 10 min
        fail_threshold=5,               # higher — only sustained throttling
        alert_cooldown_seconds=3600,    # 60 min — quieter; often transient
    ),
}


@dataclass
class _AgentFailures:
    """Sliding-window record of recent auth failures for one agent."""

    timestamps: list[float] = field(default_factory=list)
    last_error: str = ""

    def record(self, now: float, error: str, window: float) -> int:
        """Append a failure timestamp, prune older ones, return current count."""
        self.timestamps.append(now)
        self.last_error = error or ""
        cutoff = now - window
        self.timestamps = [t for t in self.timestamps if t >= cutoff]
        return len(self.timestamps)

    def reset(self) -> None:
        self.timestamps = []
        self.last_error = ""


class AuthFailureTracker:
    """Per-host auth failure detector with cooldown.

    Concurrency: callers must share a single event loop (the streaming-session
    reader loop). No internal locking is needed — every method is synchronous
    and mutation happens before return. Methods are O(N) over recent
    timestamps which is fine — N is bounded by the threshold in practice.

    Two-phase alerting: ``record_failure()`` decides whether an alert *should*
    fire but never advances the cooldown by itself. The caller must invoke
    ``commit_alert()`` only after the operator notification was actually
    delivered. If delivery raises (broker down, bot token revoked, etc.), the
    cooldown is preserved so the next failure can retry — that's the whole
    reason this PR exists, so a single failed-send doesn't silence the alert
    system for 30 minutes.
    """

    def __init__(
        self,
        *,
        fail_window_seconds: int = DEFAULT_FAIL_WINDOW_SECONDS,
        fail_threshold: int = DEFAULT_FAIL_THRESHOLD,
        alert_cooldown_seconds: int = DEFAULT_ALERT_COOLDOWN_SECONDS,
        clock=time.time,
    ) -> None:
        self._window = fail_window_seconds
        self._threshold = fail_threshold
        self._cooldown = alert_cooldown_seconds
        self._clock = clock
        self._agents: dict[str, _AgentFailures] = defaultdict(_AgentFailures)
        self._last_alert_at: float = 0.0
        self._first_failure_at: float = 0.0  # When current outage started
        self._alert_count: int = 0           # Alerts fired since last clear

    def record_failure(self, agent_name: str, error: str = "") -> dict:
        """Record one auth failure. Returns a decision dict.

        Returned keys:
            should_alert (bool):  True if this failure crossed the threshold
                                  AND cooldown has elapsed.
            reason (str):         Short message explaining the decision.
            count (int):          Current failures-in-window for this agent.
            agents_failing (int): Count of distinct agents currently failing.
        """
        now = self._clock()
        if self._first_failure_at == 0.0:
            self._first_failure_at = now

        record = self._agents[agent_name]
        count = record.record(now, error, self._window)
        agents_failing = sum(1 for r in self._agents.values() if r.timestamps)

        # Multi-agent host outage: if ≥ threshold agents failing simultaneously,
        # alert immediately on the very first qualifying failure (don't wait for
        # any single agent to hit `threshold` failures alone).
        threshold_hit = (
            count >= self._threshold or agents_failing >= self._threshold
        )

        if not threshold_hit:
            return {
                "should_alert": False,
                "reason": "below_threshold",
                "count": count,
                "agents_failing": agents_failing,
            }

        if self._last_alert_at and now - self._last_alert_at < self._cooldown:
            return {
                "should_alert": False,
                "reason": "cooldown",
                "count": count,
                "agents_failing": agents_failing,
                "cooldown_remaining": int(
                    self._cooldown - (now - self._last_alert_at)
                ),
            }

        # NOTE: do NOT advance _last_alert_at / _alert_count here. Caller must
        # invoke commit_alert() after successful delivery; that way a broker
        # failure preserves the cooldown so the alert can retry on the next
        # failure instead of being silenced for the cooldown window.
        return {
            "should_alert": True,
            "reason": (
                "host_wide" if agents_failing >= self._threshold else "agent_repeated"
            ),
            "count": count,
            "agents_failing": agents_failing,
        }

    def commit_alert(self) -> None:
        """Mark an alert as delivered: advance cooldown and bump the counter.

        Call this only after the operator notification was actually sent. If
        delivery fails, do NOT call this — the next record_failure() that
        crosses threshold will return should_alert=True so the alert retries
        instead of being lost for the cooldown window.
        """
        self._last_alert_at = self._clock()
        self._alert_count += 1

    def record_success(self, agent_name: str) -> None:
        """Clear failure tracking for an agent that successfully called Claude.

        If all agents are clear, reset the outage state so the next outage
        emits a fresh "outage started" alert.
        """
        if agent_name in self._agents:
            self._agents[agent_name].reset()
        if not any(r.timestamps for r in self._agents.values()):
            self._first_failure_at = 0.0
            # Keep _last_alert_at in place — cooldown still respected even
            # across a brief recovery → re-failure to avoid alert flapping.

    def status(self) -> dict:
        """Snapshot for /admin/watchdog and diagnostics.

        Status semantics:
            "ok"        — no agent has failures in window.
            "degraded"  — some failures but below threshold.
            "broken"    — at or above threshold (alert is or will fire).
        """
        now = self._clock()
        agents_failing: list[dict] = []
        for name, rec in self._agents.items():
            # Prune stale timestamps lazily here too so /admin/watchdog
            # doesn't show ghosts after the window has elapsed.
            cutoff = now - self._window
            rec.timestamps = [t for t in rec.timestamps if t >= cutoff]
            if not rec.timestamps:
                continue
            agents_failing.append(
                {
                    "agent": name,
                    "failures_in_window": len(rec.timestamps),
                    "last_failure_age_s": int(now - rec.timestamps[-1]),
                    "last_error": rec.last_error[:120],
                }
            )

        if not agents_failing:
            status = "ok"
        elif any(
            a["failures_in_window"] >= self._threshold for a in agents_failing
        ) or len(agents_failing) >= self._threshold:
            status = "broken"
        else:
            status = "degraded"

        return {
            "status": status,
            "fail_window_seconds": self._window,
            "fail_threshold": self._threshold,
            "alert_cooldown_seconds": self._cooldown,
            "outage_started_at": self._first_failure_at or None,
            "outage_age_seconds": (
                int(now - self._first_failure_at)
                if self._first_failure_at
                else None
            ),
            "alerts_sent": self._alert_count,
            "last_alert_age_seconds": (
                int(now - self._last_alert_at) if self._last_alert_at else None
            ),
            "agents_failing": agents_failing,
        }


# ── Operator chat resolution ────────────────────────────────────────


def resolve_operator_chat(
    *,
    get_setting,
    list_all_approved_users,
) -> tuple[str, str]:
    """Pick the (chat_id, platform) that should receive operator alerts.

    Resolution order:
        1. ``operator_chat_id`` + ``operator_platform`` in system_settings.
        2. Fallback: the chat_id appearing across the most approved_users rows
           (heuristic: the person with broadest access is the operator).
        3. Empty strings if nothing matches — caller must handle "no operator".

    Returns ("", "") when unresolved. Never raises.
    """
    try:
        chat_id = (get_setting("operator_chat_id", "") or "").strip()
        platform = (get_setting("operator_platform", "") or "").strip() or "telegram"
        if chat_id:
            return chat_id, platform
    except Exception as e:
        _warn("auth_alerts: failed to read operator_chat_id setting: %s", e)

    try:
        users: Iterable[dict] = list_all_approved_users() or []
    except Exception as e:
        _warn("auth_alerts: failed to list approved users: %s", e)
        return "", ""

    counter: Counter[str] = Counter()
    for u in users:
        cid = (u.get("chat_id") or "").strip() if isinstance(u, dict) else ""
        if cid:
            counter[cid] += 1
    if not counter:
        return "", ""

    chat_id, _ = counter.most_common(1)[0]
    return chat_id, "telegram"


# ── Alert formatting ───────────────────────────────────────────────


def format_alert_message(
    *,
    agent_name: str,
    decision: dict,
    error: str,
    host_label: str = "",
    problem: str = "Claude auth broken",
    remedy: str = DEFAULT_AUTH_REMEDY,
) -> str:
    """Render the operator-alert message body.

    Plain text — _broker_send defaults to no parse_mode, so anything wrapped
    in ``*`` or `` ` `` would render literally on Telegram. Keep it readable
    without markdown affordances.

    Kept human-readable and short; includes enough detail for the operator
    to know which host to log into and what to do.

    ``problem`` / ``remedy`` parameterize the message per failure class (#104).
    The defaults reproduce the original auth-alert wording verbatim so the
    auth path is unchanged.
    """
    reason = decision.get("reason", "")
    agents_failing = decision.get("agents_failing", 0)
    count = decision.get("count", 0)

    if reason == "host_wide":
        headline = (
            f"🚨 {problem} on {host_label or 'this host'}: "
            f"{agents_failing} agent(s) failing"
        )
    else:
        headline = (
            f"🚨 {problem} for {agent_name}: "
            f"{count} failures in window"
        )

    detail = error.strip()[:200] or "no error detail"
    return f"{headline}\n\nLast error: {detail}\n\n{remedy}"
