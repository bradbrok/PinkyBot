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

    Thread-safety: not async-safe. Caller must invoke from the same event loop
    (the streaming session reader loop). Methods are O(N) over recent
    timestamps which is fine — N is bounded by the threshold in practice.
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

        if now - self._last_alert_at < self._cooldown:
            return {
                "should_alert": False,
                "reason": "cooldown",
                "count": count,
                "agents_failing": agents_failing,
                "cooldown_remaining": int(
                    self._cooldown - (now - self._last_alert_at)
                ),
            }

        self._last_alert_at = now
        self._alert_count += 1
        return {
            "should_alert": True,
            "reason": (
                "host_wide" if agents_failing >= self._threshold else "agent_repeated"
            ),
            "count": count,
            "agents_failing": agents_failing,
        }

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
) -> str:
    """Render the operator-alert message body.

    Kept human-readable and short; includes enough detail for the operator
    to know which host to log into and re-auth.
    """
    reason = decision.get("reason", "")
    agents_failing = decision.get("agents_failing", 0)
    count = decision.get("count", 0)

    if reason == "host_wide":
        headline = (
            f"🚨 Claude auth broken on {host_label or 'this host'}: "
            f"{agents_failing} agent(s) failing"
        )
    else:
        headline = (
            f"🚨 Claude auth broken for *{agent_name}*: "
            f"{count} failures in window"
        )

    detail = error.strip()[:200] or "no error detail"
    instructions = (
        "Re-auth needed: SSH to the host, run `claude` to log back in, "
        "then `sudo systemctl restart pinkybot` (or equivalent)."
    )
    return f"{headline}\n\nLast error: `{detail}`\n\n{instructions}"
