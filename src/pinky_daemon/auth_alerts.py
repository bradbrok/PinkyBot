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


# ── Poisoned-transcript detection (auto-heal) ───────────────────────
#
# Distinct from the alert classes above: those surface a problem a HUMAN
# must fix (re-auth, top up billing, wait out throttling). A *poisoned
# transcript* is one the daemon can fix ITSELF, and must — because it
# never self-resolves and silently wedges the agent.
#
# THE FAILURE MODE
#   With extended thinking on, every ``thinking`` block carries a crypto
#   signature and Anthropic requires the latest assistant message's
#   thinking blocks passed back byte-for-byte. When Claude Code cancels a
#   sibling in a parallel tool batch (one tool errored), it reconstructs
#   that assistant message and the thinking blocks no longer match → API
#   400 "thinking blocks ... cannot be modified". That message is now baked
#   into the transcript, so EVERY subsequent turn re-sends it and fails
#   identically. ``claude --continue`` just resumes the poison. The agent
#   crash-loops until a human force-restarts it onto a fresh transcript.
#
# THE FIX
#   Detect it and force a fresh restart (drop ``--continue`` → new
#   transcript), abandoning the poisoned history. Two triggers, belt +
#   suspenders:
#     1. Signature match — the StopFailure ``message`` carries the API
#        error body; the "thinking ... cannot be modified" wording is a
#        definitive, non-retryable poison signal. Claude Code reports this
#        as a coarse ``unknown``/``invalid_request`` error_type, so the
#        signature (not error_type) is what catches it.
#     2. Tight-loop fallback — N rapid StopFailures (any class) on the SAME
#        session_id within a short window. Catches future poison variants
#        the string match misses, while the time window keeps sporadic
#        rate_limits across a healthy long session from false-triggering.

POISONED_TRANSCRIPT_ERROR_TYPE = "poisoned_transcript"

# Auto-heal tuning.
DEFAULT_POISON_SIG_THRESHOLD = 2     # consecutive signature hits on a session
DEFAULT_POISON_LOOP_THRESHOLD = 4    # rapid any-class hits on a session
DEFAULT_POISON_LOOP_WINDOW_SECONDS = 180     # "rapid" = within this gap
DEFAULT_POISON_RESET_COOLDOWN_SECONDS = 600  # ≤1 auto-heal per agent / 10 min


def classify_stop_failure(error_type: str, message: str) -> tuple[str, bool]:
    """Map a raw StopFailure to ``(effective_error_type, is_poison_signature)``.

    Detects the thinking-block integrity 400 from the error ``message``
    regardless of the coarse ``error_type`` Claude Code sends for it (it
    arrives as ``unknown`` / ``invalid_request``). When matched, the
    effective type is upgraded to ``poisoned_transcript`` for logging +
    auto-heal routing; otherwise the original ``error_type`` passes through
    unchanged so existing auth/billing/rate_limit classification is
    untouched.
    """
    msg = (message or "").lower()
    if "thinking" in msg and (
        "cannot be modified" in msg or "must remain as they were" in msg
    ):
        return POISONED_TRANSCRIPT_ERROR_TYPE, True
    return (error_type or "unknown").strip() or "unknown", False


@dataclass
class _AgentPoisonState:
    """Per-agent streak state for poisoned-transcript detection."""

    session_id: str = ""
    sig_count: int = 0          # consecutive signature hits on this session
    loop_count: int = 0         # rapid any-class hits on this session
    last_failure_at: float = 0.0
    last_reset_at: float = 0.0  # rate-limit anchor (advanced on commit_reset)


class PoisonedTranscriptTracker:
    """Decides when a wedged tmux session should be auto-healed (#poison).

    Per-agent state keyed on ``session_id``: a streak only accumulates
    within one transcript and resets the moment the session_id changes
    (i.e. after a heal spawns a fresh transcript). Two-phase like
    ``AuthFailureTracker``: ``record_failure`` decides, ``commit_reset``
    advances the cooldown — so a heal that fails to launch doesn't burn the
    cooldown and the next failure retries.

    Single-event-loop, no locking (mirrors ``AuthFailureTracker``).
    """

    def __init__(
        self,
        *,
        sig_threshold: int = DEFAULT_POISON_SIG_THRESHOLD,
        loop_threshold: int = DEFAULT_POISON_LOOP_THRESHOLD,
        loop_window_seconds: int = DEFAULT_POISON_LOOP_WINDOW_SECONDS,
        reset_cooldown_seconds: int = DEFAULT_POISON_RESET_COOLDOWN_SECONDS,
        clock=time.time,
    ) -> None:
        self._sig_threshold = sig_threshold
        self._loop_threshold = loop_threshold
        self._loop_window = loop_window_seconds
        self._cooldown = reset_cooldown_seconds
        self._clock = clock
        self._agents: dict[str, _AgentPoisonState] = defaultdict(_AgentPoisonState)

    def record_failure(
        self, agent_name: str, session_id: str, is_signature: bool
    ) -> dict:
        """Record one StopFailure; return a decision dict.

        Keys: ``should_reset`` (bool), ``reason`` (signature|loop|
        below_threshold|cooldown|no_session_id), ``sig_count``, ``loop_count``.

        A non-empty ``session_id`` is required — it's the streak anchor; we
        can't tell a tight loop apart from sporadic failures without it.
        """
        session_id = (session_id or "").strip()
        if not session_id:
            return {"should_reset": False, "reason": "no_session_id"}

        now = self._clock()
        st = self._agents[agent_name]

        # New transcript → fresh streak. (A heal clears session_id via
        # commit_reset, so the post-heal session's failures start clean.)
        if session_id != st.session_id:
            st.session_id = session_id
            st.sig_count = 0
            st.loop_count = 0
            st.last_failure_at = 0.0

        # Tight-loop counting: a gap longer than the window breaks the
        # streak, so rate_limits sprinkled across a healthy long-lived
        # session never accumulate to the loop threshold.
        if st.last_failure_at and (now - st.last_failure_at) > self._loop_window:
            st.loop_count = 0
        st.loop_count += 1
        if is_signature:
            st.sig_count += 1
        st.last_failure_at = now

        if st.last_reset_at and now - st.last_reset_at < self._cooldown:
            return {
                "should_reset": False,
                "reason": "cooldown",
                "sig_count": st.sig_count,
                "loop_count": st.loop_count,
                "cooldown_remaining": int(
                    self._cooldown - (now - st.last_reset_at)
                ),
            }

        if st.sig_count >= self._sig_threshold:
            reason = "signature"
        elif st.loop_count >= self._loop_threshold:
            reason = "loop"
        else:
            return {
                "should_reset": False,
                "reason": "below_threshold",
                "sig_count": st.sig_count,
                "loop_count": st.loop_count,
            }

        return {
            "should_reset": True,
            "reason": reason,
            "sig_count": st.sig_count,
            "loop_count": st.loop_count,
        }

    def commit_reset(self, agent_name: str) -> None:
        """Mark an auto-heal as performed: start the cooldown and clear the
        streak. Call ONLY after the force-restart succeeded — a failed heal
        must leave the cooldown unadvanced so the next failure retries."""
        st = self._agents[agent_name]
        st.last_reset_at = self._clock()
        st.sig_count = 0
        st.loop_count = 0
        # Drop the anchor so the post-heal transcript starts a clean streak
        # even if its new session_id is briefly unknown to us.
        st.session_id = ""


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
