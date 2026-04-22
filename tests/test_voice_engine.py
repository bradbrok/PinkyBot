"""Tests for pinky_daemon.voice_engine helpers.

Focused on regression coverage for #286 — finalize_call() getting handed
a None session under WS-disconnect races.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from pinky_daemon.voice_engine import finalize_call


class TestFinalizeCallNoneGuard:
    """Regression for #286: under session-expiry races,
    `voice_store.get_session()` can return None. Previously this raised
    `AttributeError: 'NoneType' object has no attribute 'call_sid'` because
    `finalize_call()` went straight to `session.call_sid` in the first log
    line. It now no-ops + logs instead."""

    def test_finalize_call_with_none_session_noops(self):
        voice_store = MagicMock()
        agents = MagicMock()
        broker_send = MagicMock()

        # Must not raise AttributeError.
        asyncio.run(
            finalize_call(
                None,
                voice_store,
                agents,
                broker_send=broker_send,
                api_key="test",
            )
        )

        # No voice_store / agents / broker side effects on the None path.
        voice_store.get_call_request.assert_not_called()
        voice_store.get_events.assert_not_called()
        voice_store.save_artifact.assert_not_called()
        broker_send.assert_not_called()

    def test_finalize_call_with_real_session_proceeds(self):
        """Sanity check: a real session is not short-circuited by the guard.

        Uses an empty events list so finalize returns after transcript check
        (no Opus/network dependency)."""
        session = MagicMock()
        session.call_sid = "CA123"
        session.id = "session-abc"
        session.call_request_id = None

        voice_store = MagicMock()
        voice_store.get_events.return_value = []
        agents = MagicMock()

        asyncio.run(
            finalize_call(
                session,
                voice_store,
                agents,
                broker_send=None,
                api_key="test",
            )
        )

        # We got past the None guard and the initial log line — evidenced by
        # get_events being called (which is the step right after the goal
        # lookup).
        voice_store.get_events.assert_called_once_with("session-abc")
        # Empty transcript → save_artifact should be skipped.
        voice_store.save_artifact.assert_not_called()


@pytest.mark.parametrize(
    "bad_value",
    [None],
    ids=["none"],
)
def test_finalize_call_parametrized_nones(bad_value):
    """Keep the bad-input set explicit so regressions show up in the
    parametrize ID."""
    asyncio.run(
        finalize_call(
            bad_value,
            MagicMock(),
            MagicMock(),
            broker_send=None,
            api_key="",
        )
    )
