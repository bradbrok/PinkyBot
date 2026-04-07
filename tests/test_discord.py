"""Tests for pinky_outreach Discord adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from pinky_outreach.discord import DiscordAdapter, DiscordError
from pinky_outreach.types import Platform


class TestDiscordAdapter:
    """Tests for DiscordAdapter using mocked HTTP."""

    def _make_adapter(self):
        adapter = DiscordAdapter("fake-discord-token")
        return adapter

    def test_init(self):
        adapter = self._make_adapter()
        assert adapter._token == "fake-discord-token"
        assert adapter._bot_user is None
        adapter.close()

    def test_headers(self):
        adapter = self._make_adapter()
        assert "Bot fake-discord-token" in adapter._client.headers["Authorization"]
        adapter.close()

    def test_send_message_success(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "1234567890",
            "channel_id": "99999",
            "content": "Hello Discord!",
            "timestamp": "2026-03-27T12:00:00+00:00",
            "author": {"id": "111", "username": "PinkyBot", "bot": True},
        }
        adapter._client.request = MagicMock(return_value=mock_response)

        msg = adapter.send_message("99999", "Hello Discord!")
        assert msg.message_id == "1234567890"
        assert msg.chat_id == "99999"
        assert msg.content == "Hello Discord!"
        assert msg.is_outbound is True
        assert msg.platform == Platform.discord
        adapter.close()

    def test_send_message_with_reply(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "1234567891",
            "channel_id": "99999",
            "content": "Reply!",
            "timestamp": "2026-03-27T12:01:00+00:00",
        }
        adapter._client.request = MagicMock(return_value=mock_response)

        msg = adapter.send_message("99999", "Reply!", reply_to="1234567890")
        assert msg.message_id == "1234567891"

        # Verify the payload included message_reference
        call_kwargs = adapter._client.request.call_args
        payload = call_kwargs.kwargs.get("json", {})
        assert payload["message_reference"]["message_id"] == "1234567890"
        adapter.close()

    def test_send_message_error(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 403
        mock_response.json.return_value = {"message": "Missing Access", "code": 50001}
        adapter._client.request = MagicMock(return_value=mock_response)

        with pytest.raises(DiscordError) as exc:
            adapter.send_message("99999", "Hello!")
        assert "Missing Access" in str(exc.value)
        assert exc.value.status_code == 403
        adapter.close()

    def test_get_messages_empty(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        adapter._client.request = MagicMock(return_value=mock_response)

        messages = adapter.get_messages("99999")
        assert messages == []
        adapter.close()

    def test_get_messages_with_results(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {
                "id": "100",
                "channel_id": "99999",
                "content": "Hey there",
                "timestamp": "2026-03-27T12:00:00+00:00",
                "author": {"id": "222", "username": "brad", "discriminator": "0", "bot": False},
            },
            {
                "id": "101",
                "channel_id": "99999",
                "content": "Bot reply",
                "timestamp": "2026-03-27T12:01:00+00:00",
                "author": {"id": "111", "username": "PinkyBot", "discriminator": "0", "bot": True},
                "message_reference": {"message_id": "100"},
            },
        ]
        adapter._client.request = MagicMock(return_value=mock_response)

        messages = adapter.get_messages("99999")
        assert len(messages) == 2
        assert messages[0].sender == "brad"
        assert messages[0].is_outbound is False
        assert messages[0].content == "Hey there"
        assert messages[1].sender == "PinkyBot"
        assert messages[1].is_outbound is True
        assert messages[1].reply_to == "100"
        adapter.close()

    def test_get_messages_with_after(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = []
        adapter._client.request = MagicMock(return_value=mock_response)

        adapter.get_messages("99999", after="100", limit=10)

        call_kwargs = adapter._client.request.call_args
        params = call_kwargs.kwargs.get("params", {})
        assert params["after"] == "100"
        assert params["limit"] == 10
        adapter.close()

    def test_get_me(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "111",
            "username": "PinkyBot",
            "discriminator": "0",
            "bot": True,
        }
        adapter._client.request = MagicMock(return_value=mock_response)

        info = adapter.get_me()
        assert info["username"] == "PinkyBot"
        assert info["bot"] is True
        # Should cache
        assert adapter._bot_user is not None
        adapter.close()

    def test_get_me_caches(self):
        adapter = self._make_adapter()
        adapter._bot_user = {"id": "111", "username": "cached"}

        info = adapter.get_me()
        assert info["username"] == "cached"
        # Should not make a request
        adapter.close()

    def test_get_channel(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "99999",
            "name": "general",
            "type": 0,
            "guild_id": "88888",
        }
        adapter._client.request = MagicMock(return_value=mock_response)

        chat = adapter.get_channel("99999")
        assert chat.chat_id == "99999"
        assert chat.title == "general"
        assert chat.chat_type == "text"
        assert chat.platform == Platform.discord
        adapter.close()

    def test_get_channel_dm(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = {
            "id": "77777",
            "name": None,
            "type": 1,
        }
        adapter._client.request = MagicMock(return_value=mock_response)

        chat = adapter.get_channel("77777")
        assert chat.chat_type == "dm"
        adapter.close()

    def test_add_reaction(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.json.return_value = {}
        adapter._client.request = MagicMock(return_value=mock_response)

        result = adapter.add_reaction("99999", "100", "\U0001f44d")
        assert result is True
        adapter.close()

    def test_remove_reaction(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_response.json.return_value = {}
        adapter._client.request = MagicMock(return_value=mock_response)

        result = adapter.remove_reaction("99999", "100", "\U0001f44d")
        assert result is True
        adapter.close()

    def test_get_guild_channels(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"id": "1", "name": "general", "type": 0},
            {"id": "2", "name": "voice", "type": 2},
            {"id": "3", "name": "announcements", "type": 5},
        ]
        adapter._client.request = MagicMock(return_value=mock_response)

        channels = adapter.get_guild_channels("88888")
        assert len(channels) == 3
        assert channels[0].title == "general"
        assert channels[1].title == "voice"
        adapter.close()


class TestOutreachServerDiscord:
    """Test the MCP server with Discord support."""

    def test_server_with_discord_token(self):
        from pinky_outreach.server import create_server
        server = create_server(discord_token="fake-discord-token")
        tool_names = {t.name for t in server._tool_manager.list_tools()}
        assert "send_message" in tool_names
        assert "check_messages" in tool_names

    def test_server_with_both_tokens(self):
        from pinky_outreach.server import create_server
        server = create_server(
            telegram_token="fake-tg-token",
            discord_token="fake-dc-token",
        )
        tool_names = {t.name for t in server._tool_manager.list_tools()}
        expected = {
            "send_message", "check_messages", "send_photo",
            "send_document", "get_chat_info", "add_reaction", "download_file", "bot_info",
            "list_platforms",
        }
        assert expected == tool_names
