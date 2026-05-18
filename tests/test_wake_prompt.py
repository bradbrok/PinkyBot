"""Tests for the pure wake-prompt builder.

The builder is contract-pinned: every transport (SDK / tmux / Codex)
must produce identical output for equivalent inputs. These tests pin the
exact string structure so a refactor that changes the header copy,
saved-state delimiter, or tool hint will fail loudly.
"""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from pinky_daemon.wake_prompt import (
    DEFAULT_TIMEZONE,
    WakePromptInput,
    WakeReason,
    build_wake_prompt,
)

# A deterministic ``now`` used across tests so header formatting is
# stable. Picks a wall-clock minute (no seconds rendered) and a known
# tz so the ``%Z`` rendering is consistent.
FIXED_PST = datetime(2026, 5, 18, 14, 30, tzinfo=ZoneInfo("America/Los_Angeles"))
EXPECTED_TIME_STR = "Monday, May 18, 2026 at 2:30 PM PDT"


def _input(reason: WakeReason, context_body: str = "") -> WakePromptInput:
    return WakePromptInput(
        reason=reason,
        context_body=context_body,
        timezone="America/Los_Angeles",
        now=FIXED_PST,
    )


class TestHeaderPerReason:
    """Each ``WakeReason`` produces a distinct header + lead sentence."""

    def test_new_session_header(self):
        out = build_wake_prompt(_input(WakeReason.NEW_SESSION))
        assert out.startswith(f"New session started. Current time: {EXPECTED_TIME_STR}.")
        assert "You're connected via Pinky's message broker." in out

    def test_resume_header_is_neutral(self):
        """RESUME copy should NOT claim 'after daemon restart' — that
        wording was wrong for tmux warm reconnect and idle-wake. Callers
        with explicit daemon-restart knowledge should encode it in
        ``context_body`` instead."""
        out = build_wake_prompt(_input(WakeReason.RESUME))
        assert out.startswith(f"Session resumed. Current time: {EXPECTED_TIME_STR}.")
        assert "Pick up where you left off." in out
        assert "after daemon restart" not in out

    def test_context_restart_header(self):
        out = build_wake_prompt(_input(WakeReason.CONTEXT_RESTART))
        assert out.startswith(f"Context restarted. Current time: {EXPECTED_TIME_STR}.")
        assert "Fresh context — pick up from saved state." in out

    def test_auto_restart_header(self):
        out = build_wake_prompt(_input(WakeReason.AUTO_RESTART))
        assert out.startswith(f"Auto-restarted (context limit). Current time: {EXPECTED_TIME_STR}.")
        assert "Context was getting full — pick up from saved state." in out

    def test_idle_wake_header(self):
        out = build_wake_prompt(_input(WakeReason.IDLE_WAKE))
        assert out.startswith(f"Waking from idle. Current time: {EXPECTED_TIME_STR}.")
        assert "Pick up from saved state." in out


class TestContextBody:
    """The ``context_body`` field is fenced with the standard delimiters."""

    def test_empty_context_omits_saved_state_block(self):
        out = build_wake_prompt(_input(WakeReason.NEW_SESSION, context_body=""))
        assert "── Saved State ──" not in out
        assert "──────────────────" not in out

    def test_context_body_is_fenced(self):
        body = "## Continuation\nYou were working on: X\n\n### Context\nfoo bar"
        out = build_wake_prompt(_input(WakeReason.RESUME, context_body=body))
        assert "── Saved State ──" in out
        assert "──────────────────" in out
        # Body appears verbatim between the fences.
        assert body in out
        # And appears AFTER the header.
        assert out.index("Session resumed.") < out.index("── Saved State ──")
        assert out.index(body) < out.index("──────────────────")

    def test_context_body_preserves_user_markup(self):
        """The body is passed through verbatim — no markdown escaping,
        no truncation, no normalization. Callers are responsible for
        format. This pins that contract."""
        weird = "**bold** and `code` and emoji 😅 and newline:\n\nline2"
        out = build_wake_prompt(_input(WakeReason.NEW_SESSION, context_body=weird))
        assert weird in out


class TestToolHintIsAlwaysPresent:
    """The static tools hint is part of every wake prompt regardless of
    reason. The exact ToolSearch invocation strings are pinned because
    agents key off them during cold start to pre-load deferred tools."""

    @pytest.mark.parametrize("reason", list(WakeReason))
    def test_tools_hint_present(self, reason: WakeReason):
        out = build_wake_prompt(_input(reason))
        # The block heading.
        assert "If your tools are deferred (require ToolSearch before use)" in out
        # The two specific ToolSearch queries the harness scripted us
        # to call on cold start.
        assert "select:mcp__pinky-messaging__send,mcp__pinky-messaging__thread" in out
        assert "select:mcp__pinky-memory__reflect,mcp__pinky-memory__recall" in out
        # The fallback note about Pinky plain-text delivery.
        assert "Pinky may fall back to plain-text delivery" in out

    @pytest.mark.parametrize("reason", list(WakeReason))
    def test_messaging_tools_listed(self, reason: WakeReason):
        out = build_wake_prompt(_input(reason))
        # Explicit outreach tool surface.
        assert "send, thread, react, send_gif, send_voice, send_photo, send_document, broadcast" in out
        # The fundamental Telegram delivery instruction.
        assert "Users will message you through Telegram" in out
        assert "Use send(chat_id, platform, text) for normal responses" in out


class TestTimeAndTimezone:
    """``now`` injection is the test seam; live calls fall back to
    ``datetime.now(ZoneInfo(timezone))``."""

    def test_explicit_now_is_honored(self):
        custom = datetime(2024, 1, 1, 9, 0, tzinfo=ZoneInfo("UTC"))
        out = build_wake_prompt(
            WakePromptInput(
                reason=WakeReason.NEW_SESSION,
                timezone="UTC",
                now=custom,
            )
        )
        # The full formatted line: "Monday, January 1, 2024 at 9:00 AM UTC".
        assert "Monday, January 1, 2024 at 9:00 AM UTC" in out

    def test_timezone_appears_in_header(self):
        """The header uses the timezone's abbreviation (PDT, EST, etc.)."""
        eastern = datetime(2026, 5, 18, 17, 30, tzinfo=ZoneInfo("America/New_York"))
        out = build_wake_prompt(
            WakePromptInput(
                reason=WakeReason.NEW_SESSION,
                timezone="America/New_York",
                now=eastern,
            )
        )
        # %Z renders the tz abbreviation (EDT for May).
        assert "EDT" in out

    def test_invalid_timezone_falls_back_to_utc(self):
        """A broken timezone string should not crash the builder — fall
        back to UTC. This matches the prior inline SDK behavior."""
        out = build_wake_prompt(
            WakePromptInput(
                reason=WakeReason.NEW_SESSION,
                timezone="Not/A/Real/Zone",
                # now=None → builder resolves it; we just check it doesn't
                # raise. UTC fallback path.
            )
        )
        assert "Current time:" in out  # Header still emitted.

    def test_default_timezone_constant_is_pacific(self):
        """Soft pin: the default timezone is Pacific (Brad's). If this
        ever changes, callers that omit the field will see a behavior
        shift — flag it explicitly."""
        assert DEFAULT_TIMEZONE == "America/Los_Angeles"


class TestEnumValues:
    """Enum string values pin the wire-format / instrumentation contract."""

    def test_wake_reason_values(self):
        # These strings appear in instrumentation logs (`wake_prompt_sent`
        # events) and on-wire restart_reason fields. Don't rename without
        # versioning the consumer.
        assert WakeReason.NEW_SESSION.value == "new_session"
        assert WakeReason.RESUME.value == "resume"
        assert WakeReason.CONTEXT_RESTART.value == "context_restart"
        assert WakeReason.AUTO_RESTART.value == "auto_restart"
        assert WakeReason.IDLE_WAKE.value == "idle_wake"


class TestContractRegression:
    """Snapshot-style tests that pin the full assembled string for the
    common cases. If a refactor accidentally drops a section or changes
    a delimiter, these fail loudly.
    """

    def test_new_session_no_context_snapshot(self):
        out = build_wake_prompt(_input(WakeReason.NEW_SESSION))
        # The exact opening, the lead, and the messaging-tools sentence
        # are all present in the right order.
        idx_header = out.index("New session started")
        idx_lead = out.index("You're connected via Pinky's message broker")
        idx_msg = out.index("Users will message you through Telegram")
        idx_hint = out.index("If your tools are deferred")
        idx_fallback = out.index("Pinky may fall back to plain-text")
        assert idx_header < idx_lead < idx_msg < idx_hint < idx_fallback

    def test_context_restart_with_body_snapshot(self):
        body = "Saved: do X\nThen Y"
        out = build_wake_prompt(_input(WakeReason.CONTEXT_RESTART, context_body=body))
        idx_header = out.index("Context restarted")
        idx_saved_open = out.index("── Saved State ──")
        idx_body = out.index(body)
        idx_saved_close = out.index("──────────────────")
        idx_lead = out.index("Fresh context — pick up from saved state")
        assert idx_header < idx_saved_open < idx_body < idx_saved_close < idx_lead
