"""Tests for pinky_daemon message broker routing."""

from __future__ import annotations

import tempfile

import pytest

from pinky_daemon.agent_registry import AgentRegistry
from pinky_daemon.broker import BrokerMessage, MessageBroker
from pinky_daemon.sessions import SessionManager

BRAD_BUZZ_PRINCIPAL = (
    "buzz:posspecialists:"
    "90425c785cf23b60e57300658a7f4855938b3c2f661b3ef33acdb54831fcb44b"
)
BUZZ_CHANNEL = "00000000-0000-4000-8000-000000000001"


class TestMessageBrokerRouting:
    def _make_broker(self):
        tmpdir = tempfile.TemporaryDirectory()
        registry = AgentRegistry(db_path=f"{tmpdir.name}/agents.db")
        registry.register("barsik", model="sonnet", working_dir=tmpdir.name)

        sent_messages: list[tuple[str, str, str, str]] = []
        reactions: list[tuple[str, str, str, str, str]] = []

        async def send_callback(
            agent_name: str, platform: str, chat_id: str, content: str,
            *, account_id: str = "",
        ):
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

    async def _capture_reply_hint(
        self,
        *,
        platform: str = "telegram",
        message_id: str = "9999",
        reply_to: str = "",
    ) -> str:
        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            from pinky_daemon.transport_state import SessionState

            class _CapturingStreaming:
                state = SessionState.CONNECTED
                resume_handle = "live"

                def __init__(self):
                    self.captured: dict = {}

                async def send(self, prompt, **kwargs) -> None:
                    self.captured = kwargs

            streaming = _CapturingStreaming()
            broker.register_streaming("barsik", streaming, label="main")

            msg = BrokerMessage(
                platform=platform,
                chat_id="6770805286",
                sender_name="Brad",
                sender_id="u-1",
                content="hi barsik",
                agent_name="barsik",
                message_id=message_id,
                reply_to=reply_to,
            )
            await broker._route_streaming("barsik", msg)
            return streaming.captured.get("agent_hint", "")
        finally:
            tmpdir.cleanup()

    def test_format_prompt_includes_thread_provenance_for_child_reply(self):
        tmpdir, registry, broker, _sent, _reactions = self._make_broker()
        try:
            registry.set_default_timezone("UTC")
            prompt = broker._format_prompt(
                BrokerMessage(
                    platform="slack", chat_id="C123", sender_name="Alex",
                    sender_id="U123", content="in thread", agent_name="barsik",
                    message_id="1711584001.000200",
                    reply_to="1711584000.000100",
                    is_group=True,
                    timestamp=0,
                )
            )
            header = prompt.splitlines()[0]
            assert "thread_root_ts:1711584000.000100" in header
            assert "is_thread_reply:true" in header
        finally:
            tmpdir.cleanup()

    def test_format_prompt_top_level_header_is_unchanged(self):
        tmpdir, registry, broker, _sent, _reactions = self._make_broker()
        try:
            registry.set_default_timezone("UTC")
            prompt = broker._format_prompt(
                BrokerMessage(
                    platform="slack", chat_id="C123", sender_name="Alex",
                    sender_id="U123", content="top level", agent_name="barsik",
                    message_id="1711584000.000100",
                    is_group=True,
                    timestamp=0,
                )
            )
            header = prompt.splitlines()[0]
            assert "thread_root_ts:" not in header
            assert "is_thread_reply:" not in header
            assert header == (
                "[slack | group | C123 | Alex | C123 | "
                "1970-01-01 00:00:00 UTC | msg_id:1711584000.000100]"
            )
        finally:
            tmpdir.cleanup()

    def test_buzz_known_contact_uses_registry_name_role_and_fingerprint(self):
        tmpdir, registry, broker, _sent, _reactions = self._make_broker()
        try:
            registry.set_default_timezone("UTC")
            prompt = broker._format_prompt(
                BrokerMessage(
                    platform="buzz",
                    chat_id=BUZZ_CHANNEL,
                    chat_title="#general",
                    sender_name="attacker-controlled name",
                    sender_id=BRAD_BUZZ_PRINCIPAL,
                    content="hello",
                    agent_name="barsik",
                    message_id="ab" * 32,
                    timestamp=0,
                    is_group=True,
                    metadata={
                        "buzz_verified_principal": BRAD_BUZZ_PRINCIPAL,
                        "buzz_mentioned_self": False,
                    },
                )
            )
            assert prompt.splitlines()[0] == (
                f"[buzz | #general | from:Brad (owner) principal:90425c785cf2… | "
                f"mentioned_self:false | chat_id:{BUZZ_CHANNEL} | "
                f"1970-01-01 00:00:00 UTC | msg_id:{'ab' * 32}]"
            )
            assert "display_name" not in prompt.splitlines()[0]
            assert BRAD_BUZZ_PRINCIPAL not in prompt.splitlines()[0]
        finally:
            tmpdir.cleanup()

    def test_buzz_unknown_contact_keeps_untrusted_name_and_full_principal(self):
        tmpdir, registry, broker, _sent, _reactions = self._make_broker()
        try:
            registry.set_default_timezone("UTC")
            principal = f"buzz:posspecialists:{'aa' * 32}"
            header = broker._format_prompt(
                BrokerMessage(
                    platform="buzz",
                    chat_id=BUZZ_CHANNEL,
                    chat_title="#general",
                    sender_name="Somebody",
                    sender_id=principal,
                    content="hello",
                    agent_name="barsik",
                    timestamp=0,
                    is_group=True,
                    metadata={"buzz_verified_principal": principal},
                )
            ).splitlines()[0]
            assert header == (
                f"[buzz | #general | display_name(untrusted):Somebody | "
                f"principal:{principal} | mentioned_self:false | "
                f"chat_id:{BUZZ_CHANNEL} | 1970-01-01 00:00:00 UTC]"
            )
        finally:
            tmpdir.cleanup()

    def test_buzz_unknown_contact_flags_registered_name_collision(self):
        tmpdir, registry, broker, _sent, _reactions = self._make_broker()
        try:
            registry.set_default_timezone("UTC")
            principal = f"buzz:posspecialists:{'bb' * 32}"
            header = broker._format_prompt(
                BrokerMessage(
                    platform="buzz",
                    chat_id=BUZZ_CHANNEL,
                    chat_title="#general",
                    sender_name="bRaD",
                    sender_id=principal,
                    content="hello",
                    agent_name="barsik",
                    timestamp=0,
                    is_group=True,
                    metadata={"buzz_verified_principal": principal},
                )
            ).splitlines()[0]
            assert "display_name(untrusted+collides:Brad):bRaD" in header
            assert f"principal:{principal}" in header
        finally:
            tmpdir.cleanup()

    def test_buzz_absent_verified_contacts_table_falls_back_without_exception(self):
        tmpdir, registry, broker, _sent, _reactions = self._make_broker()
        try:
            registry.set_default_timezone("UTC")
            registry._db.execute("DROP TABLE verified_contacts")
            registry._db.commit()
            principal = f"buzz:posspecialists:{'cc' * 32}"
            header = broker._format_prompt(
                BrokerMessage(
                    platform="buzz",
                    chat_id=BUZZ_CHANNEL,
                    chat_title="#general",
                    sender_name="Somebody",
                    sender_id=principal,
                    content="hello",
                    agent_name="barsik",
                    timestamp=0,
                    is_group=True,
                    metadata={"buzz_verified_principal": principal},
                )
            ).splitlines()[0]
            assert "display_name(untrusted):Somebody" in header
            assert f"principal:{principal}" in header
        finally:
            tmpdir.cleanup()

    def test_buzz_channel_id_is_rendered_exactly_once_with_or_without_alias(self):
        tmpdir, registry, broker, _sent, _reactions = self._make_broker()
        try:
            registry.set_default_timezone("UTC")
            registry.upsert_group_chat(
                "barsik", BUZZ_CHANNEL, chat_type="channel", platform="buzz"
            )
            principal = f"buzz:posspecialists:{'dd' * 32}"
            message = BrokerMessage(
                platform="buzz",
                chat_id=BUZZ_CHANNEL,
                sender_name="Somebody",
                sender_id=principal,
                content="hello",
                agent_name="barsik",
                timestamp=0,
                is_group=True,
                metadata={"buzz_verified_principal": principal},
            )
            raw_header = broker._format_prompt(message).splitlines()[0]
            assert raw_header.startswith(f"[buzz | {BUZZ_CHANNEL} |")
            assert raw_header.count(BUZZ_CHANNEL) == 1

            assert registry.update_group_chat_alias("barsik", BUZZ_CHANNEL, "#general")
            named_header = broker._format_prompt(message).splitlines()[0]
            assert named_header.startswith("[buzz | #general |")
            assert named_header.count(BUZZ_CHANNEL) == 1
            assert f"chat_id:{BUZZ_CHANNEL}" in named_header
        finally:
            tmpdir.cleanup()

    def test_non_buzz_group_and_dm_headers_remain_byte_identical(self):
        tmpdir, registry, broker, _sent, _reactions = self._make_broker()
        try:
            registry.set_default_timezone("UTC")
            group = broker._format_prompt(
                BrokerMessage(
                    platform="telegram",
                    chat_id="-100123",
                    chat_title="Ops",
                    sender_name="Alex",
                    sender_id="42",
                    content="group body",
                    agent_name="barsik",
                    message_id="7",
                    timestamp=0,
                    is_group=True,
                )
            )
            dm = broker._format_prompt(
                BrokerMessage(
                    platform="telegram",
                    chat_id="42",
                    sender_name="Alex",
                    sender_id="42",
                    content="dm body",
                    agent_name="barsik",
                    message_id="8",
                    timestamp=0,
                    is_group=False,
                )
            )
            assert group == (
                "[telegram | group | Ops | Alex | -100123 | "
                "1970-01-01 00:00:00 UTC | msg_id:7]\ngroup body"
            )
            assert dm == (
                "[telegram | dm | Alex | 42 | "
                "1970-01-01 00:00:00 UTC | msg_id:8]\ndm body"
            )
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_suppresses_fallback_on_external_dm(self):
        # Owner rule (Brad, 2026-06-22): plain-text fallback NEVER auto-delivers
        # to an outreach channel — not even a 1:1 owner DM with a valid stored
        # context. Agents must call an explicit outreach tool to reach external
        # channels; bare text only surfaces on internal surfaces (web/api/console).
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            broker.remember_message_context(
                BrokerMessage(
                    platform="telegram",
                    chat_id="6770805286",
                    sender_name="Brad",
                    sender_id="u-1",
                    content="ping",
                    agent_name="barsik",
                    message_id="42",
                    is_group=False,
                )
            )
            await broker.route_response(
                "barsik",
                "telegram",
                "6770805286",
                "Ping from Barsik",
                message_id="42",
                used_outreach=False,
                fallback_enabled=True,
            )

            assert sent_messages == []
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
    async def test_route_response_suppresses_fallback_on_group_chat(self):
        # An agent's reasoning must NEVER auto-deliver to a group/public channel,
        # even with fallback enabled — regression guard for the Chekov Slack leak.
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            broker.remember_message_context(
                BrokerMessage(
                    platform="slack",
                    chat_id="C0PUBLIC",
                    sender_name="teammate",
                    sender_id="u-9",
                    content="team chatter",
                    agent_name="barsik",
                    message_id="555",
                    is_group=True,
                )
            )
            await broker.route_response(
                "barsik",
                "slack",
                "C0PUBLIC",
                "This isn't directed at me, I'll hold and stay quiet.",
                message_id="555",
                used_outreach=False,
                fallback_enabled=True,
            )

            assert sent_messages == []
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_allows_fallback_on_api_surface(self):
        # Internal surfaces (web/api/console) keep the fallback convenience and
        # need no message context — they're owner-only, never public channels.
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            await broker.route_response(
                "barsik",
                "api",
                "api",
                "api console reply via fallback",
                used_outreach=False,
                fallback_enabled=True,
            )

            assert sent_messages == [
                ("barsik", "api", "api", "api console reply via fallback"),
            ]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_suppresses_fallback_when_no_context(self):
        # Fail-closed: with no positive DM context for an external platform, the
        # fallback must NOT auto-deliver (absence is not authorization).
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            await broker.route_response(
                "barsik",
                "telegram",
                "-100groupchat",
                "reasoning with no stored context",
                message_id="not-remembered",
                used_outreach=False,
                fallback_enabled=True,
            )

            assert sent_messages == []
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_suppresses_fallback_on_context_collision(self):
        # Per-chat message ids collide: a DM context overwrites a group context
        # with the same message_id. Routing the group turn must still suppress —
        # the stored ctx.chat_id no longer matches the group chat (fail-closed).
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            broker.remember_message_context(
                BrokerMessage(
                    platform="telegram", chat_id="-100public", sender_name="x",
                    sender_id="u-1", content="group msg", agent_name="barsik",
                    message_id="42", is_group=True,
                )
            )
            broker.remember_message_context(  # same message_id, overwrites
                BrokerMessage(
                    platform="telegram", chat_id="6770805286", sender_name="Brad",
                    sender_id="u-2", content="dm msg", agent_name="barsik",
                    message_id="42", is_group=False,
                )
            )
            await broker.route_response(
                "barsik",
                "telegram",
                "-100public",
                "internal hold reasoning",
                message_id="42",
                used_outreach=False,
                fallback_enabled=True,
            )

            assert sent_messages == []
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_response_allows_fallback_on_web_without_context(self):
        # Owner-only surfaces (web/api/internal) are never public channels and
        # carry no message context — the fallback convenience is preserved.
        tmpdir, _, broker, sent_messages, _ = self._make_broker()
        try:
            await broker.route_response(
                "barsik",
                "web",
                "web",
                "web console reply via fallback",
                used_outreach=False,
                fallback_enabled=True,
            )

            assert sent_messages == [
                ("barsik", "web", "web", "web console reply via fallback"),
            ]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_inject_agent_message_stamps_last_seen_on_success(self):
        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            from pinky_daemon.transport_state import SessionState

            class _FakeStreaming:
                state = SessionState.CONNECTED
                sent: list = []

                async def send(self, prompt, *, platform="", chat_id="", message_id=""):
                    _FakeStreaming.sent.append(prompt)
                    return True

            broker.register_streaming("barsik", _FakeStreaming(), label="main")
            assert registry.get("barsik").last_seen_at == 0.0

            delivered, confirmed = await broker.inject_agent_message("pushok", "barsik", "hi")
            assert delivered is True
            # Handoff succeeded but the fake advertises no capability →
            # fail-closed unconfirmed.
            assert confirmed is False
            assert registry.get("barsik").last_seen_at > 0.0
            assert _FakeStreaming.sent  # delivery happened
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_inject_agent_message_does_not_stamp_when_not_connected(self):
        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            # No streaming session registered — inject should fail without stamping.
            delivered, confirmed = await broker.inject_agent_message("pushok", "barsik", "hi")
            assert delivered is False
            assert confirmed is False
            assert registry.get("barsik").last_seen_at == 0.0
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_inject_agent_message_stamps_reply_routing_metadata(self):
        """#279: an injected agent message carries platform='agent' + chat_id=
        the requester, so the recipient's completed turn can route back."""
        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            from pinky_daemon.transport_state import SessionState

            recorded: list[tuple[str, str]] = []

            class _FakeStreaming:
                state = SessionState.CONNECTED

                async def send(self, prompt, *, platform="", chat_id="", message_id=""):
                    recorded.append((platform, chat_id))
                    return True

            broker.register_streaming("barsik", _FakeStreaming(), label="main")
            delivered, _ = await broker.inject_agent_message("pushok", "barsik", "review please")
            assert delivered is True
            assert recorded == [("agent", "pushok")]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_inject_agent_message_respects_routing_killswitch(self, monkeypatch):
        """With PINKY_AGENT_REPLY_ROUTING off, no route-back metadata is stamped —
        reverts to the legacy (drop-on-empty-chat_id) behavior."""
        import pinky_daemon.broker as broker_mod

        monkeypatch.setattr(broker_mod, "_AGENT_REPLY_ROUTING_ENABLED", False)
        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            from pinky_daemon.transport_state import SessionState

            recorded: list[tuple[str, str]] = []

            class _FakeStreaming:
                state = SessionState.CONNECTED

                async def send(self, prompt, *, platform="", chat_id="", message_id=""):
                    recorded.append((platform, chat_id))
                    return True

            broker.register_streaming("barsik", _FakeStreaming(), label="main")
            delivered, _ = await broker.inject_agent_message("pushok", "barsik", "hi")
            assert delivered is True
            assert recorded == [("", "")]
        finally:
            tmpdir.cleanup()

    # ── Fail-closed delivery + check-inbox nudge (#215 PR3) ────────
    # Confirmation is per-inject: computed by inject_agent_message on the SAME
    # session object it injected through — transport capability AND the
    # per-call send() handoff (Murzik #853 P1).

    @pytest.mark.asyncio
    async def test_inject_result_fail_closed_without_session(self):
        """No live session → (delivered=False, confirmed=False): never retire
        the durable copy."""
        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            result = await broker.inject_agent_message("pushok", "barsik", "hi")
            assert result == (False, False)
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_inject_unconfirmed_for_tmux_transport(self):
        """A tmux-style transport (capability False) never confirms even on a
        successful enqueue handoff — its inject is best-effort behind an
        external pane → (True, False)."""
        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            from pinky_daemon.transport_state import SessionState

            class _Tmuxish:
                state = SessionState.CONNECTED
                injection_confirms_consumption = False

                async def send(self, *a, **k):
                    return True  # enqueue succeeded — still not consumption

            broker.register_streaming("barsik", _Tmuxish(), label="main")
            delivered, confirmed = await broker.inject_agent_message("pushok", "barsik", "hi")
            assert delivered is True
            assert confirmed is False
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_inject_confirmed_for_sdk_transport_success(self):
        """SDK-style transport (capability True) + successful per-call handoff
        → (True, True): the durable copy may be retired."""
        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            from pinky_daemon.transport_state import SessionState

            class _Sdkish:
                state = SessionState.CONNECTED
                injection_confirms_consumption = True

                async def send(self, *a, **k):
                    return True  # client.query() accepted this message

            broker.register_streaming("barsik", _Sdkish(), label="main")
            delivered, confirmed = await broker.inject_agent_message("pushok", "barsik", "hi")
            assert delivered is True
            assert confirmed is True
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_inject_unconfirmed_when_sdk_handoff_fails(self):
        """A failed per-call handoff is not live delivery."""
        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            from pinky_daemon.transport_state import SessionState

            class _SdkishFailing:
                state = SessionState.CONNECTED
                injection_confirms_consumption = True

                async def send(self, *a, **k):
                    return False  # query raised; exception swallowed upstream

            broker.register_streaming("barsik", _SdkishFailing(), label="main")
            delivered, confirmed = await broker.inject_agent_message("pushok", "barsik", "hi")
            assert delivered is False
            assert confirmed is False
        finally:
            tmpdir.cleanup()

    class _NudgeComms:
        def __init__(self, unread: int):
            self._unread = unread

        def unread_count(self, session_id: str) -> int:
            return self._unread

    def _tmux_nudge_session(self):
        from pinky_daemon.transport_state import SessionState

        class _TmuxNudge:
            state = SessionState.CONNECTED
            injection_confirms_consumption = False

            def __init__(self):
                self.nudges: list[tuple[str, str]] = []

            async def send(self, *a, **k):
                pass

            async def _enqueue_internal_prompt(self, prompt, *, reason, **kw):
                self.nudges.append((prompt, reason))

        return _TmuxNudge()

    @pytest.mark.asyncio
    async def test_notify_unread_is_deprecated_noop(self):
        """The compatibility method never emits an unread-inbox nudge."""
        tmpdir, _, broker, _, _ = self._make_broker()
        try:
            session = self._tmux_nudge_session()
            broker.register_streaming("barsik", session, label="main")
            fired = await broker.notify_unread_agent_messages(self._NudgeComms(2), "barsik")
            assert fired is False
            assert session.nudges == []
            assert broker._stats["nudged"] == 0
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_agent_reply_is_one_way_live_injection(self):
        """The return leg is live, audited, and cannot trigger another reply."""
        from pinky_daemon.transport_state import SessionState
        from pinky_daemon.turn_response import TurnResponse

        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            sent: list = []
            injected: list = []

            class _FakeComms:
                def send(self, frm, to, content, **kw):
                    sent.append((frm, to, content, kw.get("metadata")))

            class _FakeStreaming:
                state = SessionState.CONNECTED
                injection_confirms_consumption = True

                async def send(self, prompt, *, platform="", chat_id="", message_id=""):
                    injected.append((prompt, platform, chat_id))
                    return True

            broker.register_streaming("barsik", _FakeStreaming(), label="main")
            tr = TurnResponse(
                agent_name="murzik",
                platform="agent",
                chat_id="barsik",
                text="LGTM - ship it",
            )
            handled = await broker.route_agent_reply(_FakeComms(), tr)
            assert handled is True
            assert sent == [("murzik", "barsik", "LGTM - ship it", {"auto_routed": True})]
            assert len(injected) == 1
            assert "LGTM - ship it" in injected[0][0]
            assert injected[0][1:] == ("", "")
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_agent_reply_ignores_normal_platform_turns(self):
        """A telegram turn is not an agent reply: returns False (caller falls
        through to normal routing) and nothing is agent-routed."""
        from pinky_daemon.turn_response import TurnResponse

        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            sent: list = []

            class _FakeComms:
                def send(self, *a, **k):
                    sent.append((a, k))

            tr = TurnResponse(
                agent_name="barsik", platform="telegram", chat_id="123", text="hi"
            )
            handled = await broker.route_agent_reply(_FakeComms(), tr)
            assert handled is False
            assert sent == []
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_agent_reply_skips_empty_text(self):
        """A pure tool-call agent turn (no final text) is consumed but nothing is
        delivered."""
        from pinky_daemon.turn_response import TurnResponse

        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            sent: list = []

            class _FakeComms:
                def send(self, *a, **k):
                    sent.append((a, k))

            tr = TurnResponse(
                agent_name="murzik", platform="agent", chat_id="barsik", text="   "
            )
            handled = await broker.route_agent_reply(_FakeComms(), tr)
            assert handled is True
            assert sent == []
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_agent_reply_counts_failed_delivery(self):
        """If audit storage raises, the turn is still consumed (handled True, never
        re-injected) and the failure is counted so dropped replies are visible."""
        from pinky_daemon.turn_response import TurnResponse

        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            class _BoomComms:
                def send(self, *a, **k):
                    raise RuntimeError("audit down")

            tr = TurnResponse(
                agent_name="murzik", platform="agent", chat_id="barsik", text="x"
            )
            before = broker._stats["routed_failed"]
            handled = await broker.route_agent_reply(_BoomComms(), tr)
            assert handled is True  # loop-safe: never falls through / re-injects
            assert broker._stats["routed_failed"] == before + 1
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_route_agent_reply_real_comms_is_audited_and_live(self):
        """End-to-end return leg is live and retained only in audit history."""
        from pinky_daemon.agent_comms import AgentComms
        from pinky_daemon.transport_state import SessionState
        from pinky_daemon.turn_response import TurnResponse

        tmpdir, registry, broker, _, _ = self._make_broker()
        try:
            comms = AgentComms(db_path=f"{tmpdir.name}/comms.db")
            injected: list = []

            class _FakeStreaming:
                state = SessionState.CONNECTED
                injection_confirms_consumption = True

                async def send(self, prompt, *, platform="", chat_id="", message_id=""):
                    injected.append((prompt, platform, chat_id))
                    return True

            broker.register_streaming("barsik", _FakeStreaming(), label="main")
            tr = TurnResponse(
                agent_name="murzik",
                platform="agent",
                chat_id="barsik",
                text="LGTM - ship it",
            )
            handled = await broker.route_agent_reply(comms, tr)
            assert handled is True
            assert comms.get_inbox("barsik") == []
            assert comms.unread_count("barsik") == 0
            messages, total = comms.get_all_messages()
            assert total == 1
            assert messages[0].from_session == "murzik"
            assert messages[0].content == "LGTM - ship it"
            assert messages[0].metadata.get("auto_routed") is True
            assert len(injected) == 1
            assert "LGTM - ship it" in injected[0][0]
            assert injected[0][1:] == ("", "")
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
    async def test_group_message_gates_on_channel_and_routes_back(self, monkeypatch):
        """A group/channel message gates on the CHANNEL (not the sender): it's
        held pending under the channel id, the owner is notified with
        /approve_<channel>, and the in-channel "waiting for approval" notice is
        suppressed (it would be noise to every member). On approval the held
        message re-delivers to the channel with is_group preserved."""
        tmpdir, registry, broker, sent_messages, _ = self._make_broker()
        try:
            routed: list[BrokerMessage] = []

            async def _fake_route(agent_name, message):
                routed.append(message)

            monkeypatch.setattr(broker, "_route_streaming", _fake_route)
            owner = "owner-1"
            registry.set_primary_user(owner, display_name="Brad")
            registry.set_owner_notification_destinations([
                {
                    "platform": "telegram",
                    "account_id": "owner-bot",
                    "conversation_id": owner,
                    "principal_id": owner,
                }
            ])

            channel = "C0A8WUU743F"
            user = "U0EXAMPLE1"
            await broker.handle_inbound(
                BrokerMessage(
                    platform="slack",
                    chat_id=channel,
                    sender_name="Alex Ugrin",
                    sender_id=user,
                    content="Hi",
                    agent_name="barsik",
                    is_group=True,
                )
            )

            # Gated on the CHANNEL, not the individual sender.
            assert registry.get_user_status("barsik", channel) == "pending"
            assert registry.get_user_status("barsik", user) is None
            assert routed == []
            # No in-channel "waiting for approval" spam...
            assert not any("Waiting for approval" in m[3] for m in sent_messages)
            # ...but the owner gets a /approve_<channel> prompt.
            assert any(
                m[2] == owner and f"/approve_{channel}" in m[3] for m in sent_messages
            )
            # Pending row: approval key = channel, destination = channel, is_group.
            pending = registry.get_pending_messages("barsik", channel)
            assert len(pending) == 1
            assert pending[0]["chat_id"] == channel
            assert pending[0]["reply_chat_id"] == channel
            assert pending[0]["is_group"] is True

            # Approve the channel → held message re-delivers to the channel.
            registry.approve_user("barsik", channel, display_name=channel)
            delivered = await broker.handle_approval("barsik", channel)

            assert delivered == 1
            assert len(routed) == 1
            assert routed[0].chat_id == channel
            assert routed[0].sender_id == user
            assert routed[0].is_group is True
            assert routed[0].content == "Hi"
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_owner_notification_uses_full_tuple_with_legacy_gate_key(self):
        """#863: destination identity is composite; approval rows are not yet."""
        tmpdir, registry, broker, _sent, _ = self._make_broker()
        deliveries: list[tuple[str, str, str, str]] = []

        async def capture(
            agent_name, platform, chat_id, content, *, account_id="",
        ):
            if platform == "slack":
                raise RuntimeError("owner primary unavailable")
            deliveries.append((platform, account_id, chat_id, content))

        broker._send_callback = capture
        try:
            registry.set_primary_user("legacy-owner-id", display_name="Brad")
            registry.set_owner_notification_destinations([
                {
                    "platform": "slack",
                    "account_id": "T_OWNER",
                    "conversation_id": "D_OWNER",
                    "principal_id": "U_OWNER",
                },
                {
                    "platform": "telegram",
                    "account_id": "owner-telegram-account",
                    "conversation_id": "owner-conversation",
                    "principal_id": "legacy-owner-id",
                }
            ])
            channel = "C_GATED"
            await broker.handle_inbound(
                BrokerMessage(
                    platform="slack", chat_id=channel, sender_name="Alex",
                    sender_id="U_ALEX", content="hello", agent_name="barsik",
                    is_group=True,
                )
            )

            assert len(deliveries) == 1
            assert deliveries[0][:3] == (
                "telegram", "owner-telegram-account", "owner-conversation",
            )
            assert f"/approve_{channel}" in deliveries[0][3]

            request = registry.get_approval_request("barsik", channel)
            assert request is not None
            assert request["chat_id"] == channel
            assert request["notification_destination"]["platform"] == "telegram"
            assert request["fallback_path"][0]["destination"]["platform"] == "slack"
            columns = {
                row[1]
                for row in registry._db.execute(
                    "PRAGMA table_info(approval_requests)"
                ).fetchall()
            }
            assert "platform" not in columns
            assert "account_id" not in columns
            assert "conversation_id" not in columns
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_fallback_recipient_can_approve_and_deny_fail_closed(
        self, monkeypatch,
    ):
        """Fallback DM principal gets usable commands; wrong authority gets none."""
        tmpdir, registry, broker, _sent, _ = self._make_broker()
        sends: list[tuple[str, str, str, str]] = []
        routed: list[BrokerMessage] = []

        async def send(
            agent_name, platform, chat_id, content, *, account_id="",
        ):
            if account_id == "tg-owner-account":
                raise RuntimeError("telegram owner destination unavailable")
            sends.append((platform, account_id, chat_id, content))

        async def route(agent_name, message):
            routed.append(message)

        broker._send_callback = send
        monkeypatch.setattr(broker, "_route_streaming", route)
        try:
            registry.set_primary_user("legacy-telegram-owner", display_name="Brad")
            registry.set_token(
                "barsik", "telegram", "tg-token",
                settings={"account_id": "tg-owner-account"},
            )
            registry.set_token(
                "barsik", "slack", "xoxb-token",
                settings={"team_id": "T_OWNER"},
            )
            registry.set_owner_notification_destinations([
                {
                    "platform": "telegram",
                    "account_id": "tg-owner-account",
                    "conversation_id": "legacy-telegram-owner",
                    "principal_id": "legacy-telegram-owner",
                },
                {
                    "platform": "slack",
                    "account_id": "T_OWNER",
                    "conversation_id": "D_OWNER",
                    "principal_id": "U_OWNER",
                },
            ])

            approve_channel = "C_APPROVE"
            await broker.handle_inbound(
                BrokerMessage(
                    platform="slack", chat_id=approve_channel, sender_name="Alex",
                    sender_id="U_ALEX", content="approve me", agent_name="barsik",
                    is_group=True,
                )
            )
            request = registry.get_approval_request("barsik", approve_channel)
            assert request["notification_destination"] == {
                "platform": "slack", "account_id": "T_OWNER",
                "conversation_id": "D_OWNER", "principal_id": "U_OWNER",
            }
            assert sends[0][0:3] == ("slack", "T_OWNER", "D_OWNER")

            command = BrokerMessage(
                platform="slack", chat_id="D_OWNER", sender_name="Brad",
                sender_id="U_OWNER", content=f"/approve_{approve_channel}",
                agent_name="barsik",
            )
            # Public-channel and wrong-principal copies are never authorized.
            public_command = BrokerMessage(**{**command.__dict__, "is_group": True})
            wrong_principal = BrokerMessage(**{**command.__dict__, "sender_id": "U_EVIL"})
            assert await broker._handle_approval_command(public_command) is False
            assert await broker._handle_approval_command(wrong_principal) is False
            assert registry.get_user_status("barsik", approve_channel) == "pending"

            # Token/account drift after delivery also fails closed with zero
            # gate mutation. Restoring the exact binding makes the command usable.
            registry.set_token(
                "barsik", "slack", "xoxb-token",
                settings={"team_id": "T_OTHER"},
            )
            assert await broker._handle_approval_command(command) is False
            assert registry.get_user_status("barsik", approve_channel) == "pending"
            registry.set_token(
                "barsik", "slack", "xoxb-token",
                settings={"team_id": "T_OWNER"},
            )
            await broker.handle_inbound(command)
            assert registry.get_user_status("barsik", approve_channel) == "approved"
            assert [message.content for message in routed] == ["approve me"]

            deny_channel = "C_DENY"
            await broker.handle_inbound(
                BrokerMessage(
                    platform="slack", chat_id=deny_channel, sender_name="Alex",
                    sender_id="U_ALEX", content="deny me", agent_name="barsik",
                    is_group=True,
                )
            )
            await broker.handle_inbound(
                BrokerMessage(
                    platform="slack", chat_id="D_OWNER", sender_name="Brad",
                    sender_id="U_OWNER", content=f"/deny_{deny_channel}",
                    agent_name="barsik",
                )
            )
            assert registry.get_user_status("barsik", deny_channel) == "denied"
            assert registry.get_approval_request("barsik", deny_channel)["gate_state"] == "denied"
            assert len(routed) == 1
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_owner_notification_failure_retries_and_is_operator_visible(
        self, monkeypatch,
    ):
        import pinky_daemon.broker as broker_mod

        monkeypatch.setattr(broker_mod, "_APPROVAL_NOTIFY_RETRY_BASE_SEC", 0)
        tmpdir, registry, broker, _sent, _ = self._make_broker()
        attempts = 0

        async def flaky(
            agent_name, platform, chat_id, content, *, account_id="",
        ):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RuntimeError("channel_not_found")

        broker._send_callback = flaky
        try:
            registry.set_primary_user("owner", display_name="Brad")
            registry.set_owner_notification_destinations([
                {
                    "platform": "telegram",
                    "account_id": "owner-account",
                    "conversation_id": "owner",
                    "principal_id": "owner",
                }
            ])
            await broker.handle_inbound(
                BrokerMessage(
                    platform="slack", chat_id="C_RETRY", sender_name="Alex",
                    sender_id="U_ALEX", content="hello", agent_name="barsik",
                    is_group=True,
                )
            )

            request = registry.get_approval_request("barsik", "C_RETRY")
            assert request["notification_state"] == "retrying"
            health = registry.get_approval_notification_health("barsik")
            assert health["healthy"] is False
            assert health["retrying"] == 1
            assert "channel_not_found" in health["requests"][0]["last_error"]

            assert await broker.retry_due_approval_notifications() == 1
            request = registry.get_approval_request("barsik", "C_RETRY")
            assert request["notification_state"] == "delivered"
            assert attempts == 2
            assert registry.get_approval_notification_health("barsik")["healthy"] is True
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_repeat_contacts_aggregate_with_bounded_renotify(self, monkeypatch):
        import pinky_daemon.broker as broker_mod

        monkeypatch.setattr(broker_mod, "_APPROVAL_RENOTIFY_HELD_COUNT", 2)
        monkeypatch.setattr(broker_mod, "_APPROVAL_RENOTIFY_INTERVAL_SEC", 999999)
        tmpdir, registry, broker, _sent, _ = self._make_broker()
        notifications: list[str] = []

        async def capture(
            agent_name, platform, chat_id, content, *, account_id="",
        ):
            notifications.append(content)

        broker._send_callback = capture
        try:
            registry.set_primary_user("owner", display_name="Brad")
            registry.set_owner_notification_destinations([
                {
                    "platform": "telegram",
                    "account_id": "owner-account",
                    "conversation_id": "owner",
                    "principal_id": "owner",
                }
            ])
            channel = "C_BURST"
            request_ids: list[int] = []
            for index in range(3):
                await broker.handle_inbound(
                    BrokerMessage(
                        platform="slack", chat_id=channel, sender_name="Alex",
                        sender_id="U_ALEX", content=f"message {index}",
                        agent_name="barsik", is_group=True,
                    )
                )
                request_ids.append(
                    registry.get_approval_request("barsik", channel)["id"]
                )

            request = registry.get_approval_request("barsik", channel)
            assert len(set(request_ids)) == 1
            assert request["held_count"] == 3
            assert len(registry.get_pending_messages("barsik", channel)) == 3
            assert len(notifications) == 2
            assert "Held messages: 1" in notifications[0]
            assert "Held messages: 3" in notifications[1]
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_channel_approval_admits_all_members(self, monkeypatch):
        """Core of #241: once a CHANNEL is approved, every member's messages
        flow without per-user approval. A user who was never individually
        approved routes straight through in an approved channel."""
        tmpdir, registry, broker, sent_messages, _ = self._make_broker()
        try:
            routed: list[BrokerMessage] = []

            async def _fake_route(agent_name, message):
                routed.append(message)

            monkeypatch.setattr(broker, "_route_streaming", _fake_route)
            registry.set_primary_user("owner-1", display_name="Brad")

            channel = "C0A8WUU743F"
            # Channel is approved once.
            registry.approve_user("barsik", channel, display_name=channel)

            # A brand-new, never-approved member messages in the channel.
            await broker.handle_inbound(
                BrokerMessage(
                    platform="slack", chat_id=channel, sender_name="Jake Hredzak",
                    sender_id="U_NEVER_APPROVED", content="status?",
                    agent_name="barsik", is_group=True,
                )
            )

            # Routed straight through — no pending, no approval prompt.
            assert len(routed) == 1
            assert routed[0].chat_id == channel
            assert routed[0].sender_id == "U_NEVER_APPROVED"
            assert registry.get_pending_messages("barsik", channel) == []
            assert not any("approve" in m[3].lower() for m in sent_messages)
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_dm_pending_reply_routes_back_to_sender(self, monkeypatch):
        """DM (1:1): sender == destination. A held DM re-delivers to the same
        chat on approval — reply_chat_id defaults to chat_id, so DM behavior is
        unchanged by the group-routing fix."""
        tmpdir, registry, broker, _sent, _ = self._make_broker()
        try:
            routed: list[BrokerMessage] = []

            async def _fake_route(agent_name, message):
                routed.append(message)

            monkeypatch.setattr(broker, "_route_streaming", _fake_route)
            registry.set_primary_user("owner-1", display_name="Brad")

            dm_user = "999"
            await broker.handle_inbound(
                BrokerMessage(
                    platform="telegram",
                    chat_id=dm_user,
                    sender_name="Stranger",
                    sender_id=dm_user,
                    content="let me in",
                    agent_name="barsik",
                )
            )
            assert registry.get_user_status("barsik", dm_user) == "pending"
            pending = registry.get_pending_messages("barsik", dm_user)
            assert pending[0]["reply_chat_id"] == dm_user  # defaulted to chat_id

            registry.approve_user("barsik", dm_user, display_name="Stranger")
            delivered = await broker.handle_approval("barsik", dm_user)
            assert delivered == 1
            assert routed[0].chat_id == dm_user
            assert routed[0].sender_id == dm_user
        finally:
            tmpdir.cleanup()

    def test_queue_pending_message_preserves_reply_chat_id_and_is_group(self):
        """reply_chat_id is stored distinctly from the approval-key chat_id
        (defaulting to chat_id when omitted), and is_group is persisted."""
        tmpdir, registry, _broker, _sent, _ = self._make_broker()
        try:
            registry.queue_pending_message(
                agent_name="barsik", platform="slack", chat_id="Cchan",
                sender_name="Alex", content="hi", reply_chat_id="Cchan",
                is_group=True,
            )
            registry.queue_pending_message(
                agent_name="barsik", platform="telegram", chat_id="999",
                sender_name="Stranger", content="yo",
            )
            rows = registry.get_pending_messages("barsik")
            by_key = {r["chat_id"]: r for r in rows}
            assert by_key["Cchan"]["reply_chat_id"] == "Cchan"
            assert by_key["Cchan"]["is_group"] is True
            assert by_key["999"]["reply_chat_id"] == "999"   # defaulted to chat_id
            assert by_key["999"]["is_group"] is False
        finally:
            tmpdir.cleanup()

    @pytest.mark.asyncio
    async def test_slash_approve_preserves_uppercase_channel_id(self, monkeypatch):
        """Regression: /approve_<id> must preserve the EXACT case of the target
        id. Slack channel ids are uppercase (C0A8WUU743F); lowercasing the whole
        command token approved a phantom lowercased id, delivered 0 held
        messages, and left the channel unapproved — so replies never went out.
        Exercises the real owner-notification flow end-to-end at channel scope."""
        tmpdir, registry, broker, sent_messages, _ = self._make_broker()
        try:
            routed: list[BrokerMessage] = []

            async def _fake_route(agent_name, message):
                routed.append(message)

            monkeypatch.setattr(broker, "_route_streaming", _fake_route)
            owner = "owner-1"
            registry.set_primary_user(owner, display_name="Brad")
            registry.set_token(
                "barsik", "slack", "xoxb-test",
                settings={"team_id": "T_OWNER"},
            )
            registry.set_owner_notification_destinations([
                {
                    "platform": "slack",
                    "account_id": "T_OWNER",
                    "conversation_id": owner,
                    "principal_id": owner,
                }
            ])

            channel = "C0A8WUU743F"
            # A message in the channel → held pending under the exact channel id.
            await broker.handle_inbound(
                BrokerMessage(
                    platform="slack", chat_id=channel, sender_name="Alex Ugrin",
                    sender_id="U0EXAMPLE1", content="Hi", agent_name="barsik",
                    is_group=True,
                )
            )
            assert registry.get_user_status("barsik", channel) == "pending"

            # Owner approves exactly as the notification prints it: /approve_C…
            await broker.handle_inbound(
                BrokerMessage(
                    platform="slack", chat_id=owner, sender_name="Brad",
                    sender_id=owner, content=f"/approve_{channel}", agent_name="barsik",
                )
            )

            # The exact uppercase channel id is approved — no lowercased phantom.
            assert registry.get_user_status("barsik", channel) == "approved"
            assert registry.get_user_status("barsik", channel.lower()) is None
            # The held message is delivered to the CHANNEL.
            assert len(routed) == 1
            assert routed[0].chat_id == channel
            assert routed[0].content == "Hi"
            # Owner saw a non-zero delivery count (not "0 pending message(s)").
            assert any("1 pending message" in m[3] for m in sent_messages)
            # No approval notice posted INTO the channel — that would be the same
            # in-channel noise the pending path suppresses (channel approval is
            # owner business; the held-message delivery is the visible signal).
            assert not any(
                m[2] == channel and "approved" in m[3].lower() for m in sent_messages
            )
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
    async def test_route_streaming_top_level_reply_hint_prefers_send_tool(self):
        """The agent reply hint must reference real pinky-messaging tools
        (send/thread), not the non-existent send_message()/reply() that the
        old hint named. Regression for Brad's 2026-05-29 report."""
        hint = await self._capture_reply_hint()

        assert hint, "no agent_hint passed to streaming.send()"
        assert "send_message()" not in hint
        assert "reply()" not in hint
        assert "Reply on telegram using pinky-messaging" in hint
        assert 'send(chat_id="6770805286"' in hint
        assert 'platform="telegram"' in hint
        assert 'thread(message_id="9999"' in hint
        assert hint.index("send(") < hint.index("thread(")
        assert "quote/thread-reply to this message" in hint

    @pytest.mark.asyncio
    async def test_route_streaming_thread_reply_hint_prefers_thread_tool(self):
        hint = await self._capture_reply_hint(reply_to="thread-root")

        assert "Reply IN THREAD to this message" in hint
        assert 'thread(message_id="9999", text=...)' in hint
        assert 'send(chat_id="6770805286", platform="telegram", text=...)' in hint
        assert hint.index("thread(") < hint.index("send(")
        assert "posts to the channel OUTSIDE the thread" in hint

    @pytest.mark.asyncio
    async def test_route_streaming_reply_hint_without_message_id_has_no_thread_clause(self):
        hint = await self._capture_reply_hint(
            message_id="",
            reply_to="thread-root",
        )

        assert "Reply on telegram using pinky-messaging" in hint
        assert 'send(chat_id="6770805286", platform="telegram", text=...)' in hint
        assert "thread(" not in hint
        assert "Reply IN THREAD" not in hint

    @pytest.mark.parametrize("platform", ["web", "api", ""])
    @pytest.mark.asyncio
    async def test_route_streaming_internal_platforms_have_no_reply_hint(self, platform):
        assert await self._capture_reply_hint(platform=platform) == ""

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
    async def test_route_streaming_uses_ensurer_for_existing_idle_session(self):
        """If a session object exists with a persisted session_id but is
        idle-sleeping, production routes its reconnect through the ensurer so
        registry-backed launch configuration is refreshed first (#856).
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
                await ss.connect()
                return ss

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

            # The ensurer owns the existing session's one reconnect.
            assert _FakeStreaming.connect_calls == 1
            assert ensure_calls == [("lera", "main")]
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
