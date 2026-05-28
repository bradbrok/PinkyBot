"""Tests for the auth-failure tracker and operator-alert wiring."""
from __future__ import annotations

from types import SimpleNamespace

from pinky_daemon.auth_alerts import (
    POISONED_TRANSCRIPT_ERROR_TYPE,
    TRANSPORT_FAILURE_POLICIES,
    AuthFailureTracker,
    PoisonedTranscriptTracker,
    classify_stop_failure,
    format_alert_message,
    resolve_operator_chat,
)
from pinky_daemon.streaming_session import (
    _AUTH_ASSISTANT_ERROR,
    _is_auth_error_assistant,
    _is_auth_error_result,
)

# ── SDK Literal invariant ────────────────────────────────────────
#
# ``_is_auth_error_assistant`` does an exact string match against
# AssistantMessageError. If a future SDK rename of the Literal value
# happens unnoticed, exact-match silently stops detecting credential
# failures — re-creating the exact regression mode #400 was built to
# catch. This test fails loud at CI time on any SDK bump that drops
# or renames "authentication_failed".


def test_auth_assistant_error_literal_present_in_sdk_type():
    """``_AUTH_ASSISTANT_ERROR`` must remain a member of
    ``AssistantMessageError.__args__``. If the SDK renames the Literal,
    update both the constant and ``_is_auth_error_assistant``."""
    from claude_agent_sdk.types import AssistantMessageError

    assert _AUTH_ASSISTANT_ERROR in AssistantMessageError.__args__, (
        f"claude-agent-sdk renamed the AssistantMessageError Literal — "
        f"{_AUTH_ASSISTANT_ERROR!r} no longer in {AssistantMessageError.__args__}. "
        f"Update _AUTH_ASSISTANT_ERROR in streaming_session.py."
    )

# ── _is_auth_error_assistant ──────────────────────────────────────
#
# AssistantMessage.error is the AssistantMessageError Literal:
#   authentication_failed, billing_error, rate_limit, invalid_request,
#   server_error, unknown
# Only "authentication_failed" should trip the operator alert.


def _msg(error=None):
    """Lightweight AssistantMessage stand-in (only the .error attr matters)."""
    return SimpleNamespace(error=error)


def test_is_auth_error_assistant_true_only_for_authentication_failed():
    assert _is_auth_error_assistant(_msg("authentication_failed")) is True


def test_is_auth_error_assistant_false_for_other_literal_values():
    # Other AssistantMessageError values are real errors but NOT credential
    # failures. Alerting the operator on these would be noise (or actively
    # wrong, in the case of billing/rate_limit issues that have nothing to
    # do with credentials).
    for not_auth in (
        "billing_error",
        "rate_limit",
        "invalid_request",
        "server_error",
        "unknown",
    ):
        assert _is_auth_error_assistant(_msg(not_auth)) is False, not_auth


def test_is_auth_error_assistant_false_for_none_and_missing():
    assert _is_auth_error_assistant(_msg(None)) is False
    # Missing attribute (e.g. unrelated message type) must not raise.
    assert _is_auth_error_assistant(SimpleNamespace()) is False


# ── _is_auth_error_result ─────────────────────────────────────────
#
# ResultMessage.api_error_status is int|None — raw HTTP status from a
# failing API call. 401/403 are credential failures; 429 and 5xx are
# transient/operational and must not trip the auth alert.


def _result(api_error_status=None):
    """Lightweight ResultMessage stand-in."""
    return SimpleNamespace(api_error_status=api_error_status)


def test_is_auth_error_result_true_for_401_and_403():
    assert _is_auth_error_result(_result(401)) is True
    assert _is_auth_error_result(_result(403)) is True


def test_is_auth_error_result_false_for_transient_statuses():
    # Rate limit (429) and any 5xx must NOT alert as a credential failure.
    for not_auth_status in (429, 500, 502, 503, 504, 529):
        assert _is_auth_error_result(_result(not_auth_status)) is False, not_auth_status


def test_is_auth_error_result_false_for_none_and_success():
    assert _is_auth_error_result(_result(None)) is False
    assert _is_auth_error_result(_result(200)) is False
    # Missing attribute (older SDK / non-result objects) must not raise.
    assert _is_auth_error_result(SimpleNamespace()) is False


# ── AuthFailureTracker ────────────────────────────────────────────


class _FakeClock:
    def __init__(self, start: float = 1_000_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float) -> None:
        self.now += seconds


def _make_tracker(**kw):
    clock = _FakeClock()
    tracker = AuthFailureTracker(
        fail_window_seconds=kw.get("window", 300),
        fail_threshold=kw.get("threshold", 3),
        alert_cooldown_seconds=kw.get("cooldown", 1800),
        clock=clock,
    )
    return tracker, clock


def test_below_threshold_does_not_alert():
    tracker, _ = _make_tracker()
    d = tracker.record_failure("sasha", "authentication_failed")
    assert d["should_alert"] is False
    assert d["count"] == 1
    assert d["reason"] == "below_threshold"


def test_threshold_for_single_agent_fires_alert():
    tracker, clock = _make_tracker(threshold=3)
    for _ in range(2):
        clock.tick(10)
        d = tracker.record_failure("sasha", "authentication_failed")
        assert d["should_alert"] is False
    clock.tick(10)
    d = tracker.record_failure("sasha", "authentication_failed")
    assert d["should_alert"] is True
    assert d["reason"] == "agent_repeated"
    assert d["count"] == 3
    # Decision alone must not commit cooldown — caller hasn't delivered yet.
    assert tracker._last_alert_at == 0.0
    assert tracker._alert_count == 0


def test_host_wide_outage_fires_immediately_at_threshold_agents():
    """If 3 different agents fail, alert on the 3rd — don't wait for any
    single agent to hit the per-agent threshold."""
    tracker, _ = _make_tracker(threshold=3)
    assert tracker.record_failure("sasha", "auth")["should_alert"] is False
    assert tracker.record_failure("ivan", "auth")["should_alert"] is False
    d = tracker.record_failure("kuzya", "auth")
    assert d["should_alert"] is True
    assert d["reason"] == "host_wide"
    assert d["agents_failing"] == 3


def test_cooldown_blocks_repeat_alerts():
    tracker, clock = _make_tracker(threshold=3, cooldown=600)
    # Drive past threshold once and simulate successful delivery.
    for _ in range(3):
        tracker.record_failure("sasha", "auth")
    tracker.commit_alert()
    # Next failure within cooldown — must not realert.
    clock.tick(60)
    d = tracker.record_failure("sasha", "auth")
    assert d["should_alert"] is False
    assert d["reason"] == "cooldown"
    assert d["cooldown_remaining"] > 0


def test_cooldown_elapses_then_alert_fires_again():
    """During a continuous outage we want a re-alert each cooldown period.

    Use a window long enough that failures stay live across the cooldown gap
    — that's the realistic outage shape (agent retries every minute or so).
    """
    tracker, clock = _make_tracker(threshold=3, window=3600, cooldown=600)
    for _ in range(3):
        tracker.record_failure("sasha", "auth")  # first alert decision here
    tracker.commit_alert()  # simulate first delivery succeeded
    clock.tick(601)  # past cooldown, still inside window
    d = tracker.record_failure("sasha", "auth")
    assert d["should_alert"] is True
    assert d["count"] >= 3


def test_failed_delivery_preserves_cooldown_and_retries_next_failure():
    """Regression: a broker_send raise must NOT advance the cooldown.

    Pushok review on PR #400: the original code mutated _last_alert_at
    *before* the await, so a network blip on the very first send-attempt
    silenced the alert system for 30 minutes — the exact regression this
    feature exists to prevent (Sasha-down-9-12h).
    """
    tracker, clock = _make_tracker(threshold=3, cooldown=600)
    # First three failures cross threshold; caller "tries to deliver" but
    # the broker raises, so commit_alert() is NEVER called.
    for _ in range(3):
        d = tracker.record_failure("sasha", "auth")
    assert d["should_alert"] is True
    # No commit_alert() — simulating broker_send failure.
    assert tracker._last_alert_at == 0.0  # cooldown untouched

    # Next failure (1 second later) must still be should_alert: True so the
    # caller can retry delivery.
    clock.tick(1)
    d = tracker.record_failure("sasha", "auth")
    assert d["should_alert"] is True
    assert d["reason"] == "agent_repeated"


def test_commit_alert_advances_cooldown_and_counter():
    tracker, clock = _make_tracker(threshold=3, cooldown=600)
    for _ in range(3):
        tracker.record_failure("sasha", "auth")
    assert tracker._alert_count == 0
    tracker.commit_alert()
    assert tracker._alert_count == 1
    assert tracker._last_alert_at == clock.now


def test_window_eviction_drops_old_failures():
    tracker, clock = _make_tracker(window=60, threshold=3)
    tracker.record_failure("sasha", "auth")
    clock.tick(61)  # first entry now stale
    tracker.record_failure("sasha", "auth")
    clock.tick(1)
    d = tracker.record_failure("sasha", "auth")
    # Only 2 fresh entries within window — below threshold.
    assert d["should_alert"] is False
    assert d["count"] == 2


def test_record_success_clears_agent_failures():
    tracker, _ = _make_tracker(threshold=3)
    tracker.record_failure("sasha", "auth")
    tracker.record_failure("sasha", "auth")
    tracker.record_success("sasha")
    d = tracker.record_failure("sasha", "auth")
    assert d["count"] == 1
    assert d["should_alert"] is False


def test_status_reports_ok_when_no_failures():
    tracker, _ = _make_tracker()
    s = tracker.status()
    assert s["status"] == "ok"
    assert s["agents_failing"] == []
    assert s["alerts_sent"] == 0


def test_status_reports_broken_at_or_above_threshold():
    tracker, _ = _make_tracker(threshold=3)
    for _ in range(3):
        tracker.record_failure("sasha", "auth")
    tracker.commit_alert()  # simulate delivery succeeded
    s = tracker.status()
    assert s["status"] == "broken"
    assert s["alerts_sent"] == 1
    assert any(a["agent"] == "sasha" for a in s["agents_failing"])


def test_status_reports_degraded_below_threshold():
    tracker, _ = _make_tracker(threshold=3)
    tracker.record_failure("sasha", "auth")
    s = tracker.status()
    assert s["status"] == "degraded"


# ── resolve_operator_chat ─────────────────────────────────────────


def test_resolve_operator_chat_prefers_setting():
    settings = {"operator_chat_id": "12345", "operator_platform": "telegram"}
    chat_id, platform = resolve_operator_chat(
        get_setting=lambda k, default="": settings.get(k, default),
        list_all_approved_users=lambda: [],
    )
    assert chat_id == "12345"
    assert platform == "telegram"


def test_resolve_operator_chat_falls_back_to_most_common_approved_user():
    users = [
        {"chat_id": "111", "agent_name": "a"},
        {"chat_id": "222", "agent_name": "a"},
        {"chat_id": "111", "agent_name": "b"},
        {"chat_id": "111", "agent_name": "c"},
    ]
    chat_id, platform = resolve_operator_chat(
        get_setting=lambda k, default="": "",
        list_all_approved_users=lambda: users,
    )
    assert chat_id == "111"  # appears 3× → operator
    assert platform == "telegram"


def test_resolve_operator_chat_returns_empty_when_nothing_known():
    chat_id, platform = resolve_operator_chat(
        get_setting=lambda k, default="": "",
        list_all_approved_users=lambda: [],
    )
    assert chat_id == ""
    assert platform == ""


def test_resolve_operator_chat_tolerates_setting_errors():
    def bad_setting(*_a, **_kw):
        raise RuntimeError("db down")

    chat_id, platform = resolve_operator_chat(
        get_setting=bad_setting,
        list_all_approved_users=lambda: [{"chat_id": "999"}],
    )
    assert chat_id == "999"  # fell through to fallback
    assert platform == "telegram"


# ── format_alert_message ──────────────────────────────────────────


def test_format_alert_includes_agent_when_repeated():
    msg = format_alert_message(
        agent_name="sasha",
        decision={"reason": "agent_repeated", "count": 3, "agents_failing": 1},
        error="authentication_failed",
        host_label="rpi5",
    )
    assert "sasha" in msg
    assert "authentication_failed" in msg
    assert "Re-auth" in msg


def test_format_alert_says_host_wide_when_multiple_agents_fail():
    msg = format_alert_message(
        agent_name="sasha",
        decision={"reason": "host_wide", "count": 1, "agents_failing": 3},
        error="invalid_api_key",
        host_label="rpi5",
    )
    assert "rpi5" in msg
    assert "3 agent" in msg


def test_format_alert_truncates_long_error_strings():
    long = "x" * 5000
    msg = format_alert_message(
        agent_name="sasha",
        decision={"reason": "agent_repeated", "count": 3, "agents_failing": 1},
        error=long,
        host_label="",
    )
    # Last error block is capped at 200 chars.
    assert "x" * 5000 not in msg


def test_format_alert_uses_no_markdown_so_plain_text_renders_clean():
    """Pushok review: _broker_send defaults to no parse_mode, so any markdown
    in the message renders literally on Telegram (operator sees `*sasha*`).
    Keep the message plain text.
    """
    msg = format_alert_message(
        agent_name="sasha",
        decision={"reason": "agent_repeated", "count": 3, "agents_failing": 1},
        error="authentication_failed",
        host_label="rpi5",
    )
    assert "*" not in msg
    assert "`" not in msg
    # Sanity: still actually informative.
    assert "sasha" in msg
    assert "authentication_failed" in msg


# ── /admin/watchdog integration ───────────────────────────────────


def test_admin_watchdog_exposes_auth_status(tmp_path):
    """End-to-end: /admin/watchdog includes auth_status with the right shape."""
    from fastapi.testclient import TestClient

    from pinky_daemon.api import create_api

    api = create_api(db_path=str(tmp_path / "memory.db"))
    with TestClient(api) as client:
        resp = client.get("/admin/watchdog")
    assert resp.status_code == 200
    body = resp.json()
    assert "auth_status" in body
    auth = body["auth_status"]
    assert auth["status"] == "ok"  # no failures yet
    assert auth["fail_threshold"] >= 1
    assert auth["agents_failing"] == []
    assert "fail_window_seconds" in auth
    assert "alert_cooldown_seconds" in auth


# ── #104: non-auth class alerting (billing / rate_limit) ──────────


def test_format_alert_default_problem_remedy_is_auth_verbatim():
    """The auth path must be byte-for-byte unchanged: defaults reproduce the
    original "Claude auth broken" + "Re-auth needed" wording."""
    msg = format_alert_message(
        agent_name="sasha",
        decision={"reason": "agent_repeated", "count": 3, "agents_failing": 1},
        error="authentication_failed",
        host_label="rpi5",
    )
    assert "Claude auth broken for sasha" in msg
    assert "Re-auth needed" in msg


def test_format_alert_billing_problem_and_remedy():
    pol = TRANSPORT_FAILURE_POLICIES["billing_error"]
    msg = format_alert_message(
        agent_name="sasha",
        decision={"reason": "agent_repeated", "count": 2, "agents_failing": 1},
        error="billing_error",
        host_label="rpi5",
        problem=pol.problem,
        remedy=pol.remedy,
    )
    assert "Claude billing blocked for sasha" in msg
    assert "billing" in msg.lower()
    assert "Re-auth needed" not in msg  # billing has a DIFFERENT remedy
    assert "*" not in msg and "`" not in msg  # still plain text


def test_format_alert_rate_limit_problem_and_remedy():
    pol = TRANSPORT_FAILURE_POLICIES["rate_limit"]
    msg = format_alert_message(
        agent_name="sasha",
        decision={"reason": "host_wide", "count": 1, "agents_failing": 3},
        error="rate_limit",
        host_label="rpi5",
        problem=pol.problem,
        remedy=pol.remedy,
    )
    assert "Claude rate-limited on rpi5" in msg
    assert "3 agent" in msg
    assert "rate-limit" in msg.lower()
    assert "Re-auth needed" not in msg  # rate_limit has a DIFFERENT remedy


def test_transport_failure_policies_shape():
    """Policy invariants: billing alerts faster than rate_limit; rate_limit is
    quieter (longer cooldown); auth is NOT in this map; messaging is set."""
    assert set(TRANSPORT_FAILURE_POLICIES) == {"billing_error", "rate_limit"}
    assert "authentication_failed" not in TRANSPORT_FAILURE_POLICIES
    billing = TRANSPORT_FAILURE_POLICIES["billing_error"]
    rate = TRANSPORT_FAILURE_POLICIES["rate_limit"]
    # billing won't self-resolve → lower threshold than transient rate_limit
    assert billing.fail_threshold < rate.fail_threshold
    # rate_limit is often transient → quieter (longer cooldown)
    assert rate.alert_cooldown_seconds >= billing.alert_cooldown_seconds
    for pol in (billing, rate):
        assert pol.problem and pol.remedy
        assert pol.fail_threshold >= 1
        assert pol.fail_window_seconds > 0
    assert billing.problem != rate.problem


def test_admin_watchdog_exposes_transport_alert_status(tmp_path):
    """/admin/watchdog includes per-class transport alert trackers (#104)."""
    from fastapi.testclient import TestClient

    from pinky_daemon.api import create_api

    api = create_api(db_path=str(tmp_path / "memory.db"))
    with TestClient(api) as client:
        resp = client.get("/admin/watchdog")
    assert resp.status_code == 200
    body = resp.json()
    assert "transport_alert_status" in body
    tstatus = body["transport_alert_status"]
    assert set(tstatus) == {"billing_error", "rate_limit"}
    for et, st in tstatus.items():
        assert st["status"] == "ok"  # no failures yet
        assert st["agents_failing"] == []


# ── classify_stop_failure (poisoned-transcript signature) ───────────

# Verbatim from production: the Anthropic 400 that wedged Barsik.
_REAL_POISON_MSG = (
    'API Error: 400 {"type":"error","error":{"type":"invalid_request_error",'
    '"message":"messages.5.content.70: `thinking` or `redacted_thinking` '
    "blocks in the latest assistant message cannot be modified. These blocks "
    'must remain as they were in the original response."}}'
)


def test_classify_detects_real_poison_message():
    """The exact production 400 → poisoned_transcript signature."""
    et, sig = classify_stop_failure("unknown", _REAL_POISON_MSG)
    assert et == POISONED_TRANSCRIPT_ERROR_TYPE
    assert sig is True


def test_classify_detects_must_remain_variant():
    et, sig = classify_stop_failure(
        "invalid_request",
        "thinking blocks must remain as they were in the original response",
    )
    assert et == POISONED_TRANSCRIPT_ERROR_TYPE
    assert sig is True


def test_classify_passes_through_non_poison():
    """Auth/billing/rate_limit are untouched — signature only matches the
    thinking-block wording."""
    assert classify_stop_failure("authentication_failed", "401") == (
        "authentication_failed",
        False,
    )
    assert classify_stop_failure("rate_limit", "429 Too Many Requests") == (
        "rate_limit",
        False,
    )


def test_classify_thinking_word_without_modify_phrase_is_not_poison():
    """A message merely mentioning 'thinking' must NOT trip the signature —
    both halves (the word + the immutability phrase) are required."""
    et, sig = classify_stop_failure("server_error", "still thinking about it")
    assert et == "server_error"
    assert sig is False


def test_classify_empty_defaults_unknown():
    assert classify_stop_failure("", "") == ("unknown", False)
    assert classify_stop_failure(None, None) == ("unknown", False)


# ── PoisonedTranscriptTracker ───────────────────────────────────────


def _poison_tracker(now_ref):
    return PoisonedTranscriptTracker(
        sig_threshold=2,
        loop_threshold=4,
        loop_window_seconds=180,
        reset_cooldown_seconds=600,
        clock=lambda: now_ref[0],
    )


def test_signature_resets_on_second_hit_same_session():
    now = [1000.0]
    tr = _poison_tracker(now)
    d1 = tr.record_failure("barsik", "sess-A", is_signature=True)
    assert d1["should_reset"] is False  # one hit — below threshold
    d2 = tr.record_failure("barsik", "sess-A", is_signature=True)
    assert d2["should_reset"] is True
    assert d2["reason"] == "signature"


def test_signature_streak_resets_when_session_changes():
    now = [1000.0]
    tr = _poison_tracker(now)
    assert tr.record_failure("barsik", "sess-A", is_signature=True)["should_reset"] is False
    # New transcript → streak restarts, so a single hit on B doesn't fire.
    d = tr.record_failure("barsik", "sess-B", is_signature=True)
    assert d["should_reset"] is False
    assert d["sig_count"] == 1


def test_loop_threshold_fires_on_rapid_any_class_failures():
    now = [1000.0]
    tr = _poison_tracker(now)
    decisions = []
    for _ in range(4):
        now[0] += 5  # rapid — well within the 180s window
        decisions.append(
            tr.record_failure("ivan", "sess-X", is_signature=False)
        )
    assert [d["should_reset"] for d in decisions] == [False, False, False, True]
    assert decisions[-1]["reason"] == "loop"


def test_loop_streak_breaks_when_failures_are_spread_out():
    """Sporadic rate_limits across a healthy long session must NOT trigger —
    a gap past the window resets the loop counter."""
    now = [1000.0]
    tr = _poison_tracker(now)
    for _ in range(3):
        now[0] += 5
        tr.record_failure("ivan", "sess-X", is_signature=False)
    now[0] += 200  # gap > 180s window → streak broken
    d = tr.record_failure("ivan", "sess-X", is_signature=False)
    assert d["should_reset"] is False
    assert d["loop_count"] == 1


def test_no_session_id_never_resets():
    now = [1000.0]
    tr = _poison_tracker(now)
    for _ in range(5):
        d = tr.record_failure("barsik", "", is_signature=True)
        assert d["should_reset"] is False
        assert d["reason"] == "no_session_id"


def test_cooldown_blocks_repeat_heal():
    now = [1000.0]
    tr = _poison_tracker(now)
    tr.record_failure("barsik", "sess-A", is_signature=True)
    assert tr.record_failure("barsik", "sess-A", is_signature=True)["should_reset"] is True
    tr.commit_reset("barsik")  # heal landed → cooldown starts
    # Fresh transcript after the heal, immediately poisoned again, but within
    # cooldown → suppressed (no reset storm).
    now[0] += 60
    d1 = tr.record_failure("barsik", "sess-B", is_signature=True)
    d2 = tr.record_failure("barsik", "sess-B", is_signature=True)
    assert d1["should_reset"] is False
    assert d2["should_reset"] is False
    assert d2["reason"] == "cooldown"
    # Past the cooldown → heals again.
    now[0] += 600
    tr.record_failure("barsik", "sess-B", is_signature=True)
    assert tr.record_failure("barsik", "sess-B", is_signature=True)["should_reset"] is True


def test_commit_reset_clears_streak_and_anchor():
    now = [1000.0]
    tr = _poison_tracker(now)
    tr.record_failure("barsik", "sess-A", is_signature=True)
    tr.record_failure("barsik", "sess-A", is_signature=True)
    tr.commit_reset("barsik")
    now[0] += 700  # past cooldown so only the streak-clear is under test
    # Same session_id reappears but the streak was cleared → one hit, no fire.
    d = tr.record_failure("barsik", "sess-A", is_signature=True)
    assert d["should_reset"] is False
    assert d["sig_count"] == 1


def test_separate_agents_track_independently():
    now = [1000.0]
    tr = _poison_tracker(now)
    tr.record_failure("barsik", "s1", is_signature=True)
    # Ivan's first hit must not inherit Barsik's streak.
    d = tr.record_failure("ivan", "s2", is_signature=True)
    assert d["should_reset"] is False
    assert d["sig_count"] == 1
