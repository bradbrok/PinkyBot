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

    @pytest.mark.asyncio
    async def test_inject_agent_message_stamps_last_seen_on_success(self):
        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            class _FakeStreaming:
                is_connected = True
                sent: list[str] = []

                async def send(self, prompt: str) -> None:
                    _FakeStreaming.sent.append(prompt)

            broker.register_streaming("barsik", _FakeStreaming(), label="main")
            assert registry.get("barsik").last_seen_at == 0.0

            ok = await broker.inject_agent_message("pushok", "barsik", "hi")
            assert ok is True
            assert registry.get("barsik").last_seen_at > 0.0
            assert _FakeStreaming.sent  # delivery happened
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_inject_agent_message_does_not_stamp_when_not_connected(self):
        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            # No streaming session registered — inject should fail without stamping.
            ok = await broker.inject_agent_message("pushok", "barsik", "hi")
            assert ok is False
            assert registry.get("barsik").last_seen_at == 0.0
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_stop_typing_cancels_active_task(self):
        """_stop_typing must cancel a running typing-loop task."""
        import asyncio

        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            async def _fake_loop():
                await asyncio.sleep(60)

            task = asyncio.create_task(_fake_loop())
            # Yield once so the task actually starts before we cancel it.
            await asyncio.sleep(0)
            broker._typing_tasks[("barsik", "6770805286")] = task

            broker._stop_typing("barsik", "6770805286")
            # Yield until cancellation has propagated to the task.
            try:
                await task
            except asyncio.CancelledError:
                pass

            assert ("barsik", "6770805286") not in broker._typing_tasks
            assert task.cancelled()
        finally:
            tmpdir.cleanup()

    def test_stop_typing_is_silent_noop_when_no_task(self):
        """Defensive _stop_typing calls (e.g. after every outreach send) must be no-op."""
        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            # No task registered for this chat — should not raise, should not log.
            broker._stop_typing("barsik", "never-typed-here")
            assert ("barsik", "never-typed-here") not in broker._typing_tasks
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_stop_typing_scoped_per_chat(self):
        """Stopping typing for one chat must not affect other chats for the same agent."""
        import asyncio

        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            async def _fake_loop():
                await asyncio.sleep(60)

            task_a = asyncio.create_task(_fake_loop())
            task_b = asyncio.create_task(_fake_loop())
            broker._typing_tasks[("barsik", "chat-A")] = task_a
            broker._typing_tasks[("barsik", "chat-B")] = task_b

            broker._stop_typing("barsik", "chat-A")
            # Yield once so cancellation propagates.
            await asyncio.sleep(0)

            assert ("barsik", "chat-A") not in broker._typing_tasks
            assert ("barsik", "chat-B") in broker._typing_tasks
            assert task_a.cancelled() or task_a.done()
            assert not task_b.done()

            # Cleanup
            task_b.cancel()
            try:
                await task_b
            except asyncio.CancelledError:
                pass
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
