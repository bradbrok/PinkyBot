"""Bounded conversation history and complete dream prompt pagination."""

from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from pinky_daemon import dream_runner as dream_module
from pinky_daemon.api import create_api
from pinky_daemon.claude_runner import RunResult
from pinky_daemon.conversation_store import ConversationStore
from pinky_daemon.dream_runner import DreamRunner


def _insert_rows(store, rows, session_id="reader-main"):
    with store._conn:
        store._conn.executemany(
            "INSERT INTO messages (session_id, role, content, timestamp) VALUES (?, ?, ?, ?)",
            [(session_id, "user", content, timestamp) for timestamp, content in rows],
        )


def _store_provider(store):
    def provider(agent_name, after_ts, limit, role, before_ts=0.0):
        return [
            message.to_dict()
            for message in store.get_messages_for_sessions(
                [f"{agent_name}-main"],
                after_ts=after_ts,
                before_ts=before_ts,
                limit=limit,
                role=role,
            )
        ]

    return provider


def _line(timestamp, content):
    label = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
    return f"[{label}] [user] {content}"


def _interval_rows():
    return [(timestamp, f"interval-{timestamp}") for timestamp in (120, 140, 160, 180, 200)]


def _later_rows():
    return [(300 + index, f"later-{index:04d}") for index in range(1200)]


@pytest.fixture
def history_store(tmp_path):
    store = ConversationStore(db_path=str(tmp_path / "conversations.db"))
    try:
        yield store
    finally:
        store.close()


def _capture_prompt(monkeypatch, runner):
    prompts = []

    class CapturingRunner:
        def __init__(self, config, agent_name=""):
            self.config = config

        async def run(self, prompt):
            prompts.append(prompt)
            return RunResult(output="Consolidated.", exit_code=0)

    monkeypatch.setattr(dream_module, "SDKRunner", CapturingRunner)
    monkeypatch.setattr(runner, "_write_dream_mcp_config", lambda *args: None)
    monkeypatch.setattr(runner, "_reflection_ids_for_attempt", lambda *args, **kwargs: [])
    for method in (
        "_build_memory_links",
        "_extract_user_profiles",
        "_extract_user_relationships",
        "_extract_proposed_skills",
        "_extract_kg_from_dream_output",
        "_extract_kg_triples",
    ):
        monkeypatch.setattr(runner, method, lambda *args, **kwargs: 0)
    monkeypatch.setattr(runner, "_surface_kg_insights", lambda *args, **kwargs: "")
    return prompts


def _history_block(prompt):
    return prompt.split("<conversation_history>\n", 1)[1].split("\n</conversation_history>", 1)[0]


def test_retry_interval_ignores_later_messages_and_excludes_start(history_store, tmp_path):
    rows = _interval_rows()
    _insert_rows(history_store, [(100, "at-lower-bound"), *rows, *_later_rows()])
    runner = DreamRunner(
        db_path=str(tmp_path / "dream.db"), history_provider=_store_provider(history_store)
    )
    try:
        lines, watermark = runner._fetch_unprocessed_history(
            "reader", after_ts=100.0, through_ts=200.0
        )
        assert lines == [_line(timestamp, content) for timestamp, content in rows]
        assert watermark == 200.0
    finally:
        runner.close()


def test_api_provider_forwards_history_upper_bound(tmp_path):
    app = create_api(default_working_dir=str(tmp_path), db_path=str(tmp_path / "api.db"))
    try:
        app.state.agents.register("reader", working_dir=str(tmp_path / "reader"))
        _insert_rows(app.state.conversation_store, [*_interval_rows(), *_later_rows()])
        messages = app.state.dream_runner._history_provider(
            "reader", 100.0, 1000, "", before_ts=200.0
        )
        assert [message["content"] for message in messages] == [
            content for _, content in reversed(_interval_rows())
        ]
        assert [message["timestamp"] for message in messages] == [200, 180, 160, 140, 120]
    finally:
        app.state.dream_runner.close()
        app.state.conversation_store.close()
        app.state.agents.close()
        app.state.store_catalog.close()


@pytest.mark.asyncio
async def test_normal_run_includes_all_messages_and_newest_watermark(
    history_store, tmp_path, monkeypatch
):
    rows = [(101 + index, f"message-{index:04d}") for index in range(2500)]
    _insert_rows(history_store, rows)
    runner = DreamRunner(
        db_path=str(tmp_path / "dream.db"), history_provider=_store_provider(history_store)
    )
    try:
        runner._save_state("reader", "seed", last_message_ts=100.0)
        prompts = _capture_prompt(monkeypatch, runner)
        result = await runner.run_dream("reader", SimpleNamespace(working_dir=str(tmp_path)))
        assert result == "Consolidated."
        assert len(prompts) == 1
        block = _history_block(prompts[0])
        assert block.splitlines() == [_line(timestamp, content) for timestamp, content in rows]
        assert "...(truncated older messages)" not in block
        assert runner._get_last_message_ts("reader") == 2600.0
    finally:
        runner.close()


@pytest.mark.parametrize("extra_chars", [0, 1000], ids=["exact-page-cap", "smaller-cap"])
@pytest.mark.asyncio
async def test_normal_run_crosses_cap_before_truncating_newest_history(
    history_store, tmp_path, monkeypatch, extra_chars
):
    rows = [(101 + index, f"message-{index:04d}") for index in range(2500)]
    _insert_rows(history_store, rows)
    expected_lines = [_line(timestamp, content) for timestamp, content in rows]
    cap = len("\n".join(expected_lines[-1000:])) + extra_chars
    monkeypatch.setattr(dream_module, "_MAX_HISTORY_CHARS", cap)
    runner = DreamRunner(
        db_path=str(tmp_path / "dream.db"), history_provider=_store_provider(history_store)
    )
    try:
        runner._save_state("reader", "seed", last_message_ts=100.0)
        lines, newest = runner._fetch_unprocessed_history("reader", after_ts=100.0)
        assert 1000 < len(lines) < len(rows)
        assert len("\n".join(lines)) > cap
        assert newest == 2600.0
        prompts = _capture_prompt(monkeypatch, runner)
        result = await runner.run_dream("reader", SimpleNamespace(working_dir=str(tmp_path)))
        assert result == "Consolidated."
        assert len(prompts) == 1
        expected_block = "...(truncated older messages)\n" + "\n".join(expected_lines)[-cap:]
        assert _history_block(prompts[0]) == expected_block
        assert "message-2499" in _history_block(prompts[0])
        assert "message-0000" not in _history_block(prompts[0])
        assert runner._get_last_message_ts("reader") == 2600.0
    finally:
        runner.close()


@pytest.mark.parametrize("tied_count", [3, 1005], ids=["page-edge", "larger-than-page"])
def test_equal_timestamp_page_boundary_has_no_duplicates_or_gaps(
    history_store, tmp_path, tied_count
):
    rows = [
        *[(900 + index, f"older-{index}") for index in range(5)],
        *[(999, f"tied-{index:04d}") for index in range(tied_count)],
        *[(1000 + index, f"newer-{index:04d}") for index in range(999)],
    ]
    _insert_rows(history_store, rows)
    runner = DreamRunner(
        db_path=str(tmp_path / "dream.db"), history_provider=_store_provider(history_store)
    )
    try:
        lines, watermark = runner._fetch_unprocessed_history(
            "reader", after_ts=100.0, through_ts=1998.0
        )
        expected = [_line(timestamp, content) for timestamp, content in rows]
        assert Counter(lines) == Counter(expected)
        assert len(lines) == len(rows)
        assert lines[:5] == expected[:5]
        assert lines[-999:] == expected[-999:]
        assert watermark == 1998.0
    finally:
        runner.close()


@pytest.mark.asyncio
async def test_pending_recovery_keeps_original_history_upper_bound(
    history_store, tmp_path, monkeypatch
):
    rows = _interval_rows()
    _insert_rows(history_store, [*rows, *_later_rows()])
    runner = DreamRunner(
        db_path=str(tmp_path / "dream.db"), history_provider=_store_provider(history_store)
    )
    try:
        runner._save_state("reader", "seed", last_message_ts=100.0)
        receipt = runner._begin_reflection_attempt(
            "reader",
            datetime.now(timezone.utc).date().isoformat(),
            dream_start=datetime.now(timezone.utc),
            history_start_ts=100.0,
            history_end_ts=200.0,
        )
        assert receipt is not None
        with runner._db:
            runner._db.execute("UPDATE dream_reflection_attempts SET lease_expires_at=0")
        prompts = _capture_prompt(monkeypatch, runner)
        await runner.run_dream("reader", SimpleNamespace(working_dir=str(tmp_path)))
        assert runner._get_last_message_ts("reader") == 200.0
        assert len(prompts) == 1
        assert _history_block(prompts[0]).splitlines() == [
            _line(timestamp, content) for timestamp, content in rows
        ]
        assert runner._db.execute(
            "SELECT status, attempt_n, history_start_ts, history_end_ts "
            "FROM dream_reflection_attempts"
        ).fetchall() == [("completed", 1, 100.0, 200.0)]
    finally:
        runner.close()
