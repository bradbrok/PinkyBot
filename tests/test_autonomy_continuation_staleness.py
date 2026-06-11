"""Tests for the continuation-staleness guard (#732).

Autonomy event-wakes fire into LIVE sessions, where the conversation is the
freshest record. A saved-context snapshot older than the agent's last
conversation activity must collapse to a one-line breadcrumb instead of
replaying the full task/context/notes block (and, critically, instead of
replaying a stale wake_action that could double-fire completed work).

Pinned behaviors:
- ``mark_stale_context``: strictly-newer message activity (beyond slack)
  flags the snapshot; missing timestamps on either side leave it alone.
- ``build_wake_prompt``: stale context renders the STALE one-liner only;
  fresh context renders the full block with an age stamp.
- ``ConversationStore.last_message_time``: prefix-scoped MAX(timestamp),
  None when the agent has no messages.
"""

from __future__ import annotations

import os
import tempfile
import time

import pytest

from pinky_daemon.autonomy import build_wake_prompt, mark_stale_context
from pinky_daemon.conversation_store import ConversationStore


@pytest.fixture
def convos():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    store = ConversationStore(db_path=path)
    yield store
    store.close()
    os.unlink(path)


def _ctx(saved_at: float) -> dict:
    return {
        "task": "deploy release 26.06.125",
        "context": "long context body",
        "notes": "some notes",
        "wake_action": "deploy X then report",
        "updated_at": saved_at,
    }


class TestMarkStaleContext:
    def test_newer_message_flags_stale(self):
        ctx = _ctx(saved_at=1000.0)
        out = mark_stale_context(ctx, last_message_time=1100.0)
        assert out["stale"] is True

    def test_message_within_slack_not_stale(self):
        ctx = _ctx(saved_at=1000.0)
        out = mark_stale_context(ctx, last_message_time=1001.0)
        assert "stale" not in out

    def test_older_message_not_stale(self):
        ctx = _ctx(saved_at=1000.0)
        out = mark_stale_context(ctx, last_message_time=900.0)
        assert "stale" not in out

    def test_missing_probe_keeps_context_untouched(self):
        ctx = _ctx(saved_at=1000.0)
        out = mark_stale_context(ctx, last_message_time=None)
        assert "stale" not in out

    def test_missing_saved_at_keeps_context_untouched(self):
        ctx = _ctx(saved_at=0.0)
        out = mark_stale_context(ctx, last_message_time=1100.0)
        assert "stale" not in out

    def test_none_context_passthrough(self):
        assert mark_stale_context(None, last_message_time=1100.0) is None


class TestBuildWakePromptStaleness:
    def test_fresh_context_renders_full_block_with_age_stamp(self):
        ctx = _ctx(saved_at=time.time() - 600)  # 10 minutes old, not flagged
        prompt = build_wake_prompt(
            "barsik", events=[], pending_tasks=[], inbox_messages=[], context=ctx
        )
        assert "## Continuation (saved 10m ago)" in prompt
        assert "You were working on: deploy release 26.06.125" in prompt
        assert "### Previous Context" in prompt
        assert "long context body" in prompt
        assert "### Notes" in prompt

    def test_stale_context_collapses_to_one_liner(self):
        ctx = _ctx(saved_at=time.time() - 3600)
        ctx["stale"] = True
        prompt = build_wake_prompt(
            "barsik", events=[], pending_tasks=[], inbox_messages=[], context=ctx
        )
        assert "STALE snapshot" in prompt
        assert "trust the live thread" in prompt
        # breadcrumb keeps the task title…
        assert "deploy release 26.06.125" in prompt
        # …but the body, notes, and full-block header are gone
        assert "### Previous Context" not in prompt
        assert "long context body" not in prompt
        assert "### Notes" not in prompt
        assert "You were working on:" not in prompt

    def test_stale_context_without_task_renders_nothing(self):
        ctx = {"context": "body only", "updated_at": 1000.0, "stale": True}
        prompt = build_wake_prompt(
            "barsik", events=[], pending_tasks=[], inbox_messages=[], context=ctx
        )
        assert "Continuation" not in prompt
        assert "body only" not in prompt

    def test_no_context_unchanged(self):
        prompt = build_wake_prompt(
            "barsik", events=[], pending_tasks=[], inbox_messages=[], context=None
        )
        assert "Continuation" not in prompt


class TestLastMessageTime:
    def test_none_when_no_messages(self, convos):
        assert convos.last_message_time("barsik-") is None

    def test_returns_max_timestamp_for_prefix(self, convos):
        convos.append("barsik-main", "user", "hi", timestamp=100.0)
        convos.append("barsik-main", "assistant", "hello", timestamp=200.0)
        convos.append("barsik-research", "user", "side task", timestamp=300.0)
        assert convos.last_message_time("barsik-") == 300.0

    def test_prefix_does_not_match_other_agents(self, convos):
        convos.append("barsik-main", "user", "hi", timestamp=100.0)
        convos.append("pushok-main", "user", "yo", timestamp=999.0)
        assert convos.last_message_time("barsik-") == 100.0

    def test_underscore_in_agent_name_is_not_a_wildcard(self, convos):
        # `_` is a SQL LIKE wildcard — unescaped, foo_bar- would match
        # fooXbar-main (Murzik repro, PR #733 review).
        convos.append("fooXbar-main", "user", "imposter", timestamp=1234.0)
        convos.append("foo_bar-main", "user", "real", timestamp=100.0)
        assert convos.last_message_time("foo_bar-") == 100.0

    def test_known_ambiguity_prefix_family_names(self, convos):
        # PINS the documented residual: the agent-label session-id encoding
        # cannot distinguish prefix-family agent names, so foo- also sees
        # foo-bar's sessions. Worst case = false STALE (collapsed
        # continuation), never a wrong replay. Real fix = agent_name column
        # on conversation rows. If this test starts failing because the
        # encoding got fixed, delete it and the docstring caveat together.
        convos.append("foo-main", "user", "mine", timestamp=100.0)
        convos.append("foo-bar-main", "user", "sibling agent", timestamp=999.0)
        assert convos.last_message_time("foo-") == 999.0
