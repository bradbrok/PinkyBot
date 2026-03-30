"""Tests for pinky_outreach Slack adapter."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from pinky_outreach.slack import SlackAdapter, SlackError
from pinky_outreach.types import Platform


class TestSlackAdapter:
    """Tests for SlackAdapter using mocked HTTP."""

    def _make_adapter(self):
        adapter = SlackAdapter("xoxb-fake-slack-token")
        return adapter

    def test_init(self):
        adapter = self._make_adapter()
        assert adapter._token == "xoxb-fake-slack-token"
        assert adapter._bot_info is None
        adapter.close()

    def test_headers(self):
        adapter = self._make_adapter()
        assert "Bearer xoxb-fake-slack-token" in adapter._client.headers["Authorization"]
        adapter.close()

    def test_send_message_success(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "channel": "C12345",
            "message": {
                "text": "Hello Slack!",
                "ts": "1711584000.000100",
                "type": "message",
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        msg = adapter.send_message("C12345", "Hello Slack!")
        assert msg.chat_id == "C12345"
        assert msg.content == "Hello Slack!"
        assert msg.message_id == "1711584000.000100"
        assert msg.is_outbound is True
        assert msg.platform == Platform.slack
        adapter.close()

    def test_send_message_in_thread(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "channel": "C12345",
            "message": {
                "text": "Thread reply",
                "ts": "1711584060.000200",
                "thread_ts": "1711584000.000100",
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        msg = adapter.send_message("C12345", "Thread reply", thread_ts="1711584000.000100")
        assert msg.message_id == "1711584060.000200"
        assert msg.metadata.get("thread_ts") == "1711584000.000100"

        # Verify thread_ts was passed
        call_kwargs = adapter._client.post.call_args
        payload = call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))
        assert payload["thread_ts"] == "1711584000.000100"
        adapter.close()

    def test_send_message_error(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": False,
            "error": "channel_not_found",
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        with pytest.raises(SlackError) as exc:
            adapter.send_message("C99999", "Hello!")
        assert "channel_not_found" in str(exc.value)
        adapter.close()

    def test_get_history_empty(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "messages": [],
            "has_more": False,
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        messages = adapter.get_history("C12345")
        assert messages == []
        adapter.close()

    def test_get_history_with_messages(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "messages": [
                {
                    "type": "message",
                    "user": "U12345",
                    "text": "Hello from user",
                    "ts": "1711584000.000100",
                },
                {
                    "type": "message",
                    "bot_id": "B12345",
                    "text": "Bot response",
                    "ts": "1711584060.000200",
                    "subtype": "bot_message",
                },
            ],
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        messages = adapter.get_history("C12345")
        assert len(messages) == 2
        assert messages[0].sender == "U12345"
        assert messages[0].is_outbound is False
        assert messages[0].content == "Hello from user"
        assert messages[1].sender == "B12345"
        assert messages[1].is_outbound is True
        adapter.close()

    def test_get_history_with_oldest(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True, "messages": []}
        adapter._client.post = MagicMock(return_value=mock_response)

        adapter.get_history("C12345", oldest="1711584000.000100", limit=10)

        call_kwargs = adapter._client.post.call_args
        payload = call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))
        assert payload["oldest"] == "1711584000.000100"
        assert payload["limit"] == 10
        adapter.close()

    def test_get_bot_info(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "user_id": "U111",
            "bot_id": "B111",
            "team_id": "T111",
            "team": "TestTeam",
            "user": "pinkybot",
            "url": "https://testteam.slack.com/",
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        info = adapter.get_bot_info()
        assert info["user"] == "pinkybot"
        assert info["team"] == "TestTeam"
        assert adapter._bot_info is not None
        adapter.close()

    def test_get_bot_info_caches(self):
        adapter = self._make_adapter()
        adapter._bot_info = {"user": "cached", "user_id": "U111"}

        info = adapter.get_bot_info()
        assert info["user"] == "cached"
        adapter.close()

    def test_get_channel_info(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "channel": {
                "id": "C12345",
                "name": "general",
                "is_channel": True,
                "is_private": False,
                "is_im": False,
                "is_mpim": False,
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        chat = adapter.get_channel_info("C12345")
        assert chat.chat_id == "C12345"
        assert chat.title == "general"
        assert chat.chat_type == "channel"
        assert chat.platform == Platform.slack
        adapter.close()

    def test_get_channel_info_dm(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "channel": {
                "id": "D12345",
                "is_im": True,
                "is_private": False,
                "is_mpim": False,
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        chat = adapter.get_channel_info("D12345")
        assert chat.chat_type == "dm"
        adapter.close()

    def test_get_channel_info_private(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "channel": {
                "id": "G12345",
                "name": "secret-stuff",
                "is_im": False,
                "is_mpim": False,
                "is_private": True,
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        chat = adapter.get_channel_info("G12345")
        assert chat.chat_type == "private"
        adapter.close()

    def test_add_reaction(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}
        adapter._client.post = MagicMock(return_value=mock_response)

        result = adapter.add_reaction("C12345", "1711584000.000100", "thumbsup")
        assert result is True

        call_kwargs = adapter._client.post.call_args
        payload = call_kwargs.kwargs.get("json", call_kwargs[1].get("json", {}))
        assert payload["name"] == "thumbsup"
        assert payload["timestamp"] == "1711584000.000100"
        adapter.close()

    def test_remove_reaction(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": True}
        adapter._client.post = MagicMock(return_value=mock_response)

        result = adapter.remove_reaction("C12345", "1711584000.000100", "thumbsup")
        assert result is True
        adapter.close()

    def test_add_reaction_error(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "error": "already_reacted"}
        adapter._client.post = MagicMock(return_value=mock_response)

        with pytest.raises(SlackError) as exc:
            adapter.add_reaction("C12345", "1711584000.000100", "thumbsup")
        assert "already_reacted" in str(exc.value)
        adapter.close()


class TestOutreachServerSlack:
    """Test the MCP server with Slack support."""

    def test_server_with_slack_token(self):
        from pinky_outreach.server import create_server
        server = create_server(slack_token="xoxb-fake")
        tool_names = {t.name for t in server._tool_manager.list_tools()}
        assert "send_message" in tool_names

    def test_server_with_all_tokens(self):
        from pinky_outreach.server import create_server
        server = create_server(
            telegram_token="fake-tg",
            discord_token="fake-dc",
            slack_token="xoxb-fake",
        )
        tool_names = {t.name for t in server._tool_manager.list_tools()}
        expected = {
            "send_message", "check_messages", "send_photo",
            "send_document", "get_chat_info", "add_reaction", "download_file", "bot_info",
        }
        assert expected == tool_names
