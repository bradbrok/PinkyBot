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

        async def send_callback(agent_name: str, platform: str, chat_id: str, content: str):
            sent_messages.append((agent_name, platform, chat_id, content))

        broker = MessageBroker(registry, SessionManager(), send_callback=send_callback)
        return tmpdir, registry, broker, sent_messages

    @pytest.mark.asyncio
    async def test_route_response_targets_newline_channel_block(self):
        tmpdir, registry, broker, sent_messages = self._make_broker()
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
        tmpdir, registry, broker, sent_messages = self._make_broker()
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
        tmpdir, registry, broker, sent_messages = self._make_broker()
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
