"""Retry closure preserves history boundaries and atomic state changes."""

from __future__ import annotations

import asyncio
import re
import sqlite3
import threading
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

import pinky_daemon.dream_runner as dream_runner_module
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.dream_runner import DreamRunner

_AGENT = "worker"
_RETRY_NIGHT = "2026-01-01"
_CURRENT_NIGHT = "2026-01-02"


class _InterruptedDream(BaseException):
    """Leave the persisted attempt intact after observing its SDK input."""


@pytest.fixture
def retry_context(tmp_path, monkeypatch):
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    calls = []

    def provider(agent, after_ts, limit, role, before_ts=0.0):
        calls.append((agent, after_ts, limit, role, before_ts))
        messages = store.get_messages_for_sessions(
            ["session"],
            after_ts=after_ts,
            before_ts=before_ts,
            limit=limit,
            role=role,
        )
        return [message.to_dict() for message in messages]

    runner = DreamRunner(
        db_path=str(tmp_path / "dream.db"),
        history_provider=provider,
    )
    prompts = []
    logs = []

    class CapturingSDKRunner:
        def __init__(self, config, agent_name=""):
            pass

        async def run(self, prompt):
            prompts.append(prompt)
            raise _InterruptedDream

    monkeypatch.setattr(dream_runner_module, "SDKRunner", CapturingSDKRunner)
    monkeypatch.setattr(dream_runner_module, "_log", logs.append)
    monkeypatch.setattr(runner, "_resolve_dream_transport", lambda: "sdk")
    monkeypatch.setattr(runner, "_write_dream_mcp_config", lambda *args: None)
    monkeypatch.setattr(runner, "_night_key", lambda agent_config, dream_start: _CURRENT_NIGHT)
    config = SimpleNamespace(working_dir=str(tmp_path), model="", dream_model="", provider_key="")
    context = SimpleNamespace(
        store=store,
        runner=runner,
        config=config,
        prompts=prompts,
        logs=logs,
        calls=calls,
        db_path=tmp_path / "dream.db",
    )
    try:
        yield context
    finally:
        runner.close()
        store.close()


def _insert_messages(context, *, interval=False, newer=0):
    rows = [("session", "user", "at-start", 100.0)]
    if interval:
        rows.extend(
            ("session", "user", f"interval-{index}", timestamp)
            for index, timestamp in enumerate((120.0, 140.0, 160.0, 180.0, 200.0))
        )
    rows.extend(("session", "user", f"newer-{index}", 201.0 + index) for index in range(newer))
    with context.store._conn:
        context.store._conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            rows,
        )


def _seed_failed_attempts(context, *, watermark=100.0):
    context.runner._save_state(_AGENT, "Initial state", last_message_ts=100.0)
    for _ in range(2):
        attempt = context.runner._begin_reflection_attempt(
            _AGENT,
            _RETRY_NIGHT,
            dream_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
            history_start_ts=100.0,
            history_end_ts=200.0,
        )
        assert attempt is not None
        context.runner._fail_reflection_attempt(attempt, "Interrupted before output")
    context.runner._save_state(_AGENT, "Initial state", last_message_ts=watermark)


async def _run_until_input(context):
    try:
        return await context.runner.run_dream(_AGENT, context.config)
    except _InterruptedDream:
        return None


def _retry_rows(context):
    return context.runner._db.execute(
        """SELECT attempt_n, status, terminal FROM dream_reflection_attempts
           WHERE agent_name=? AND night_key=? ORDER BY attempt_n""",
        (_AGENT, _RETRY_NIGHT),
    ).fetchall()


def _history_lines(prompt):
    return (
        prompt.split("<conversation_history>\n", 1)[1]
        .split("\n</conversation_history>", 1)[0]
        .splitlines()
    )


def _expected_line(timestamp, content):
    stamp = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    return f"[{stamp}] [user] {content}"


@pytest.mark.asyncio
async def test_failed_retry_starts_third_attempt_with_complete_frozen_interval(retry_context):
    context = retry_context
    _seed_failed_attempts(context)
    _insert_messages(context, interval=True, newer=1200)

    await _run_until_input(context)

    pending = context.runner._get_pending_reflection_attempt(_AGENT)
    assert pending is not None, "The frozen interval must begin its third attempt"
    assert (pending.attempt_n, pending.history_start_ts, pending.history_end_ts) == (
        3,
        100.0,
        200.0,
    )
    assert pending.night_key == _RETRY_NIGHT
    assert len(context.prompts) == 1
    assert _history_lines(context.prompts[0]) == [
        _expected_line(timestamp, f"interval-{index}")
        for index, timestamp in enumerate((120.0, 140.0, 160.0, 180.0, 200.0))
    ]
    assert _retry_rows(context) == [(1, "failed", 0), (2, "failed", 0), (3, "pending", 0)]
    assert context.runner._get_last_message_ts(_AGENT) == 100.0


@pytest.mark.asyncio
async def test_empty_retry_closes_all_failed_rows_then_begins_new_history(retry_context):
    context = retry_context
    _seed_failed_attempts(context)
    _insert_messages(context, newer=1200)

    await _run_until_input(context)

    assert _retry_rows(context) == [(1, "failed", 1), (2, "failed", 1)]
    assert context.runner._get_last_message_ts(_AGENT) == 200.0
    assert context.runner._get_retryable_failed_attempt(_AGENT) is None
    pending = context.runner._get_pending_reflection_attempt(_AGENT)
    assert pending is not None, "The same call must begin the newer interval"
    assert pending.night_key == _CURRENT_NIGHT
    assert (pending.history_start_ts, pending.history_end_ts) == (200.0, 1400.0)
    assert len(context.prompts) == 1
    assert _history_lines(context.prompts[0]) == [
        _expected_line(201.0 + index, f"newer-{index}") for index in range(1200)
    ]
    assert (
        context.runner._db.execute(
            "SELECT COUNT(*) FROM dream_reflection_outcomes WHERE agent_name=? AND night_key=?",
            (_AGENT, _RETRY_NIGHT),
        ).fetchone()[0]
        == 0
    )
    warnings = [line for line in context.logs if "WARNING" in line]
    assert any(
        _AGENT in line
        and _RETRY_NIGHT in line
        and re.search(r"\b2\b", line)
        and re.search(r"\b100(?:\.0)?\b", line)
        and re.search(r"\b200(?:\.0)?\b", line)
        for line in warnings
    ), warnings


@pytest.mark.asyncio
@pytest.mark.parametrize("newer", [0, 1], ids=["idle", "new-history"])
async def test_empty_overlapping_retry_never_lowers_watermark(retry_context, newer):
    context = retry_context
    _seed_failed_attempts(context, watermark=150.0)
    _insert_messages(context, newer=newer)

    summary = await _run_until_input(context)

    assert context.runner._get_last_message_ts(_AGENT) >= 200.0
    assert _retry_rows(context) == [(1, "failed", 1), (2, "failed", 1)]
    assert context.runner._get_retryable_failed_attempt(_AGENT) is None
    if newer:
        pending = context.runner._get_pending_reflection_attempt(_AGENT)
        assert pending is not None
        assert (pending.history_start_ts, pending.history_end_ts) == (200.0, 201.0)
    else:
        assert summary == "No new conversation history to process."
        assert context.runner._get_pending_reflection_attempt(_AGENT) is None
        assert context.prompts == []


@pytest.mark.asyncio
async def test_empty_retry_state_write_failure_rolls_back_terminal_flags(
    retry_context, monkeypatch
):
    context = retry_context
    _seed_failed_attempts(context)
    _insert_messages(context)
    original_state = context.runner._db.execute(
        "SELECT * FROM dream_state WHERE agent_name=?", (_AGENT,)
    ).fetchone()
    attempted_watermarks = []
    write_state = context.runner._write_state

    def fail_state_write(agent_name, summary, last_message_ts, *, saved_at):
        attempted_watermarks.append(last_message_ts)
        write_state(agent_name, summary, last_message_ts, saved_at=saved_at)
        with sqlite3.connect(context.db_path) as observer:
            assert observer.execute(
                "SELECT terminal FROM dream_reflection_attempts ORDER BY attempt_n"
            ).fetchall() == [(0,), (0,)]
            assert (
                observer.execute(
                    "SELECT last_message_ts FROM dream_state WHERE agent_name=?", (_AGENT,)
                ).fetchone()[0]
                == 100.0
            )
        raise sqlite3.OperationalError("Injected state write failure")

    monkeypatch.setattr(context.runner, "_write_state", fail_state_write)
    with pytest.raises(sqlite3.OperationalError, match="Injected state write failure"):
        await context.runner.run_dream(_AGENT, context.config)

    assert attempted_watermarks == [200.0]
    assert _retry_rows(context) == [(1, "failed", 0), (2, "failed", 0)]
    assert context.runner._get_last_message_ts(_AGENT) == 100.0
    assert (
        context.runner._db.execute(
            "SELECT * FROM dream_state WHERE agent_name=?", (_AGENT,)
        ).fetchone()
        == original_state
    )
    assert (
        context.runner._db.execute("SELECT COUNT(*) FROM dream_reflection_outcomes").fetchone()[0]
        == 0
    )
    assert context.prompts == []
    with sqlite3.connect(context.db_path) as observer:
        assert observer.execute(
            "SELECT terminal FROM dream_reflection_attempts ORDER BY attempt_n"
        ).fetchall() == [(0,), (0,)]
        assert (
            observer.execute(
                "SELECT last_message_ts FROM dream_state WHERE agent_name=?", (_AGENT,)
            ).fetchone()[0]
            == 100.0
        )


@pytest.mark.asyncio
async def test_empty_retry_closes_only_failed_rows_of_its_agent_and_night(retry_context):
    context = retry_context
    _seed_failed_attempts(context)
    _insert_messages(context)
    with context.runner._db:
        context.runner._db.executemany(
            """INSERT INTO dream_reflection_attempts
               (agent_name, night_key, attempt_n, status, dream_started_at,
                history_start_ts, history_end_ts, correlation_id, terminal)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [
                ("other", _RETRY_NIGHT, 1, "failed", "2026-01-01", 100, 200, "other", 0),
                (_AGENT, "2025-12-31", 1, "failed", "2025-12-31", 50, 99, "older", 0),
                (_AGENT, _RETRY_NIGHT, 3, "completed", "2026-01-01", 100, 200, "done", 0),
                (_AGENT, _RETRY_NIGHT, 4, "failed", "2026-01-01", 100, 200, "closed", 1),
            ],
        )
    untouched = context.runner._db.execute(
        """SELECT * FROM dream_reflection_attempts
           WHERE correlation_id IN ('other', 'older', 'done', 'closed')
           ORDER BY correlation_id"""
    ).fetchall()

    await _run_until_input(context)

    assert _retry_rows(context) == [
        (1, "failed", 1),
        (2, "failed", 1),
        (3, "completed", 0),
        (4, "failed", 1),
    ]
    assert (
        context.runner._db.execute(
            """SELECT * FROM dream_reflection_attempts
           WHERE correlation_id IN ('other', 'older', 'done', 'closed')
           ORDER BY correlation_id"""
        ).fetchall()
        == untouched
    )
    assert context.runner._get_last_message_ts(_AGENT) == 200.0
    assert (
        context.runner._db.execute("SELECT COUNT(*) FROM dream_reflection_outcomes").fetchone()[0]
        == 0
    )


@pytest.mark.asyncio
async def test_empty_retry_preserves_watermark_advanced_during_read(retry_context, monkeypatch):
    """Skip stale reads; the next run starts from moved progress, so nothing is lost."""
    context = retry_context
    _seed_failed_attempts(context, watermark=150.0)
    _insert_messages(context, newer=100)
    provider = context.runner._history_provider
    progressed_state = []

    def advance_once(agent, after_ts, limit, role, before_ts=0.0):
        if not progressed_state:
            context.runner._save_state(agent, "Concurrent progress", last_message_ts=250.0)
            progressed_state.extend(context.runner._db.execute("SELECT * FROM dream_state"))
        return provider(agent, after_ts, limit, role, before_ts=before_ts)

    monkeypatch.setattr(context.runner, "_history_provider", advance_once)
    result = await _run_until_input(context)
    assert result == "Dream skipped: dream progress moved while this run read its history."
    assert context.runner._get_last_message_ts(_AGENT) == 250.0
    assert _retry_rows(context) == [(1, "failed", 0), (2, "failed", 0)]
    assert context.runner._db.execute("SELECT * FROM dream_state").fetchall() == progressed_state
    assert context.runner._get_pending_reflection_attempt(_AGENT) is None
    assert context.runner._db.execute("SELECT * FROM dream_reflection_outcomes").fetchall() == []
    assert context.prompts == []


@pytest.mark.asyncio
@pytest.mark.parametrize("history_path", ["normal", "retry", "recovery"])
@pytest.mark.parametrize("failed_page", [1, 2], ids=["first-page", "later-page"])
async def test_history_read_error_preserves_receipts_and_watermark(
    retry_context, monkeypatch, history_path, failed_page
):
    context = retry_context
    if history_path == "retry":
        _seed_failed_attempts(context)
    else:
        context.runner._save_state(_AGENT, "Initial state", last_message_ts=100.0)
    if history_path == "recovery":
        attempt = context.runner._begin_reflection_attempt(
            _AGENT,
            _RETRY_NIGHT,
            dream_start=datetime.now(timezone.utc),
            history_start_ts=100.0,
            history_end_ts=200.0,
        )
        assert attempt is not None
        with context.runner._db:
            context.runner._db.execute("UPDATE dream_reflection_attempts SET lease_expires_at=0")
        monkeypatch.setattr(context.runner, "_reflection_ids_for_attempt", lambda *a: [])
    with context.store._conn:
        context.store._conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            [("session", "user", f"entry-{index}", 100.05 + index * 0.05) for index in range(1500)],
        )
    state_before = context.runner._db.execute("SELECT * FROM dream_state").fetchall()
    rows_before = context.runner._db.execute(
        "SELECT agent_name, night_key, attempt_n, status, terminal, summary, completed_at "
        "FROM dream_reflection_attempts"
    ).fetchall()
    provider = context.runner._history_provider
    calls = 0

    def fail_page(agent, after_ts, limit, role, before_ts=0.0):
        nonlocal calls
        calls += 1
        if calls == failed_page:
            raise OSError("Injected history read failure")
        return provider(agent, after_ts, limit, role, before_ts=before_ts)

    monkeypatch.setattr(context.runner, "_history_provider", fail_page)
    error = None
    try:
        await _run_until_input(context)
    except OSError as exc:
        error = exc
    assert context.runner._db.execute("SELECT * FROM dream_state").fetchall() == state_before
    assert (
        context.runner._db.execute(
            "SELECT agent_name, night_key, attempt_n, status, terminal, summary, completed_at "
            "FROM dream_reflection_attempts"
        ).fetchall()
        == rows_before
    )
    assert error is not None
    assert str(error) == "Injected history read failure"
    assert context.runner._db.execute("SELECT * FROM dream_reflection_outcomes").fetchall() == []
    assert context.prompts == []
    warnings = [line for line in context.logs if "WARNING" in line]
    assert len(warnings) == 1
    assert _AGENT in warnings[0] and history_path in warnings[0]
    assert "Injected history read failure" in warnings[0]
    if history_path == "recovery":
        pending = context.runner._get_pending_reflection_attempt(_AGENT)
        assert pending is not None and pending.attempt_n == 1
        assert pending.lease_owner == context.runner._lease_owner
        assert pending.lease_expires_at > 0


@pytest.mark.asyncio
@pytest.mark.parametrize("failed_page", [1, 2], ids=["first-page", "later-page"])
async def test_empty_retry_continuation_error_does_not_close_window(
    retry_context, monkeypatch, failed_page
):
    context = retry_context
    _seed_failed_attempts(context, watermark=150.0)
    _insert_messages(context, newer=1500)
    state_before = context.runner._db.execute("SELECT * FROM dream_state").fetchall()
    provider = context.runner._history_provider
    calls = 0
    writes = []
    original_write = context.runner._write_state

    def track_write(*args, **kwargs):
        writes.append((args, kwargs))
        return original_write(*args, **kwargs)

    def fail_continuation(agent, after_ts, limit, role, before_ts=0.0):
        nonlocal calls
        assert not context.runner._db.in_transaction
        calls += 1
        if calls == failed_page + 1:
            raise OSError("Injected continuation read failure")
        return provider(agent, after_ts, limit, role, before_ts=before_ts)

    monkeypatch.setattr(context.runner, "_write_state", track_write)
    monkeypatch.setattr(context.runner, "_history_provider", fail_continuation)
    error = None
    try:
        await _run_until_input(context)
    except OSError as exc:
        error = exc
    assert _retry_rows(context) == [(1, "failed", 0), (2, "failed", 0)]
    assert context.runner._db.execute("SELECT * FROM dream_state").fetchall() == state_before
    assert error is not None and str(error) == "Injected continuation read failure"
    assert writes == []
    assert context.runner._get_pending_reflection_attempt(_AGENT) is None
    assert context.runner._db.execute("SELECT * FROM dream_reflection_outcomes").fetchall() == []
    assert context.prompts == []
    warnings = [line for line in context.logs if "WARNING" in line]
    assert len(warnings) == 1 and "normal" in warnings[0]


_STALE_RESULT = "Dream skipped: dream progress moved while this run read its history."


def _dream_tables(runner):
    """Capture every column, including summaries, times, leases, and health flags."""
    return {
        table: runner._db.execute(f"SELECT * FROM {table} ORDER BY 1, 2").fetchall()
        for table in (
            "dream_state",
            "dream_reflection_attempts",
            "dream_reflection_outcomes",
            "kg_surfaced",
        )
    }


@pytest.fixture
def concurrent_context(tmp_path, monkeypatch):
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    context = SimpleNamespace(
        store=store,
        db_path=tmp_path / "dream.db",
        config=SimpleNamespace(
            working_dir=str(tmp_path), model="", dream_model="", provider_key=""
        ),
        logs=[],
        prompts=[],
        sdk_results=[
            SimpleNamespace(output="Concurrent dream completed.", exit_code=0, error=""),
            SimpleNamespace(output="", exit_code=1, error="Interrupted before output"),
        ],
        calls=[],
    )

    def provider(agent, after_ts, limit, role, before_ts=0.0):
        context.calls.append((agent, after_ts, limit, role, before_ts))
        return [
            message.to_dict()
            for message in store.get_messages_for_sessions(
                ["session"],
                after_ts=after_ts,
                before_ts=before_ts,
                limit=limit,
                role=role,
            )
        ]

    context.provider = provider
    context.runner = DreamRunner(db_path=str(context.db_path), history_provider=provider)
    context.other = DreamRunner(db_path=str(context.db_path), history_provider=provider)

    class FinishingSDKRunner:
        def __init__(self, config, agent_name=""):
            pass

        async def run(self, prompt):
            context.prompts.append(prompt)
            assert context.sdk_results, "An unexpected SDK run consumed another attempt"
            return context.sdk_results.pop(0)

    monkeypatch.setattr(dream_runner_module, "SDKRunner", FinishingSDKRunner)
    monkeypatch.setattr(dream_runner_module, "_log", context.logs.append)
    for runner in (context.runner, context.other):
        monkeypatch.setattr(runner, "_resolve_dream_transport", lambda: "sdk")
        monkeypatch.setattr(runner, "_write_dream_mcp_config", lambda *args: None)
        monkeypatch.setattr(runner, "_night_key", lambda config, start: _CURRENT_NIGHT)
        monkeypatch.setattr(runner, "_build_memory_links", lambda *args, **kwargs: 0)
        monkeypatch.setattr(runner, "_reflection_ids_for_attempt", lambda *args, **kwargs: set())
        monkeypatch.setattr(runner, "_extract_user_profiles", lambda *args, **kwargs: 0)
        monkeypatch.setattr(runner, "_extract_user_relationships", lambda *args, **kwargs: 0)
        monkeypatch.setattr(runner, "_extract_proposed_skills", lambda *args, **kwargs: 0)
        monkeypatch.setattr(runner, "_extract_kg_from_dream_output", lambda *args, **kwargs: 0)
        monkeypatch.setattr(runner, "_extract_kg_triples", lambda *args, **kwargs: 0)
        monkeypatch.setattr(runner, "_surface_kg_insights", lambda *args, **kwargs: "")
    try:
        yield context
    finally:
        context.runner.close()
        context.other.close()
        store.close()


def _seed_one_failed_interval(context, *, watermark=100.0):
    context.runner._save_state(_AGENT, "Initial state", last_message_ts=100.0)
    attempt = context.runner._begin_reflection_attempt(
        _AGENT,
        _RETRY_NIGHT,
        dream_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        history_start_ts=100.0,
        history_end_ts=200.0,
    )
    assert attempt is not None
    context.runner._fail_reflection_attempt(attempt, "Interrupted before output")
    context.runner._save_state(_AGENT, "Initial state", last_message_ts=watermark)
    return attempt


def _complete_other_runner(context):
    """Finish a real second run while the first run is inside its read seam."""
    results = []
    errors = []

    def run_other():
        try:
            results.append(asyncio.run(context.other.run_dream(_AGENT, context.config)))
        except BaseException as exc:
            errors.append(exc)
        finally:
            context.other.close()
            context.store.close()

    worker = threading.Thread(target=run_other, name="dream-contender", daemon=True)
    worker.start()
    worker.join(timeout=10)
    assert not worker.is_alive(), "The competing dream must finish before the stale read resumes"
    if errors:
        raise errors[0]
    assert results == ["Concurrent dream completed."]
    assert context.other._get_last_message_ts(_AGENT) == 250.0
    assert context.other._get_pending_reflection_attempt(_AGENT) is None
    assert context.other._db.execute(
        "SELECT status, history_end_ts FROM dream_reflection_attempts "
        "WHERE agent_name=? AND night_key=?",
        (_AGENT, _CURRENT_NIGHT),
    ).fetchall() == [("completed", 250.0)]
    context.logs.clear()
    return _dream_tables(context.other)


def _append_after_completion(context):
    with context.store._conn:
        context.store._conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            [("session", "user", f"arrived-{index}", 251.0 + index) for index in range(50)],
        )


def _assert_stale_run(context, result, expected, *, path, start, watermark, night):
    assert _dream_tables(context.runner) == expected, "A stale run must not change any dream table"
    assert result == _STALE_RESULT
    assert context.runner._get_last_message_ts(_AGENT) == watermark
    assert context.runner._get_pending_reflection_attempt(_AGENT) is None
    warnings = [line for line in context.logs if "WARNING" in line]
    assert len(warnings) == 1, warnings
    assert _AGENT in warnings[0] and night in warnings[0]
    assert f"path={path}" in warnings[0]
    assert f"start={start}" in warnings[0]
    assert f"watermark={watermark}" in warnings[0]


@pytest.mark.asyncio
async def test_empty_retry_continuation_skips_when_another_runner_completes(
    concurrent_context, monkeypatch
):
    """A real continuation completion makes the first runner's selected history stale."""
    context = concurrent_context
    _seed_one_failed_interval(context)
    _insert_messages(context, newer=50)
    expected = None

    def finish_during_continuation(agent, after_ts, limit, role, before_ts=0.0):
        nonlocal expected
        assert not context.runner._db.in_transaction
        if after_ts == 200.0 and expected is None:
            expected = _complete_other_runner(context)
            _append_after_completion(context)
        return context.provider(agent, after_ts, limit, role, before_ts=before_ts)

    monkeypatch.setattr(context.runner, "_history_provider", finish_during_continuation)
    result = await context.runner.run_dream(_AGENT, context.config)

    assert expected is not None
    _assert_stale_run(
        context,
        result,
        expected,
        path="continuation",
        start=200.0,
        watermark=250.0,
        night=_RETRY_NIGHT,
    )
    assert len(context.prompts) == 1, "Only the competing runner may reach the SDK"
    assert len(_history_lines(context.prompts[0])) == 50
    assert _retry_rows(context) == [(1, "failed", 1)]
    assert context.runner._db.execute(
        "SELECT 1 FROM dream_reflection_attempts WHERE history_end_ts=300"
    ).fetchall() == []


@pytest.mark.asyncio
async def test_normal_history_skips_when_another_runner_completes(concurrent_context, monkeypatch):
    context = concurrent_context
    context.runner._save_state(_AGENT, "Initial state", last_message_ts=100.0)
    _insert_messages(context, newer=50)
    expected = None

    def finish_during_read(agent, after_ts, limit, role, before_ts=0.0):
        nonlocal expected
        assert not context.runner._db.in_transaction
        assert expected is None
        expected = _complete_other_runner(context)
        _append_after_completion(context)
        return context.provider(agent, after_ts, limit, role, before_ts=before_ts)

    monkeypatch.setattr(context.runner, "_history_provider", finish_during_read)
    result = await context.runner.run_dream(_AGENT, context.config)

    assert expected is not None
    _assert_stale_run(
        context,
        result,
        expected,
        path="normal",
        start=100.0,
        watermark=250.0,
        night=_CURRENT_NIGHT,
    )
    assert len(context.prompts) == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("concurrent_change", ["terminal-rows", "covered-end"])
async def test_retry_begin_rechecks_interval_after_concurrent_progress(
    concurrent_context, monkeypatch, concurrent_change
):
    """Both the failed-row predicate and the end watermark must still permit retry."""
    context = concurrent_context
    _seed_one_failed_interval(context)
    _insert_messages(context, interval=True)
    expected = None

    def close_during_read(agent, after_ts, limit, role, before_ts=0.0):
        nonlocal expected
        assert not context.runner._db.in_transaction
        messages = context.provider(agent, after_ts, limit, role, before_ts=before_ts)
        assert expected is None
        if concurrent_change == "terminal-rows":
            with context.other._db:
                context.other._db.execute(
                    "UPDATE dream_reflection_attempts SET terminal=1 "
                    "WHERE agent_name=? AND night_key=? AND status='failed'",
                    (_AGENT, _RETRY_NIGHT),
                )
        else:
            context.other._save_state(_AGENT, "Concurrent progress", last_message_ts=200.0)
        expected = _dream_tables(context.other)
        context.logs.clear()
        return messages

    monkeypatch.setattr(context.runner, "_history_provider", close_during_read)
    result = await context.runner.run_dream(_AGENT, context.config)

    assert expected is not None
    _assert_stale_run(
        context,
        result,
        expected,
        path="retry",
        start=100.0,
        watermark=100.0 if concurrent_change == "terminal-rows" else 200.0,
        night=_RETRY_NIGHT,
    )
    assert context.prompts == []


def test_failed_attempt_preserves_progress_inside_its_retry_interval(concurrent_context):
    context = concurrent_context
    context.runner._save_state(_AGENT, "Initial state", last_message_ts=100.0)
    attempt = context.runner._begin_reflection_attempt(
        _AGENT,
        _RETRY_NIGHT,
        dream_start=datetime(2026, 1, 1, tzinfo=timezone.utc),
        history_start_ts=100.0,
        history_end_ts=200.0,
    )
    assert attempt is not None
    context.other._save_state(_AGENT, "Concurrent progress", last_message_ts=150.0)

    context.runner._fail_reflection_attempt(attempt, "Interrupted before output")

    assert context.runner._get_last_message_ts(_AGENT) == 150.0
    assert _retry_rows(context) == [(1, "failed", 0)]
    retry = context.runner._get_retryable_failed_attempt(_AGENT)
    assert retry is not None
    assert (retry.history_start_ts, retry.history_end_ts) == (100.0, 200.0)
    assert retry.correlation_id == attempt.correlation_id
    assert context.runner._db.execute("SELECT * FROM dream_reflection_outcomes").fetchall() == []


@pytest.mark.asyncio
async def test_retry_with_interior_watermark_reads_full_interval_and_preserves_progress(
    concurrent_context,
):
    context = concurrent_context
    _seed_one_failed_interval(context, watermark=150.0)
    _insert_messages(context, interval=True, newer=100)
    context.sdk_results = [
        SimpleNamespace(output="", exit_code=1, error="Interrupted before output")
    ]

    result = await context.runner.run_dream(_AGENT, context.config)

    assert len(context.prompts) == 1, "An interior watermark must not block a frozen retry"
    assert _history_lines(context.prompts[0]) == [
        _expected_line(timestamp, f"interval-{index}")
        for index, timestamp in enumerate((120.0, 140.0, 160.0, 180.0, 200.0))
    ]
    assert "Dream run failed" in result
    assert context.runner._get_last_message_ts(_AGENT) == 150.0
    assert _retry_rows(context) == [(1, "failed", 0), (2, "failed", 0)]
    assert context.runner._db.execute(
        "SELECT history_start_ts, history_end_ts FROM dream_reflection_attempts "
        "WHERE agent_name=? AND night_key=? ORDER BY attempt_n",
        (_AGENT, _RETRY_NIGHT),
    ).fetchall() == [(100.0, 200.0), (100.0, 200.0)]
    retry = context.runner._get_retryable_failed_attempt(_AGENT)
    assert retry is not None and retry.attempt_n == 2
    assert context.runner._db.execute("SELECT * FROM dream_reflection_outcomes").fetchall() == []


@pytest.mark.asyncio
async def test_idle_read_preserves_another_runners_completed_state(concurrent_context, monkeypatch):
    context = concurrent_context
    context.runner._save_state(_AGENT, "Initial state", last_message_ts=100.0)
    _insert_messages(context)
    monkeypatch.setattr(context.runner, "_night_key", lambda config, start: "2026-01-03")
    expected = None

    def finish_after_empty_read(agent, after_ts, limit, role, before_ts=0.0):
        nonlocal expected
        assert not context.runner._db.in_transaction
        messages = context.provider(agent, after_ts, limit, role, before_ts=before_ts)
        assert expected is None
        _insert_messages(context, newer=50)
        expected = _complete_other_runner(context)
        return messages

    monkeypatch.setattr(context.runner, "_history_provider", finish_after_empty_read)
    result = await context.runner.run_dream(_AGENT, context.config)

    assert expected is not None
    _assert_stale_run(
        context,
        result,
        expected,
        path="normal",
        start=100.0,
        watermark=250.0,
        night="2026-01-03",
    )
    assert len(context.prompts) == 1


def test_begin_holds_write_transaction_before_reads_and_until_insert(concurrent_context):
    context = concurrent_context
    context.runner._save_state(_AGENT, "Initial state", last_message_ts=100.0)
    trace = []
    competing_writes = []
    observer = sqlite3.connect(context.db_path, timeout=0)

    def observe_first_read(statement):
        trace.append((statement.strip(), context.runner._db.in_transaction))
        if statement.lstrip().upper().startswith("SELECT") and not competing_writes:
            try:
                observer.execute(
                    "UPDATE dream_state SET last_message_ts=150 WHERE agent_name=?", (_AGENT,)
                )
                observer.commit()
                competing_writes.append("committed")
            except sqlite3.OperationalError as exc:
                observer.rollback()
                competing_writes.append("blocked" if "locked" in str(exc) else str(exc))

    context.runner._db.set_trace_callback(observe_first_read)
    try:
        attempt = context.runner._begin_reflection_attempt(
            _AGENT,
            _CURRENT_NIGHT,
            dream_start=datetime.now(timezone.utc),
            history_start_ts=100.0,
            history_end_ts=200.0,
        )
    finally:
        context.runner._db.set_trace_callback(None)
        observer.close()

    assert attempt is not None
    first_read = next(index for index, (sql, _) in enumerate(trace) if sql.startswith("SELECT"))
    assert trace[first_read][1], trace
    assert any(sql.upper().startswith("BEGIN") for sql, _ in trace[:first_read]), trace
    assert competing_writes == ["blocked"], trace
    assert context.runner._get_last_message_ts(_AGENT) == 100.0
    assert context.runner._db.execute(
        "SELECT status, history_start_ts, history_end_ts FROM dream_reflection_attempts"
    ).fetchall() == [("pending", 100.0, 200.0)]
