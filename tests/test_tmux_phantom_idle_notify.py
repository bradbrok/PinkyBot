"""Phantom cleanup reports idle only after all pending work has drained."""

import json
from unittest.mock import MagicMock

import pytest

from tests.test_tmux_fold_remove_consumption import (
    _bind_ticket,
    _enable_fast_idle_reconcile,
    _make_session,
    _run_until_reconciled,
    _seed_inflight,
)


@pytest.mark.parametrize("consumed,deferred,notifications", [
    (True, False, 1), (False, False, 0), (True, True, 0),
])
async def test_phantom_cleanup_notifies_only_when_ready(
    tmp_path, monkeypatch, consumed, deferred, notifications,
):
    session = _make_session()
    notify = MagicMock()
    session._config.on_turn_idle = notify
    session._defer_scheduler_idle_notify = deferred
    transcript = tmp_path / "transcript.jsonl"
    transcript.write_text('{"type":"system"}\n')
    session._tailer = MagicMock()
    session._tailer.transcript_path = transcript
    entry = _seed_inflight(session, prompt="work")
    _bind_ticket(entry, transcript)
    if consumed:
        with transcript.open("a") as stream:
            stream.write(json.dumps({"type": "user", "message": {"content": "work"}}) + "\n")
    _enable_fast_idle_reconcile(monkeypatch, session)
    await _run_until_reconciled(session)
    assert not session._inflight_metas and session._inflight_turn is None
    assert notify.call_count == notifications
    if consumed:
        assert session._message_queue.empty()
    else:
        assert session._message_queue.qsize() == 1
        assert session._message_queue.get_nowait() is entry.turn
