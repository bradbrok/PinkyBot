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
            from pinky_daemon.transport_state import SessionState

            class _FakeStreaming:
                state = SessionState.CONNECTED
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
    async def test_route_streaming_waits_for_in_flight_reconnect(self, monkeypatch):
        """Regression: messages arriving during ``context_restart`` (where the
        streaming session object exists but ``state`` is briefly != CONNECTED
        and ``resume_handle`` is wiped to "") must be held until the reconnect
        completes — not dropped with a "not running" fallback.

        Simulates the restart window by flipping ``state`` back to CONNECTED
        on a background task after a short delay, mirroring what
        ``StreamingSession.force_restart`` does in production.
        """
        import asyncio

        # Speed up the poll loop so the test stays fast.
        import pinky_daemon.broker as broker_mod
        monkeypatch.setattr(broker_mod, "_INBOUND_RECONNECT_WAIT_SEC", 2.0)
        monkeypatch.setattr(broker_mod, "_INBOUND_RECONNECT_POLL_SEC", 0.01)

        from pinky_daemon.transport_state import SessionState

        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            class _RestartingSession:
                # Mirror StreamingSession state during force_restart:
                # disconnect() has run, resume_handle has been wiped.
                resume_handle = ""

                def __init__(self):
                    self.state = SessionState.RECONNECTING
                    self.sent: list[str] = []

                async def send(self, prompt, **kwargs):
                    self.sent.append(prompt)

            ss = _RestartingSession()
            broker.register_streaming("barsik", ss, label="main")

            # Background task to flip state=CONNECTED after a small delay,
            # simulating force_restart's connect() completing.
            async def _finish_restart():
                await asyncio.sleep(0.1)
                ss.state = SessionState.CONNECTED

            asyncio.create_task(_finish_restart())

            msg = BrokerMessage(
                platform="telegram",
                chat_id="6770805286",
                sender_name="Brad",
                sender_id="u-1",
                content="message during restart",
                agent_name="barsik",
            )
            await broker._route_streaming("barsik", msg)

            # The "not running" fallback MUST NOT fire — that's the bug.
            assert not any(
                "not running" in m[3] for m in sent_messages
            ), f"unexpected fallback sent during restart window: {sent_messages}"
            # And the message DID get delivered to the reconnected session.
            assert ss.sent, "session reconnected but message wasn't delivered"
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_streaming_falls_back_when_reconnect_never_completes(
        self, monkeypatch,
    ):
        """If the wait window elapses without reconnect, the broker must still
        surface the "not running" fallback (preserving the previous behavior
        for genuinely-dead sessions). The wait is bounded, not infinite.
        """
        import pinky_daemon.broker as broker_mod
        monkeypatch.setattr(broker_mod, "_INBOUND_RECONNECT_WAIT_SEC", 0.2)
        monkeypatch.setattr(broker_mod, "_INBOUND_RECONNECT_POLL_SEC", 0.01)

        from pinky_daemon.transport_state import SessionState

        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            class _DeadSession:
                resume_handle = ""

                def __init__(self):
                    self.state = SessionState.DEAD
                    self.sent: list[str] = []

                async def send(self, prompt, **kwargs):  # pragma: no cover
                    self.sent.append(prompt)

            ss = _DeadSession()
            broker.register_streaming("barsik", ss, label="main")

            msg = BrokerMessage(
                platform="telegram",
                chat_id="6770805286",
                sender_name="Brad",
                sender_id="u-1",
                content="message into the void",
                agent_name="barsik",
            )
            await broker._route_streaming("barsik", msg)

            # Wait elapsed without reconnect → fallback fires exactly once
            # with the canonical text. Message was NOT delivered.
            assert sent_messages == [
                ("barsik", "telegram", "6770805286",
                 "⚠️ barsik is not running right now. Try again later."),
            ]
            assert ss.sent == []
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_streaming_does_not_double_connect_during_reconnecting(
        self, monkeypatch,
    ):
        """Regression for @murzik PR #492 blocker 1.

        Pre-fix the auto-wake branch fired for ANY non-CONNECTED state
        as long as resume_handle was non-empty. During force_restart /
        attempt_reconnect, state is RECONNECTING and resume_handle may
        still be set, so an inbound message racing the in-flight reconnect
        would call ss.connect() a SECOND time — concurrent with the
        in-flight one. Post-fix the auto-wake only fires for
        IDLE_SLEEPING; RECONNECTING falls through to the wait-for-reconnect
        poll loop and waits for the in-flight to land naturally.
        """
        import pinky_daemon.broker as broker_mod
        monkeypatch.setattr(broker_mod, "_INBOUND_RECONNECT_WAIT_SEC", 0.3)
        monkeypatch.setattr(broker_mod, "_INBOUND_RECONNECT_POLL_SEC", 0.01)

        from pinky_daemon.transport_state import SessionState

        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            class _ReconnectingSession:
                # Mid-reconnect: state RECONNECTING, resume_handle non-empty
                # (the bug-triggering combo — pre-fix this combo would
                # cause the broker to call connect() again).
                resume_handle = "sdk-abc123"

                def __init__(self):
                    self.state = SessionState.RECONNECTING
                    self.connect_calls = 0
                    self.sent: list[str] = []

                async def connect(self):
                    self.connect_calls += 1
                    self.state = SessionState.CONNECTED

                async def send(self, prompt, **kwargs):
                    self.sent.append(prompt)

            ss = _ReconnectingSession()
            broker.register_streaming("barsik", ss, label="main")

            # Simulate in-flight reconnect completing mid-wait.
            import asyncio as _a
            async def _settle():
                await _a.sleep(0.05)
                ss.state = SessionState.CONNECTED

            _a.create_task(_settle())

            msg = BrokerMessage(
                platform="telegram", chat_id="6770805286",
                sender_name="Brad", sender_id="u-1",
                content="msg during reconnect", agent_name="barsik",
            )
            await broker._route_streaming("barsik", msg)

            # The load-bearing assertion: the broker MUST NOT have called
            # connect() — that's the bug. The in-flight reconnect (the
            # _settle task) is what lands the session in CONNECTED.
            assert ss.connect_calls == 0, (
                f"broker called connect() {ss.connect_calls}x during RECONNECTING — "
                f"the auto-wake branch must only fire for IDLE_SLEEPING. "
                f"Pre-fix this was the double-connect race."
            )
            # Delivery still happens because the wait-for-reconnect block
            # picked up the settle.
            assert ss.sent, "message should deliver once reconnect settles"
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_streaming_auto_wakes_idle_sleeping(self, monkeypatch):
        """Companion to the no-double-connect test: IDLE_SLEEPING with a
        retained resume_handle IS the intended auto-wake path. Pre-fix this
        worked via the broader `not is_connected` check; post-fix it
        works via the explicit `state == IDLE_SLEEPING` check.
        """
        from pinky_daemon.transport_state import SessionState

        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            class _SleepingSession:
                resume_handle = "sdk-resume"

                def __init__(self):
                    self.state = SessionState.IDLE_SLEEPING
                    self.connect_calls = 0
                    self.sent: list[str] = []

                async def connect(self):
                    self.connect_calls += 1
                    self.state = SessionState.CONNECTED

                async def send(self, prompt, **kwargs):
                    self.sent.append(prompt)

            ss = _SleepingSession()
            broker.register_streaming("barsik", ss, label="main")

            msg = BrokerMessage(
                platform="telegram", chat_id="6770805286",
                sender_name="Brad", sender_id="u-1",
                content="ping while asleep", agent_name="barsik",
            )
            await broker._route_streaming("barsik", msg)

            assert ss.connect_calls == 1, (
                f"IDLE_SLEEPING auto-wake must call connect() exactly once; "
                f"got {ss.connect_calls}"
            )
            assert ss.sent, "message should deliver after auto-wake"
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

    @pytest.mark.asyncio
    async def test_route_streaming_cold_starts_via_ensurer_when_no_session(self):
        """Regression: inbound platform message must cold-start the session.

        Pre-fix, ``_route_streaming`` only auto-woke a streaming session
        if one already existed in ``broker._streaming`` with a persisted
        ``session_id``. Sibling agents under the boot policy never have a
        session created at boot, so inbound Telegram/Discord/etc. messages
        for them fell straight to "not running" — even though the web
        admin chat path always cold-started via
        ``_ensure_streaming_session``. The ensurer callback closes that
        gap.
        """
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            from pinky_daemon.transport_state import SessionState

            class _FakeStreaming:
                state = SessionState.CONNECTED
                resume_handle = "freshly-cold-started"
                sent: list[str] = []

                async def send(self, prompt, **kwargs) -> None:
                    _FakeStreaming.sent.append(prompt)

            ensure_calls: list[tuple[str, str]] = []

            async def fake_ensurer(agent_name, *, label):
                ensure_calls.append((agent_name, label))
                return _FakeStreaming()

            broker.set_ensure_session_callback(fake_ensurer)

            msg = BrokerMessage(
                platform="telegram",
                chat_id="6770805286",
                sender_name="Brad",
                sender_id="u-1",
                content="hi lera",
                agent_name="lera",
            )
            await broker._route_streaming("lera", msg)

            # Ensurer was called for the missing session.
            assert ensure_calls == [("lera", "main")]
            # "Not running" fallback did NOT fire.
            assert not any("not running" in m[3] for m in sent_messages), (
                f"unexpected fallback message sent: {sent_messages}"
            )
            # The cold-started session received the routed message.
            assert _FakeStreaming.sent, "cold-started session received no message"
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_streaming_falls_back_when_no_ensurer_wired(self):
        """If no ensurer is wired (e.g. in tests/embedded scenarios), the
        broker must preserve the pre-fix behavior and surface the
        "not running" fallback rather than crashing on a missing callback.
        """
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            # No set_ensure_session_callback call — _ensure_session_callback is None.
            msg = BrokerMessage(
                platform="telegram",
                chat_id="6770805286",
                sender_name="Brad",
                sender_id="u-1",
                content="hi lera",
                agent_name="lera",
            )
            await broker._route_streaming("lera", msg)

            # Fallback fired exactly once with the canonical text.
            assert sent_messages == [
                ("lera", "telegram", "6770805286",
                 "⚠️ lera is not running right now. Try again later."),
            ]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_streaming_uses_existing_disconnected_session_before_ensurer(self):
        """If a session object exists with a persisted session_id but is
        disconnected, the existing auto-wake (reconnect) path must run
        first — the ensurer is only for the cold-start case.
        """
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            from pinky_daemon.transport_state import SessionState

            class _FakeStreaming:
                resume_handle = "persisted-id"
                connect_calls = 0
                sent: list[str] = []

                def __init__(self):
                    self.state = SessionState.IDLE_SLEEPING

                async def connect(self):
                    type(self).connect_calls += 1
                    self.state = SessionState.CONNECTED

                async def send(self, prompt, **kwargs):
                    type(self).sent.append(prompt)

            ss = _FakeStreaming()
            broker.register_streaming("lera", ss, label="main")

            ensure_calls: list[tuple[str, str]] = []

            async def fake_ensurer(agent_name, *, label):
                ensure_calls.append((agent_name, label))
                return None  # would fail if called — we expect it NOT to be

            broker.set_ensure_session_callback(fake_ensurer)

            msg = BrokerMessage(
                platform="telegram",
                chat_id="6770805286",
                sender_name="Brad",
                sender_id="u-1",
                content="hi lera",
                agent_name="lera",
            )
            await broker._route_streaming("lera", msg)

            # Existing session's connect() was called once.
            assert _FakeStreaming.connect_calls == 1
            # Ensurer was NOT called — existing session won.
            assert ensure_calls == [], (
                f"ensurer should not be called when an existing session is "
                f"reconnectable, got: {ensure_calls}"
            )
            assert _FakeStreaming.sent, "reconnected session received no message"
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
