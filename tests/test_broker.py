"""Tests for pinky_daemon message broker routing."""

from __future__ import annotations

import tempfile

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.sessions import SessionManager


class TestMessageBrokerRouting:
    def _make_broker(self):
        tmpdir = tempfile.TemporaryDirectory()
        registry = AgentRegistry(db_path=f"{tmpdir.name}/agents.db")
        registry.register("barsik", model="sonnet", working_dir=tmpdir.name)

        sent_messages: list[tuple[str, str, str, str]] = []
        reactions: list[tuple[str, str, str, str, str]] = []

        async def send_callback(agent_name: str, platform: str, chat_id: str, content: str):
            sent_messages.append((agent_name, platform, chat_id, content))

        async def reaction_callback(
            agent_name: str,
            platform: str,
            chat_id: str,
            message_id: str,
            emoji: str,
        ):
            reactions.append((agent_name, platform, chat_id, message_id, emoji))

        broker = MessageBroker(
            registry,
            SessionManager(),
            send_callback=send_callback,
            reaction_callback=reaction_callback,
        )
        return tmpdir, registry, broker, sent_messages, reactions

    @pytest.mark.asyncio
    async def test_route_response_sends_plain_text_when_fallback_enabled(self):
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            await broker.route_response(
                "barsik",
                "telegram",
                "6770805286",
                "Ping from Barsik",
                message_id="42",
                used_outreach=False,
                fallback_enabled=True,
            )

            assert sent_messages == [
                ("barsik", "telegram", "6770805286", "Ping from Barsik"),
            ]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_skips_plain_text_when_fallback_disabled(self):
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            await broker.route_response(
                "barsik",
                "telegram",
                "6770805286",
                "Do not send this automatically",
                used_outreach=False,
                fallback_enabled=False,
            )

            assert sent_messages == []
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_skips_plain_text_when_outreach_used(self):
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            await broker.route_response(
                "barsik",
                "telegram",
                "6770805286",
                "Handled via thread()",
                used_outreach=True,
                fallback_enabled=True,
            )

            assert sent_messages == []
        finally:
            tmpdir.cleanup()

    def test_remember_message_context_tracks_voice_and_reply_metadata(self):
        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            broker.remember_message_context(
                BrokerMessage(
                    platform="telegram",
                    chat_id="6770805286",
                    sender_name="Brad",
                    sender_id="u-1",
                    content="voice note",
                    agent_name="barsik",
                    message_id="99",
                    reply_to="42",
                    attachments=[{"type": "voice", "file_id": "file-1"}],
                    metadata={"chat_title": "Brad"},
                ),
                source_was_voice=True,
            )

            ctx = broker.get_message_context("barsik", "99")
            assert ctx is not None
            assert ctx.chat_id == "6770805286"
            assert ctx.reply_to == "42"
            assert ctx.source_was_voice is True
            assert ctx.attachments == [{"type": "voice", "file_id": "file-1"}]
        finally:
            tmpdir.cleanup()
