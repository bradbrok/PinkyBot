"""Tests for pinky_outreach — types, telegram adapter, and MCP server."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from pinky_outreach.types import Chat, Message, Platform

# ── Types ────────────────────────────────────────────────────


class TestMessage:
    def test_create_message(self):
        msg = Message(
            platform=Platform.telegram,
            chat_id="12345",
            sender="Brad",
            content="Hello",
            timestamp=datetime(2026, 3, 27, tzinfo=timezone.utc),
        )
        assert msg.platform == Platform.telegram
        assert msg.chat_id == "12345"
        assert msg.sender == "Brad"
        assert msg.content == "Hello"
        assert msg.is_outbound is False

    def test_message_to_dict(self):
        msg = Message(
            platform=Platform.telegram,
            chat_id="12345",
            sender="bot",
            content="Hi there",
            timestamp=datetime(2026, 3, 27, 12, 0, 0, tzinfo=timezone.utc),
            message_id="99",
            is_outbound=True,
        )
        d = msg.to_dict()
        assert d["platform"] == "telegram"
        assert d["chat_id"] == "12345"
        assert d["sender"] == "bot"
        assert d["content"] == "Hi there"
        assert d["message_id"] == "99"
        assert d["is_outbound"] is True
        assert "2026-03-27" in d["timestamp"]

    def test_message_defaults(self):
        msg = Message(
            platform=Platform.telegram,
            chat_id="1",
            sender="x",
            content="y",
            timestamp=datetime.now(timezone.utc),
        )
        assert msg.message_id == ""
        assert msg.reply_to == ""
        assert msg.metadata == {}


class TestChat:
    def test_create_chat(self):
        chat = Chat(
            platform=Platform.telegram,
            chat_id="12345",
            title="Test Chat",
            chat_type="group",
        )
        assert chat.chat_id == "12345"
        assert chat.title == "Test Chat"

    def test_chat_to_dict(self):
        chat = Chat(
            platform=Platform.telegram,
            chat_id="12345",
            title="Test",
            chat_type="private",
            username="testuser",
        )
        d = chat.to_dict()
        assert d["platform"] == "telegram"
        assert d["chat_id"] == "12345"
        assert d["username"] == "testuser"


class TestPlatform:
    def test_platform_values(self):
        assert Platform.telegram.value == "telegram"
        assert Platform.discord.value == "discord"
        assert Platform.slack.value == "slack"
        assert Platform.imessage.value == "imessage"
        assert Platform.email.value == "email"


# ── Telegram Adapter ────────────────────────────────────────


class TestTelegramAdapter:
    """Tests for TelegramAdapter using mocked HTTP."""

    def _make_adapter(self):
        from pinky_outreach.telegram import TelegramAdapter
        adapter = TelegramAdapter("fake-token-123")
        return adapter

    def test_init(self):
        adapter = self._make_adapter()
        assert adapter._token == "fake-token-123"
        assert adapter._last_update_id == 0
        adapter.close()

    def test_send_message_success(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": {
                "message_id": 42,
                "chat": {"id": 12345, "type": "private"},
                "date": 1711584000,
                "text": "Hello!",
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        msg = adapter.send_message("12345", "Hello!")
        assert msg.message_id == "42"
        assert msg.chat_id == "12345"
        assert msg.is_outbound is True
        assert msg.platform == Platform.telegram
        adapter.close()

    def test_send_message_error(self):
        from pinky_outreach.telegram import TelegramError

        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": False,
            "error_code": 400,
            "description": "Bad Request: chat not found",
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        with pytest.raises(TelegramError) as exc:
            adapter.send_message("99999", "Hello!")
        assert "chat not found" in str(exc.value)
        adapter.close()

    @pytest.mark.parametrize(
        "method_name",
        ["send_photo", "send_document", "send_animation"],
    )
    def test_media_sends_omit_none_reply_to_message_id(self, method_name, tmp_path):
        adapter = self._make_adapter()
        media_path = tmp_path / "media.bin"
        media_path.write_bytes(b"media")
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": {
                "message_id": 42,
                "chat": {"id": 12345, "type": "private"},
                "date": 1711584000,
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        getattr(adapter, method_name)("12345", str(media_path), caption="hi")

        data = adapter._client.post.call_args.kwargs["data"]
        assert "reply_to_message_id" not in data
        assert data["chat_id"] == "12345"
        adapter.close()

    def test_get_updates_empty(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": []}
        adapter._client.post = MagicMock(return_value=mock_response)

        messages = adapter.get_updates()
        assert messages == []
        adapter.close()

    def test_get_updates_with_messages(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": [
                {
                    "update_id": 100,
                    "message": {
                        "message_id": 1,
                        "chat": {"id": 12345, "type": "private"},
                        "from": {
                            "id": 67890,
                            "first_name": "Brad",
                            "last_name": "B",
                            "username": "ohmybingbong",
                        },
                        "date": 1711584000,
                        "text": "Hey Oleg",
                    },
                },
                {
                    "update_id": 101,
                    "message": {
                        "message_id": 2,
                        "chat": {"id": 12345, "type": "private"},
                        "from": {
                            "id": 67890,
                            "first_name": "Brad",
                        },
                        "date": 1711584060,
                        "text": "What's up?",
                    },
                },
            ],
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        messages = adapter.get_updates()
        assert len(messages) == 2
        assert messages[0].sender == "Brad B"
        assert messages[0].content == "Hey Oleg"
        assert messages[0].metadata["username"] == "ohmybingbong"
        assert messages[1].sender == "Brad"
        assert messages[1].content == "What's up?"
        assert adapter._last_update_id == 101
        adapter.close()

    def test_get_updates_tracks_offset(self):
        adapter = self._make_adapter()
        adapter._last_update_id = 99

        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": []}
        adapter._client.post = MagicMock(return_value=mock_response)

        adapter.get_updates()

        # Should have passed offset=100
        call_kwargs = adapter._client.post.call_args
        body = call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))
        assert body.get("offset") == 100
        adapter.close()

    def test_get_me(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": {
                "id": 111,
                "is_bot": True,
                "first_name": "PinkyBot",
                "username": "pinky_ai_bot",
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        info = adapter.get_me()
        assert info["username"] == "pinky_ai_bot"
        adapter.close()

    def test_get_chat(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "result": {
                "id": 12345,
                "type": "private",
                "first_name": "Brad",
                "username": "ohmybingbong",
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        chat = adapter.get_chat("12345")
        assert chat.chat_id == "12345"
        assert chat.title == "Brad"
        assert chat.chat_type == "private"
        adapter.close()

    def test_set_reaction(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": True}
        adapter._client.post = MagicMock(return_value=mock_response)

        result = adapter.set_reaction("12345", 42, "👍")
        assert result is True
        adapter.close()

    def test_set_reaction_valid_emoji_passes_through(self):
        """Valid TG reaction emoji is sent as-is."""
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": True}
        adapter._client.post = MagicMock(return_value=mock_response)

        adapter.set_reaction("12345", 42, "🔥")
        call_args = adapter._client.post.call_args
        payload = call_args[1].get("json", call_args[0][1] if len(call_args[0]) > 1 else {})
        # The reaction should contain the fire emoji
        assert any(r["emoji"] == "🔥" for r in payload.get("reaction", []))
        adapter.close()

    def test_set_reaction_invalid_emoji_falls_back(self):
        """Invalid emoji (not in TG's allowed set) falls back to 👍."""
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": True}
        adapter._client.post = MagicMock(return_value=mock_response)

        # 🤙 (call me hand) is NOT in Telegram's valid reaction set
        adapter.set_reaction("12345", 42, "🤙")
        call_args = adapter._client.post.call_args
        payload = call_args[1].get("json", call_args[0][1] if len(call_args[0]) > 1 else {})
        assert any(r["emoji"] == "👍" for r in payload.get("reaction", []))
        adapter.close()

    def test_set_reaction_shortcode_resolves(self):
        """Shortcode like 'fire' resolves to a valid emoji."""
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "result": True}
        adapter._client.post = MagicMock(return_value=mock_response)

        adapter.set_reaction("12345", 42, "fire")
        call_args = adapter._client.post.call_args
        payload = call_args[1].get("json", call_args[0][1] if len(call_args[0]) > 1 else {})
        assert any(r["emoji"] == "🔥" for r in payload.get("reaction", []))
        adapter.close()

    def test_valid_reactions_set_is_populated(self):
        """VALID_REACTIONS contains the expected count of emojis."""
        from pinky_outreach.telegram import TelegramAdapter
        assert len(TelegramAdapter.VALID_REACTIONS) >= 70

    def test_all_shortcuts_map_to_valid_reactions(self):
        """Every EMOJI_SHORTCUTS value is in VALID_REACTIONS."""
        from pinky_outreach.telegram import TelegramAdapter
        for shortcode, emoji in TelegramAdapter.EMOJI_SHORTCUTS.items():
            stripped = emoji.replace("\ufe0f", "")
            assert emoji in TelegramAdapter.VALID_REACTIONS or \
                stripped in TelegramAdapter.VALID_REACTIONS, \
                f"Shortcut '{shortcode}' maps to {repr(emoji)} which is not a valid TG reaction"


# ── MCP Server ───────────────────────────────────────────────


class TestOutreachServer:
    """Test the MCP server tool functions."""

    def _make_server_tools(self, telegram_token: str = ""):
        """Create server and extract tool functions."""
        from pinky_outreach.server import create_server
        server = create_server(telegram_token=telegram_token)
        return server

    def test_server_creates_without_token(self):
        server = self._make_server_tools()
        assert server is not None

    def test_server_creates_with_token(self):
        server = self._make_server_tools(telegram_token="fake-token")
        assert server is not None

    def test_send_message_no_platforms(self):
        """No tools are registered when no platform is configured."""
        from pinky_outreach.server import create_server
        server = create_server(telegram_token="")
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        # No platforms configured — no tools should be registered
        assert "send_message" not in tools

    def test_unsupported_platform(self):
        """Platform routing should handle unknown platforms."""
        from pinky_outreach.server import create_server
        with patch("pinky_outreach.server.TelegramAdapter"):
            server = create_server(telegram_token="tok")
        tools = {t.name: t for t in server._tool_manager.list_tools()}
        assert "check_messages" in tools
        assert "send_photo" in tools
        assert "send_document" in tools
        assert "get_chat_info" in tools
        assert "add_reaction" in tools
        assert "bot_info" in tools

    def test_all_tools_registered(self):
        """Verify all expected tools are present when a platform is configured."""
        from pinky_outreach.server import create_server
        with patch("pinky_outreach.server.TelegramAdapter"):
            server = create_server(telegram_token="tok")
        tool_names = {t.name for t in server._tool_manager.list_tools()}
        expected = {
            "send_message",
            "check_messages",
            "send_photo",
            "send_document",
            "get_chat_info",
            "add_reaction",
            "download_file",
            "bot_info",
            "list_platforms",
        }
        assert expected == tool_names
