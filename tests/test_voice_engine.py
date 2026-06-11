"""Tests for pinky_daemon.voice_engine helpers.

Focused on regression coverage for #286 — finalize_call() getting handed
a None session under WS-disconnect races, plus TwiML attribute escaping,
finalize-time artifact-save resilience, and Anthropic client reuse.
"""

from __future__ import annotations

import asyncio
import sqlite3
import xml.dom.minidom
from unittest.mock import MagicMock

import pytest

import pinky_daemon.voice_engine as voice_engine
from pinky_daemon.voice_engine import build_outbound_twiml, finalize_call


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


class TestBuildOutboundTwiml:
    """welcomeGreeting is agent-goal-derived text embedded in an XML
    attribute; it must be quote-escaped or quotes break the TwiML."""

    def test_greeting_with_double_quotes_produces_wellformed_xml(self):
        greeting = 'Ask for the "early bird" rate, please.'
        twiml = build_outbound_twiml("wss://example/ws/abc", welcome_greeting=greeting)

        doc = xml.dom.minidom.parseString(twiml)
        relay = doc.getElementsByTagName("ConversationRelay")[0]
        assert relay.getAttribute("welcomeGreeting") == greeting

    def test_greeting_cannot_inject_attributes(self):
        greeting = 'hi" ttsProvider="Evil'
        twiml = build_outbound_twiml("wss://example/ws/abc", welcome_greeting=greeting)

        doc = xml.dom.minidom.parseString(twiml)
        relay = doc.getElementsByTagName("ConversationRelay")[0]
        assert relay.getAttribute("ttsProvider") == "Google"
        assert relay.getAttribute("welcomeGreeting") == greeting

    def test_no_greeting_omits_attribute(self):
        twiml = build_outbound_twiml("wss://example/ws/abc")
        doc = xml.dom.minidom.parseString(twiml)
        relay = doc.getElementsByTagName("ConversationRelay")[0]
        assert not relay.hasAttribute("welcomeGreeting")


class TestFinalizeCallArtifactResilience:
    """A duplicate finalize hits the UNIQUE call_sid constraint on
    voice_call_artifact; the owner notification must still go out."""

    def test_save_artifact_failure_still_notifies(self, monkeypatch):
        session = MagicMock()
        session.call_sid = "CA123"
        session.id = "sess-abc"
        session.call_request_id = "req-1"
        session.to_number = "+15551234567"

        req = MagicMock()
        req.goal = "book a table"
        req.target_name = "Pizza Place"

        event = MagicMock()
        event.event_type = "prompt"
        event.role = "caller"
        event.content = "hello"
        event.ts = 1.0

        voice_store = MagicMock()
        voice_store.get_call_request.return_value = req
        voice_store.get_events.return_value = [event]
        voice_store.save_artifact.side_effect = sqlite3.IntegrityError(
            "UNIQUE constraint failed: voice_call_artifact.call_sid"
        )

        agents = MagicMock()
        agents.get_primary_user.return_value = {
            "chat_id": "12345", "default_agent": "barsik",
        }

        async def fake_outcome(session, transcript, goal, api_key=""):
            return {"success": True, "summary": "done"}

        monkeypatch.setattr(
            voice_engine, "extract_outcome_with_opus", fake_outcome
        )

        sent = []

        async def broker_send(agent, platform, chat_id, text):
            sent.append((agent, platform, chat_id, text))

        asyncio.run(
            finalize_call(
                session, voice_store, agents,
                broker_send=broker_send, api_key="test",
            )
        )

        voice_store.save_artifact.assert_called_once()
        assert len(sent) == 1


class TestAnthropicClientReuse:
    """One AsyncAnthropic client per API key, not one per conversation turn."""

    def test_same_key_returns_same_client(self):
        # anthropic is an optional dependency imported lazily by the voice
        # engine; CI installs only the [dev] extras.
        pytest.importorskip("anthropic")
        voice_engine._anthropic_clients.clear()
        try:
            a = voice_engine._get_anthropic_client("key-1")
            b = voice_engine._get_anthropic_client("key-1")
            c = voice_engine._get_anthropic_client("key-2")
            assert a is b
            assert a is not c
        finally:
            voice_engine._anthropic_clients.clear()
