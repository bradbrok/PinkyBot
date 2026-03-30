"""Tests for pinky_daemon.message broker routing."""

from __future__ import annotations

import tempfile

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.broker import MessageBroker
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
    async def test_route_response_targets_newline_channel_block(self):
        tmpdir, registry, broker, sent_messages, _ = self._make_broker()
        try:
            registry.approve_user("barsik", "6770805286", display_name="Brad", approved_by="test")

            await broker.route_response(
                "barsik",
                "web",
                "web",
                "@channel:6770805286\nPing from Barsik",
            )

            assert sent_messages == [
                ("barsik", "telegram", "6770805286", "Ping from Barsik"),
            ]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_targets_multiple_newline_channel_blocks(self):
        tmpdir, registry, broker, sent_messages, _ = self._make_broker()
        try:
            registry.approve_user("barsik", "111", display_name="Brad", approved_by="test")
            registry.approve_user("barsik", "222", display_name="Oleg", approved_by="test")

            await broker.route_response(
                "barsik",
                "web",
                "web",
                "@channel:111\nFirst ping\n@channel:222\nSecond ping",
            )

            assert sent_messages == [
                ("barsik", "telegram", "111", "First ping"),
                ("barsik", "telegram", "222", "Second ping"),
            ]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_falls_back_with_unknown_newline_channel_block(self):
        tmpdir, registry, broker, sent_messages, _ = self._make_broker()
        try:
            await broker.route_response(
                "barsik",
                "web",
                "web",
                "@channel:missing-user\nFallback body",
            )

            assert sent_messages == [
                ("barsik", "web", "web", "@channel:missing-user\nFallback body"),
            ]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_reacts_to_triggering_message(self):
        tmpdir, registry, broker, sent_messages, reactions = self._make_broker()
        try:
            await broker.route_response(
                "barsik",
                "telegram",
                "6770805286",
                "@react:👍",
                message_id="42",
            )

            assert sent_messages == []
            assert reactions == [
                ("barsik", "telegram", "6770805286", "42", "👍"),
            ]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_reacts_and_sends_remaining_text(self):
        tmpdir, registry, broker, sent_messages, reactions = self._make_broker()
        try:
            await broker.route_response(
                "barsik",
                "telegram",
                "6770805286",
                "@react:42 ✅\nOn it.",
                message_id="41",
            )

            assert reactions == [
                ("barsik", "telegram", "6770805286", "42", "✅"),
            ]
            assert sent_messages == [
                ("barsik", "telegram", "6770805286", "On it."),
            ]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_reacts_in_target_channel(self):
        tmpdir, registry, broker, sent_messages, reactions = self._make_broker()
        try:
            registry.approve_user("barsik", "6770805286", display_name="Brad", approved_by="test")

            await broker.route_response(
                "barsik",
                "telegram",
                "999",
                "@react:Brad 55 🔥",
                message_id="41",
            )

            assert sent_messages == []
            assert reactions == [
                ("barsik", "telegram", "6770805286", "55", "🔥"),
            ]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_reaction_falls_back_without_message_id(self):
        tmpdir, registry, broker, sent_messages, reactions = self._make_broker()
        try:
            await broker.route_response(
                "barsik",
                "telegram",
                "6770805286",
                "@react:👍",
            )

            assert reactions == []
            assert sent_messages == [
                ("barsik", "telegram", "6770805286", "@react:👍"),
            ]
        finally:
            tmpdir.cleanup()
