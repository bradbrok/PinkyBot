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
    async def test_first_inbound_claims_primary_on_fresh_install(self, monkeypatch):
        """Fresh install: no primary user configured. The first person to
        message the bot should be claimed as primary/owner and auto-approved,
        instead of getting the confusing 'waiting for approval' reply from
        their own bot."""
        tmpdir, registry, broker, sent_messages, _ = self._make_broker()
        try:
            routed: list[str] = []

            async def _fake_route(agent_name, message):
                routed.append(message.sender_id)

            monkeypatch.setattr(broker, "_route_streaming", _fake_route)
            assert registry.get_primary_user().get("chat_id") in (None, "")

            msg = BrokerMessage(
                platform="telegram",
                chat_id="6770805286",
                sender_name="Brad",
                sender_id="6770805286",
                content="hey",
                agent_name="barsik",
            )
            await broker.handle_inbound(msg)

            assert registry.get_primary_user().get("chat_id") == "6770805286"
            assert registry.get_user_status("barsik", "6770805286") == "approved"
            assert routed == ["6770805286"]  # routed, not queued as pending
            assert not any("Waiting for approval" in m[3] for m in sent_messages)
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_stranger_not_claimed_when_primary_already_set(self, monkeypatch):
        """Once a primary owner exists, a new sender must NOT hijack ownership —
        they go through the normal pending-approval flow."""
        tmpdir, registry, broker, sent_messages, _ = self._make_broker()
        try:
            routed: list[str] = []

            async def _fake_route(agent_name, message):
                routed.append(message.sender_id)

            monkeypatch.setattr(broker, "_route_streaming", _fake_route)
            registry.set_primary_user("6770805286", display_name="Brad")

            msg = BrokerMessage(
                platform="telegram",
                chat_id="999",
                sender_name="Stranger",
                sender_id="999",
                content="let me in",
                agent_name="barsik",
            )
            await broker.handle_inbound(msg)

            assert registry.get_primary_user().get("chat_id") == "6770805286"
            assert registry.get_user_status("barsik", "999") == "pending"
            assert routed == []  # not routed — queued pending approval
            assert any("Waiting for approval" in m[3] for m in sent_messages)
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
    async def test_idle_autowake_blocked_by_isolation_guard(self):
        """#149 P1 re-review (Murzik #642): the broker's idle auto-wake calls
        connect() directly, bypassing _ensure_streaming_session. It must
        consult the isolation guard first — a blocked agent (e.g. a local
        session relabeled unix_user) is NOT relaunched under the daemon uid."""
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
            # Guard returns a block reason → wake must be skipped.
            broker.set_isolation_guard(
                lambda name: (501, "isolation_mode 'unix_user' is not runnable yet")
            )

            msg = BrokerMessage(
                platform="telegram", chat_id="6770805286",
                sender_name="Brad", sender_id="u-1",
                content="ping while asleep", agent_name="barsik",
            )
            await broker._route_streaming("barsik", msg)

            assert ss.connect_calls == 0, "blocked agent must not be auto-woken"
            assert ss.state == SessionState.IDLE_SLEEPING, "session left untouched"
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_idle_autowake_proceeds_when_guard_allows(self):
        """Control for the guard-block test: a guard returning None (mode
        runnable) leaves the existing auto-wake behavior intact."""
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
            broker.set_isolation_guard(lambda name: None)  # mode runnable

            msg = BrokerMessage(
                platform="telegram", chat_id="6770805286",
                sender_name="Brad", sender_id="u-1",
                content="ping while asleep", agent_name="barsik",
            )
            await broker._route_streaming("barsik", msg)

            assert ss.connect_calls == 1, "runnable agent auto-wakes as before"
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
    async def test_route_streaming_reply_hint_names_pinky_messaging_tools(self):
        """The agent reply hint must reference real pinky-messaging tools
        (send/thread), not the non-existent send_message()/reply() that the
        old hint named. Regression for Brad's 2026-05-29 report."""
        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            from pinky_daemon.transport_state import SessionState

            class _CapturingStreaming:
                state = SessionState.CONNECTED
                resume_handle = "live"
                captured: dict = {}

                async def send(self, prompt, **kwargs) -> None:
                    _CapturingStreaming.captured = kwargs

            broker.register_streaming("barsik", _CapturingStreaming(), label="main")

            msg = BrokerMessage(
                platform="telegram",
                chat_id="6770805286",
                sender_name="Brad",
                sender_id="u-1",
                content="hi barsik",
                agent_name="barsik",
                message_id="9999",
            )
            await broker._route_streaming("barsik", msg)

            hint = _CapturingStreaming.captured.get("agent_hint", "")
            assert hint, "no agent_hint passed to streaming.send()"
            # The bogus tool names must be gone.
            assert "send_message()" not in hint
            assert "reply()" not in hint
            # Real pinky-messaging tools, with the live chat_id/platform/message_id.
            assert 'send(chat_id="6770805286"' in hint
            assert 'platform="telegram"' in hint
            assert 'thread(message_id="9999"' in hint
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


class TestOutboundDedupe:
    """Issue #113 — suppress accidental duplicate outbound sends."""

    def _make_broker(self, **env):
        tmpdir = tempfile.TemporaryDirectory()
        registry = AgentRegistry(db_path=f"{tmpdir.name}/agents.db")
        registry.register("barsik", model="sonnet", working_dir=tmpdir.name)
        broker = MessageBroker(registry, SessionManager())
        return tmpdir, broker

    def test_first_send_is_not_a_duplicate(self):
        tmpdir, broker = self._make_broker()
        try:
            dup = broker.register_outbound("barsik", "telegram", "111", "hello")
            assert dup is None
        finally:
            tmpdir.cleanup()

    def test_identical_inflight_send_is_suppressed(self):
        tmpdir, broker = self._make_broker()
        try:
            # First reserve, delivery still "in flight" (no finalize yet).
            assert broker.register_outbound("barsik", "telegram", "111", "hello") is None
            dup = broker.register_outbound("barsik", "telegram", "111", "hello")
            assert dup is not None
            assert dup["deduped"] is True
            # Reports success so the retrying caller stops retrying.
            assert dup["sent"] is True
            assert broker._stats["deduped"] == 1
        finally:
            tmpdir.cleanup()

    def test_completed_send_returns_original_message_id(self):
        tmpdir, broker = self._make_broker()
        try:
            assert broker.register_outbound("barsik", "telegram", "111", "hi") is None
            broker.finalize_outbound(
                "barsik", "telegram", "111", "hi",
                {"sent": True, "message_id": "555", "chat_id": "111"},
            )
            dup = broker.register_outbound("barsik", "telegram", "111", "hi")
            assert dup is not None
            assert dup["deduped"] is True
            assert dup["message_id"] == "555"
        finally:
            tmpdir.cleanup()

    def test_different_content_is_not_deduped(self):
        tmpdir, broker = self._make_broker()
        try:
            assert broker.register_outbound("barsik", "telegram", "111", "one") is None
            assert broker.register_outbound("barsik", "telegram", "111", "two") is None
        finally:
            tmpdir.cleanup()

    def test_different_chat_is_not_deduped(self):
        """broadcast sends identical content to many chats — must not collide."""
        tmpdir, broker = self._make_broker()
        try:
            assert broker.register_outbound("barsik", "telegram", "111", "x") is None
            assert broker.register_outbound("barsik", "telegram", "222", "x") is None
        finally:
            tmpdir.cleanup()

    def test_different_presentation_options_not_deduped(self):
        """#802: identical text with different presentation options (link
        preview / parse mode) is NOT a duplicate — the second must deliver,
        while the SAME options still dedupe."""
        lpo = '{"lpo":{"is_disabled":true},"pm":""}'
        tmpdir, broker = self._make_broker()
        try:
            # Plain send, then same text with a preview-suppress option: both fresh.
            assert broker.register_outbound(
                "barsik", "telegram", "111", "see https://x", key_extra=""
            ) is None
            assert broker.register_outbound(
                "barsik", "telegram", "111", "see https://x", key_extra=lpo
            ) is None
            # The same text + same options still collapses to a duplicate.
            dup = broker.register_outbound(
                "barsik", "telegram", "111", "see https://x", key_extra=lpo
            )
            assert dup is not None and dup["deduped"] is True
        finally:
            tmpdir.cleanup()

    def test_reply_to_is_part_of_identity(self):
        tmpdir, broker = self._make_broker()
        try:
            assert broker.register_outbound(
                "barsik", "telegram", "111", "x", reply_to="10"
            ) is None
            # Same text, different reply target → distinct send.
            assert broker.register_outbound(
                "barsik", "telegram", "111", "x", reply_to="20"
            ) is None
        finally:
            tmpdir.cleanup()

    def test_clear_outbound_allows_retry_after_failure(self):
        tmpdir, broker = self._make_broker()
        try:
            assert broker.register_outbound("barsik", "telegram", "111", "hi") is None
            # Delivery failed — release the reservation.
            broker.clear_outbound("barsik", "telegram", "111", "hi")
            # A genuine retry must now be allowed through.
            assert broker.register_outbound("barsik", "telegram", "111", "hi") is None
        finally:
            tmpdir.cleanup()

    def test_expired_entry_is_pruned(self):
        tmpdir, broker = self._make_broker()
        try:
            broker._dedupe_window = 60.0
            assert broker.register_outbound("barsik", "telegram", "111", "hi") is None
            # Simulate the entry aging past the window.
            key = broker._dedupe_key("barsik", "telegram", "111", "hi")
            broker._recent_sends[key]["ts"] -= 120
            # Next identical send is treated as fresh.
            assert broker.register_outbound("barsik", "telegram", "111", "hi") is None
        finally:
            tmpdir.cleanup()

    def test_window_zero_disables_dedupe(self):
        tmpdir, broker = self._make_broker()
        try:
            broker._dedupe_window = 0
            assert broker.register_outbound("barsik", "telegram", "111", "hi") is None
            assert broker.register_outbound("barsik", "telegram", "111", "hi") is None
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_deliver_deduped_delivers_once_and_suppresses_repeat(self):
        tmpdir, broker = self._make_broker()
        try:
            calls = []

            async def deliver():
                calls.append(1)
                return {"sent": True, "message_id": "1", "chat_id": "111"}

            r1 = await broker.deliver_deduped("barsik", "telegram", "111", "hi", deliver)
            r2 = await broker.deliver_deduped("barsik", "telegram", "111", "hi", deliver)

            assert calls == [1]  # second call never reached the deliver fn
            assert r1["message_id"] == "1"
            assert r2["deduped"] is True
            assert r2["message_id"] == "1"  # idempotent: original message_id
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_deliver_deduped_failure_releases_reservation(self):
        tmpdir, broker = self._make_broker()
        try:
            async def boom():
                raise RuntimeError("telegram down")

            with pytest.raises(RuntimeError):
                await broker.deliver_deduped("barsik", "telegram", "111", "hi", boom)

            # Reservation released — a genuine retry is allowed through.
            assert broker.register_outbound("barsik", "telegram", "111", "hi") is None
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_deliver_deduped_cancellation_keeps_reservation(self):
        """Regression guard (#113): CancelledError must NOT clear the
        reservation — the executor delivery may still be in flight, so a retry
        has to be deduped, not re-delivered. Broadening back to
        `except BaseException` would break this test."""
        tmpdir, broker = self._make_broker()
        try:
            import asyncio as _asyncio

            async def cancelled():
                raise _asyncio.CancelledError()

            with pytest.raises(_asyncio.CancelledError):
                await broker.deliver_deduped("barsik", "telegram", "111", "hi", cancelled)

            # Reservation intact — an identical send is suppressed.
            dup = broker.register_outbound("barsik", "telegram", "111", "hi")
            assert dup is not None
            assert dup["deduped"] is True
        finally:
            tmpdir.cleanup()


class TestStreamingSessionRegistry:
    """_get_streaming_session mapping + register_streaming displacement."""

    def _make_broker(self):
        tmpdir = tempfile.TemporaryDirectory()
        registry = AgentRegistry(db_path=f"{tmpdir.name}/agents.db")
        registry.register("barsik", model="sonnet", working_dir=tmpdir.name)
        broker = MessageBroker(registry, SessionManager())
        return tmpdir, registry, broker

    def test_mapped_session_returned_even_when_not_connected(self):
        """A channel mapped to a non-main label must get THAT session back in
        any state, so _route_streaming's auto-wake targets the assigned
        session instead of leaking the message into main's context."""
        from pinky_daemon.transport_state import SessionState

        tmpdir, registry, broker = self._make_broker()
        try:
            class _Session:
                def __init__(self, state):
                    self.state = state

            main = _Session(SessionState.CONNECTED)
            research = _Session(SessionState.IDLE_SLEEPING)
            broker.register_streaming("barsik", main, label="main")
            broker.register_streaming("barsik", research, label="research")
            registry.set_channel_session("barsik", "chat-1", "research")

            got = broker._get_streaming_session("barsik", "chat-1")
            assert got is research, (
                "mapped session must be returned in ANY state -- falling back "
                "to main delivers the message into the wrong session context"
            )
            # Unmapped chat still falls back to main.
            assert broker._get_streaming_session("barsik", "chat-2") is main
            # Mapped label with no session object falls back to main.
            registry.set_channel_session("barsik", "chat-3", "ghost")
            assert broker._get_streaming_session("barsik", "chat-3") is main
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_register_streaming_disconnects_displaced_connected_session(self):
        """Overwriting a still-connected session must schedule a disconnect of
        the displaced one instead of orphaning a live SDK subprocess."""
        import asyncio

        from pinky_daemon.transport_state import SessionState

        tmpdir, _, broker = self._make_broker()
        try:
            class _Session:
                def __init__(self, state):
                    self.state = state
                    self.disconnect_calls = 0

                async def disconnect(self):
                    self.disconnect_calls += 1

            old = _Session(SessionState.CONNECTED)
            new = _Session(SessionState.CONNECTED)
            broker.register_streaming("barsik", old, label="main")
            broker.register_streaming("barsik", new, label="main")

            # Let the scheduled disconnect task run.
            for _ in range(5):
                await asyncio.sleep(0)

            assert old.disconnect_calls == 1, "displaced session must be disconnected"
            assert new.disconnect_calls == 0
            assert broker._streaming["barsik"]["main"] is new
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_register_streaming_leaves_disconnected_displaced_alone(self):
        import asyncio

        from pinky_daemon.transport_state import SessionState

        tmpdir, _, broker = self._make_broker()
        try:
            class _Session:
                def __init__(self, state):
                    self.state = state
                    self.disconnect_calls = 0

                async def disconnect(self):
                    self.disconnect_calls += 1

            old = _Session(SessionState.DEAD)
            new = _Session(SessionState.CONNECTED)
            broker.register_streaming("barsik", old, label="main")
            broker.register_streaming("barsik", new, label="main")
            for _ in range(5):
                await asyncio.sleep(0)

            assert old.disconnect_calls == 0, "non-connected displaced session is left alone"
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_register_streaming_skips_disconnect_when_transport_shared(self):
        """Tmux names its OS session pinky-{agent} (no per-instance component),
        so a displaced and a replacement session object drive the SAME tmux
        session -- resume_handle is that name on both. Disconnecting the
        displaced object would kill-session the replacement's live transport."""
        import asyncio

        from pinky_daemon.transport_state import SessionState

        tmpdir, _, broker = self._make_broker()
        try:
            class _TmuxLike:
                def __init__(self, handle):
                    self.state = SessionState.CONNECTED
                    self.resume_handle = handle
                    self.disconnect_calls = 0

                async def disconnect(self):
                    self.disconnect_calls += 1

            old = _TmuxLike("pinky-barsik")
            new = _TmuxLike("pinky-barsik")
            broker.register_streaming("barsik", old, label="main")
            broker.register_streaming("barsik", new, label="main")
            for _ in range(5):
                await asyncio.sleep(0)

            assert old.disconnect_calls == 0, (
                "displaced session sharing the transport resource must NOT be "
                "disconnected -- that would kill the replacement's tmux session"
            )
            assert new.disconnect_calls == 0
            assert broker._streaming["barsik"]["main"] is new

            # Distinct resources (e.g. two SDK sessions with their own
            # subprocesses) still get the displaced-disconnect treatment.
            third = _TmuxLike("other-handle")
            broker.register_streaming("barsik", third, label="main")
            for _ in range(5):
                await asyncio.sleep(0)
            assert new.disconnect_calls == 1
            assert third.disconnect_calls == 0
        finally:
            tmpdir.cleanup()


class TestEventLoopOffload:
    """Blocking platform I/O must run off the event loop (asyncio.to_thread)."""

    def _make_broker(self):
        tmpdir = tempfile.TemporaryDirectory()
        registry = AgentRegistry(db_path=f"{tmpdir.name}/agents.db")
        registry.register("barsik", model="sonnet", working_dir=tmpdir.name)

        sent_messages: list[tuple[str, str, str, str]] = []

        async def send_callback(agent_name, platform, chat_id, content):
            sent_messages.append((agent_name, platform, chat_id, content))

        broker = MessageBroker(registry, SessionManager(), send_callback=send_callback)
        return tmpdir, registry, broker, sent_messages

    @pytest.mark.asyncio
    async def test_download_photo_attachments_offloads_sync_download(self, monkeypatch):
        """TelegramAdapter.download_file is fully synchronous (blocking httpx
        GET + file write). It must run in a worker thread, not on the loop."""
        import os
        import threading

        from pinky_outreach.telegram import TelegramAdapter

        tmpdir, registry, broker, _ = self._make_broker()
        try:
            registry.set_token("barsik", "telegram", "123:abc")
            loop_thread = threading.get_ident()
            download_threads: list[int] = []

            def fake_download(self, file_id, dest_dir=""):
                download_threads.append(threading.get_ident())
                return os.path.join(dest_dir, "photo.jpg")

            monkeypatch.setattr(TelegramAdapter, "download_file", fake_download)

            msg = BrokerMessage(
                platform="telegram",
                chat_id="6770805286",
                sender_name="Brad",
                sender_id="u-1",
                content="look at this",
                agent_name="barsik",
                attachments=[{"type": "photo", "file_id": "f-1"}],
            )
            await broker._download_photo_attachments("barsik", msg)

            assert download_threads, "download_file was never called"
            assert download_threads[0] != loop_thread, (
                "download_file ran on the event-loop thread -- a multi-MB "
                "download would freeze the entire daemon"
            )
            assert msg.attachments[0]["local_path"].endswith("photo.jpg")
        finally:
            tmpdir.cleanup()

    async def test_download_photo_attachments_slack_uses_url(self, monkeypatch):
        """Slack tags every inbound attachment ``type="file"`` and downloads via
        a ``url_private_download`` URL through the SlackAdapter -- NOT a file_id
        through the TelegramAdapter. Regression for the Telegram-hardcoded
        download path that left Chekov unable to read inbound Slack files."""
        import os

        from pinky_outreach.slack import SlackAdapter

        tmpdir, registry, broker, _ = self._make_broker()
        try:
            registry.set_token("barsik", "slack", "xoxb-123")
            seen: dict = {}

            def fake_download(self, url, dest_dir=""):
                seen["url"] = url
                return os.path.join(dest_dir, "report.md")

            monkeypatch.setattr(SlackAdapter, "download_file", fake_download)

            url = "https://files.slack.com/files-pri/T-F123/download/report.md"
            msg = BrokerMessage(
                platform="slack",
                chat_id="C0BBT4WAYVA",
                sender_name="Brad",
                sender_id="U1",
                content="here's the doc",
                agent_name="barsik",
                attachments=[{
                    "type": "file",
                    "file_id": "F123",
                    "url": url,
                    "file_name": "report.md",
                }],
            )
            await broker._download_photo_attachments("barsik", msg)

            assert seen.get("url") == url, (
                "Slack download must use url_private_download, not the file_id"
            )
            assert msg.attachments[0]["local_path"].endswith("report.md")
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_try_voice_reply_uses_daemon_url_and_offloads(self, monkeypatch):
        """The voice-reply HTTP loopback targets THIS process: a sync urlopen
        on the event loop self-deadlocks until the 60s socket timeout. It must
        run in a worker thread and honor PINKY_DAEMON_URL."""
        import threading
        import urllib.request

        tmpdir, registry, broker, sent_messages = self._make_broker()
        try:
            registry.register("barsik", voice_config={"voice_reply": True})
            monkeypatch.setenv("PINKY_DAEMON_URL", "http://127.0.0.1:9111")

            loop_thread = threading.get_ident()
            captured: list[tuple[str, int]] = []

            class _FakeResp:
                def __enter__(self):
                    return self

                def __exit__(self, *args):
                    return False

                def read(self):
                    return b'{"sent": true}'

            def fake_urlopen(req, timeout=0):
                captured.append((req.full_url, threading.get_ident()))
                return _FakeResp()

            monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

            sent = await broker._try_voice_reply(
                "barsik", "telegram", "6770805286", "hello in voice",
            )

            assert sent is True
            assert captured, "urlopen never called"
            url, thread_id = captured[0]
            assert url == "http://127.0.0.1:9111/broker/send-voice", (
                f"voice reply must honor PINKY_DAEMON_URL, got {url}"
            )
            assert thread_id != loop_thread, (
                "urlopen ran on the event-loop thread -- guaranteed deadlock "
                "against our own /broker/send-voice endpoint"
            )
            # Accessibility text companion still goes out.
            assert ("barsik", "telegram", "6770805286", "hello in voice") in sent_messages
        finally:
            tmpdir.cleanup()
