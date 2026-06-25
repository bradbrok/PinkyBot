"""Tests for BrokerSlackPoller (Socket Mode inbound).

These exercise the event-handling/normalization/self-filter logic directly via
``_handle_event`` (the cleanest unit surface) plus the no-app-token start guard.
They intentionally do NOT require ``slack_sdk`` — the SDK import is lazy inside
``start()``, and these paths never reach it.
"""

from __future__ import annotations

import asyncio
import sys
import types
from unittest.mock import AsyncMock, MagicMock

import pytest


@pytest.fixture
def mock_broker():
    broker = MagicMock()
    broker.handle_inbound = AsyncMock()
    return broker


def _make_poller(broker, *, app_token="xapp-test", bot_user_id="UBOT", bot_id="B_SELF"):
    from pinky_daemon.pollers import BrokerSlackPoller

    adapter = MagicMock()
    adapter.get_bot_info.return_value = {
        "user_id": "UBOT", "bot_id": "B_SELF", "user": "testbot", "team": "T1",
    }
    adapter.bot_token = "xoxb-test"
    poller = BrokerSlackPoller(adapter, "barsik", broker, registry=None, app_token=app_token)
    # Normally set during start() from auth.test; set directly for unit tests.
    poller._bot_user_id = bot_user_id
    poller._bot_id = bot_id
    return poller


def _event(event_body: dict) -> dict:
    """Wrap a Slack message event in the Socket Mode events_api payload shape."""
    return {"event": event_body}


class TestBrokerSlackPoller:
    @pytest.mark.asyncio
    async def test_user_message_routed_to_broker(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message",
            "user": "U123",
            "text": "hello barsik",
            "channel": "C999",
            "channel_type": "channel",
            "ts": "1718830000.000100",
        }))
        await asyncio.sleep(0)  # let the fire-and-forget delivery task run
        mock_broker.handle_inbound.assert_called_once()
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert bmsg.platform == "slack"
        assert bmsg.chat_id == "C999"
        assert bmsg.sender_id == "U123"
        assert bmsg.content == "hello barsik"
        assert bmsg.message_id == "1718830000.000100"
        assert bmsg.reply_to == ""
        assert bmsg.is_group is True

    @pytest.mark.asyncio
    async def test_own_message_filtered(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message", "user": "UBOT", "text": "my own msg",
            "channel": "C1", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_subtype_filtered(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message", "subtype": "message_changed",
            "user": "U123", "text": "edited", "channel": "C1", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_peer_bot_message_delivered(self, mock_broker):
        """A peer agent/bot (bot_message subtype, different bot_id) must reach us
        (parity with the Discord poller)."""
        poller = _make_poller(mock_broker, bot_id="B_SELF")
        await poller._handle_event(_event({
            "type": "message", "subtype": "bot_message", "bot_id": "B_PEER",
            "username": "peerbot", "text": "from a peer agent",
            "channel": "C1", "channel_type": "channel", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_called_once()
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert bmsg.sender_id == "B_PEER"  # bot_id used as sender id when no user
        assert bmsg.content == "from a peer agent"

    @pytest.mark.asyncio
    async def test_own_bot_message_filtered(self, mock_broker):
        """Our own bot's posts echo back as bot_message with our bot_id — drop
        them (no self-reply loop)."""
        poller = _make_poller(mock_broker, bot_id="B_SELF")
        await poller._handle_event(_event({
            "type": "message", "subtype": "bot_message", "bot_id": "B_SELF",
            "text": "my own bot echo", "channel": "C1", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_bot_message_dropped_when_self_bot_id_unknown(self, mock_broker):
        """Fail closed: if we never resolved our own bot_id, we can't tell a peer
        bot from our own echo, so drop all bot messages."""
        poller = _make_poller(mock_broker, bot_id="")
        await poller._handle_event(_event({
            "type": "message", "subtype": "bot_message", "bot_id": "B_PEER",
            "text": "from a bot", "channel": "C1", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_peer_user_message_delivered(self, mock_broker):
        """A different real user (not us) must be delivered (cross-fleet parity)."""
        poller = _make_poller(mock_broker, bot_user_id="UBOT")
        await poller._handle_event(_event({
            "type": "message", "user": "UPEER", "text": "ping",
            "channel": "C1", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_called_once()

    @pytest.mark.asyncio
    async def test_non_message_event_ignored(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({"type": "reaction_added", "user": "U123"}))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_thread_reply_sets_reply_to(self, mock_broker):
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message", "user": "U123", "text": "in thread",
            "channel": "D1", "ts": "2.2", "thread_ts": "1.1",
            "channel_type": "im",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_called_once()
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert bmsg.reply_to == "1.1"
        assert bmsg.is_group is False  # im == direct message

    @pytest.mark.asyncio
    async def test_file_attachment_normalized(self, mock_broker):
        poller = _make_poller(mock_broker)
        # Real Slack file uploads carry subtype "file_share" — include it so the
        # test exercises the same payload Slack actually sends (a bare `files`
        # message with no subtype does not occur in practice).
        await poller._handle_event(_event({
            "type": "message", "subtype": "file_share",
            "user": "U123", "text": "see file",
            "channel": "C1", "ts": "3.3",
            "files": [{
                "id": "F1", "name": "doc.pdf",
                "url_private_download": "https://files.slack.com/doc.pdf",
                "mimetype": "application/pdf", "size": 1234,
            }],
        }))
        await asyncio.sleep(0)
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert len(bmsg.attachments) == 1
        att = bmsg.attachments[0]
        assert att["file_id"] == "F1"
        assert att["file_name"] == "doc.pdf"
        assert att["mime_type"] == "application/pdf"
        assert att["file_size"] == 1234

    @pytest.mark.asyncio
    async def test_file_share_screenshot_no_caption_delivered(self, mock_broker):
        """Regression: a screenshot (subtype `file_share`, often no text) must be
        DELIVERED with its image attachment, not dropped by the subtype filter.
        Before the fix, `file_share` was filtered out before attachment handling,
        so screenshots never reached the agent at all."""
        poller = _make_poller(mock_broker)
        await poller._handle_event(_event({
            "type": "message", "subtype": "file_share",
            "user": "U123", "text": "",  # caption-less screenshot
            "channel": "C1", "ts": "4.4",
            "files": [{
                "id": "F2", "name": "Screenshot.png",
                "url_private_download": "https://files.slack.com/shot.png",
                "mimetype": "image/png", "size": 5678,
            }],
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_called_once()
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert bmsg.content == ""
        assert len(bmsg.attachments) == 1
        assert bmsg.attachments[0]["mime_type"] == "image/png"
        assert bmsg.attachments[0]["file_name"] == "Screenshot.png"

    @pytest.mark.asyncio
    async def test_start_without_app_token_is_noop(self, mock_broker):
        poller = _make_poller(mock_broker, app_token="")
        await poller.start()
        assert poller._client is None
        assert poller._running is False
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_start_refuses_without_bot_user_id(self, mock_broker):
        """Fail closed: no user_id from auth.test => don't start (self-filter disarmed)."""
        poller = _make_poller(mock_broker, bot_user_id="")
        poller._adapter.get_bot_info.return_value = {"user": "x", "team": "T1"}  # no user_id
        await poller.start()
        assert poller._running is False
        assert poller._client is None
        mock_broker.handle_inbound.assert_not_called()

    @pytest.mark.asyncio
    async def test_double_start_is_benign(self, mock_broker):
        """A second start() on an already-running poller short-circuits (no client leak)."""
        poller = _make_poller(mock_broker)
        poller._running = True
        sentinel = object()
        poller._client = sentinel
        await poller.start()
        assert poller._client is sentinel  # not replaced

    @pytest.mark.asyncio
    async def test_event_callback_invoked_for_user_message(self, mock_broker):
        from pinky_daemon.pollers import BrokerSlackPoller

        adapter = MagicMock()
        adapter.bot_token = "xoxb-test"
        cb = AsyncMock()
        poller = BrokerSlackPoller(
            adapter, "barsik", mock_broker, registry=None,
            app_token="xapp-test", event_callback=cb,
        )
        poller._bot_user_id = "UBOT"
        await poller._handle_event(_event({
            "type": "message", "user": "U123", "text": "hello",
            "channel": "C999", "channel_type": "channel", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        cb.assert_awaited_once_with(
            platform="slack", chat_id="C999", sender="U123", content="hello",
        )

    @pytest.mark.asyncio
    async def test_start_connects_registers_listener_and_acks(self, monkeypatch, mock_broker):
        """Happy path: start() resolves identity, builds the client, registers the
        listener, connects; the listener ACKs an events_api envelope and routes it."""
        captured = {}

        class FakeSocketModeClient:
            def __init__(self, app_token=None, web_client=None):
                captured["app_token"] = app_token
                captured["web_client"] = web_client
                self.socket_mode_request_listeners = []
                self.connected = False
                self.responses = []

            async def connect(self):
                self.connected = True

            async def send_socket_mode_response(self, resp):
                self.responses.append(resp)

        class FakeSocketModeResponse:
            def __init__(self, envelope_id=None):
                self.envelope_id = envelope_id

        class FakeAsyncWebClient:
            def __init__(self, token=None):
                captured["web_token"] = token

        # Inject fake slack_sdk modules (incl. parents) so the lazy imports in
        # start() resolve to the fakes whether or not slack_sdk is installed.
        for name in ("slack_sdk", "slack_sdk.socket_mode", "slack_sdk.web"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        aiohttp_mod = types.ModuleType("slack_sdk.socket_mode.aiohttp")
        aiohttp_mod.SocketModeClient = FakeSocketModeClient
        response_mod = types.ModuleType("slack_sdk.socket_mode.response")
        response_mod.SocketModeResponse = FakeSocketModeResponse
        web_mod = types.ModuleType("slack_sdk.web.async_client")
        web_mod.AsyncWebClient = FakeAsyncWebClient
        monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode.aiohttp", aiohttp_mod)
        monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode.response", response_mod)
        monkeypatch.setitem(sys.modules, "slack_sdk.web.async_client", web_mod)

        poller = _make_poller(mock_broker)
        poller._bot_user_id = ""  # let start() populate it from get_bot_info
        poller._bot_id = ""
        await poller.start()

        assert poller._bot_user_id == "UBOT"
        assert poller._bot_id == "B_SELF"
        assert poller._running is True
        assert poller._client is not None
        assert poller._client.connected is True
        assert captured["app_token"] == "xapp-test"
        assert captured["web_token"] == "xoxb-test"  # AsyncWebClient(token=adapter.bot_token)
        assert len(poller._client.socket_mode_request_listeners) == 1

        # Drive a fake events_api request through the listener -> ACK + _handle_event.
        listener = poller._client.socket_mode_request_listeners[0]
        req = MagicMock()
        req.type = "events_api"
        req.envelope_id = "env-123"
        req.payload = _event({
            "type": "message", "user": "U123", "text": "hi",
            "channel": "C1", "channel_type": "channel", "ts": "9.9",
        })
        await listener(poller._client, req)
        await asyncio.sleep(0)
        assert len(poller._client.responses) == 1
        assert poller._client.responses[0].envelope_id == "env-123"  # ACKed
        mock_broker.handle_inbound.assert_called_once()

    # ── Identity resolution (users:read / channels:read) ──────────────

    @pytest.mark.asyncio
    async def test_user_and_channel_names_resolved(self, mock_broker):
        """users:read -> sender_name display name; channels:read -> chat_title.
        sender_id always stays the raw id (approval keys on it)."""
        poller = _make_poller(mock_broker)
        poller._adapter.get_user_info.return_value = {
            "user_id": "U123", "display_name": "Alice", "real_name": "Alice A",
            "name": "alice", "email": "alice@example.com",
        }
        poller._adapter.get_channel_info.return_value = MagicMock(title="general")
        await poller._handle_event(_event({
            "type": "message", "user": "U123", "text": "hi",
            "channel": "C999", "channel_type": "channel", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert bmsg.sender_name == "Alice"
        assert bmsg.sender_id == "U123"   # unchanged — raw id
        assert bmsg.chat_title == "general"

    @pytest.mark.asyncio
    async def test_resolution_is_cached(self, mock_broker):
        """Repeat messages from the same user/channel hit the cache, not the API."""
        poller = _make_poller(mock_broker)
        poller._adapter.get_user_info.return_value = {"display_name": "Bob"}
        poller._adapter.get_channel_info.return_value = MagicMock(title="ops")
        for _ in range(3):
            await poller._handle_event(_event({
                "type": "message", "user": "U7", "text": "x",
                "channel": "C7", "channel_type": "channel", "ts": "1.1",
            }))
            await asyncio.sleep(0)
        assert poller._adapter.get_user_info.call_count == 1
        assert poller._adapter.get_channel_info.call_count == 1

    @pytest.mark.asyncio
    async def test_resolution_falls_back_to_raw_id_on_error(self, mock_broker):
        """A transient API error must not break delivery: keep the raw id/empty title."""
        poller = _make_poller(mock_broker)
        poller._adapter.get_user_info.side_effect = Exception("network boom")
        poller._adapter.get_channel_info.side_effect = Exception("network boom")
        await poller._handle_event(_event({
            "type": "message", "user": "U123", "text": "hi",
            "channel": "C999", "channel_type": "channel", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        mock_broker.handle_inbound.assert_called_once()
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert bmsg.sender_name == "U123"   # fell back to raw id
        assert bmsg.sender_id == "U123"
        assert bmsg.chat_title == ""

    @pytest.mark.asyncio
    async def test_missing_users_scope_disables_user_resolution(self, mock_broker):
        """A missing_scope failure disables further users.info calls (no hammering)."""
        poller = _make_poller(mock_broker)
        poller._adapter.get_user_info.side_effect = Exception("missing_scope")
        for uid in ("U1", "U2", "U3"):
            await poller._handle_event(_event({
                "type": "message", "user": uid, "text": "x",
                "channel": "C1", "channel_type": "channel", "ts": "1.1",
            }))
            await asyncio.sleep(0)
        assert poller._adapter.get_user_info.call_count == 1  # disabled after first
        assert poller._users_read_ok is False

    @pytest.mark.asyncio
    async def test_channel_missing_scope_cached_per_channel(self, mock_broker):
        """A channel scope failure is cached per-channel ("" title), not retried."""
        poller = _make_poller(mock_broker)
        poller._adapter.get_user_info.return_value = {"display_name": "Z"}
        poller._adapter.get_channel_info.side_effect = Exception("missing_scope")
        for _ in range(3):
            await poller._handle_event(_event({
                "type": "message", "user": "U9", "text": "x",
                "channel": "C5", "channel_type": "channel", "ts": "1.1",
            }))
            await asyncio.sleep(0)
        assert poller._adapter.get_channel_info.call_count == 1  # cached "" for C5
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert bmsg.chat_title == ""

    @pytest.mark.asyncio
    async def test_bot_message_skips_user_resolution(self, mock_broker):
        """Bot-authored messages (bot_id, no user) never call users.info."""
        poller = _make_poller(mock_broker, bot_id="B_SELF")
        poller._adapter.get_channel_info.return_value = MagicMock(title="general")
        await poller._handle_event(_event({
            "type": "message", "subtype": "bot_message", "bot_id": "B_PEER",
            "username": "peerbot", "text": "hi", "channel": "C1",
            "channel_type": "channel", "ts": "1.1",
        }))
        await asyncio.sleep(0)
        poller._adapter.get_user_info.assert_not_called()
        bmsg = mock_broker.handle_inbound.call_args[0][0]
        assert bmsg.sender_name == "peerbot"  # username kept for bots
        assert bmsg.chat_title == "general"

    @pytest.mark.asyncio
    async def test_non_events_api_request_acked_but_not_routed(self, monkeypatch, mock_broker):
        """A non-events_api envelope that carries an envelope_id (slash command /
        interactive) must still be ACKed — so Slack stops retrying it — but must
        NOT be routed to the broker (only message events are wired up)."""

        class FakeSocketModeClient:
            def __init__(self, app_token=None, web_client=None):
                self.socket_mode_request_listeners = []
                self.responses = []

            async def connect(self):
                pass

            async def send_socket_mode_response(self, resp):
                self.responses.append(resp)

        class FakeSocketModeResponse:
            def __init__(self, envelope_id=None):
                self.envelope_id = envelope_id

        for name in ("slack_sdk", "slack_sdk.socket_mode", "slack_sdk.web"):
            monkeypatch.setitem(sys.modules, name, types.ModuleType(name))
        aiohttp_mod = types.ModuleType("slack_sdk.socket_mode.aiohttp")
        aiohttp_mod.SocketModeClient = FakeSocketModeClient
        response_mod = types.ModuleType("slack_sdk.socket_mode.response")
        response_mod.SocketModeResponse = FakeSocketModeResponse
        web_mod = types.ModuleType("slack_sdk.web.async_client")
        web_mod.AsyncWebClient = lambda token=None: MagicMock()
        monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode.aiohttp", aiohttp_mod)
        monkeypatch.setitem(sys.modules, "slack_sdk.socket_mode.response", response_mod)
        monkeypatch.setitem(sys.modules, "slack_sdk.web.async_client", web_mod)

        poller = _make_poller(mock_broker)
        await poller.start()
        listener = poller._client.socket_mode_request_listeners[0]

        # slash_commands envelope: has an envelope_id → ACK, but don't route.
        req = MagicMock()
        req.type = "slash_commands"
        req.envelope_id = "env-slash"
        await listener(poller._client, req)
        await asyncio.sleep(0)
        assert len(poller._client.responses) == 1
        assert poller._client.responses[0].envelope_id == "env-slash"  # ACKed
        mock_broker.handle_inbound.assert_not_called()  # not routed
