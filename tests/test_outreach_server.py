"""Tests for pinky_outreach MCP server tools.

All adapter calls are mocked — no live Telegram/Discord/Slack tokens needed.
"""
from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest

from pinky_outreach.server import create_server
from pinky_outreach.telegram import TelegramError
from pinky_outreach.discord import DiscordError
from pinky_outreach.slack import SlackError


# ── Fixtures ───────────────────────────────────────────────────────────────────

def _msg(mid: int = 1):
    m = MagicMock()
    m.message_id = mid
    m.to_dict.return_value = {"message_id": mid, "text": "hi"}
    return m


def _chat():
    c = MagicMock()
    c.to_dict.return_value = {"id": "123", "title": "Test Chat"}
    return c


@pytest.fixture
def tg_srv():
    """Server with mocked Telegram adapter."""
    with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
        tg = MagicMock()
        MockTG.return_value = tg
        srv = create_server(telegram_token="fake-tg-token")
        srv._tg = tg  # stash for test access
    return srv


@pytest.fixture
def dc_srv():
    """Server with mocked Discord adapter."""
    with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
        dc = MagicMock()
        MockDC.return_value = dc
        srv = create_server(discord_token="fake-dc-token")
        srv._dc = dc
    return srv


@pytest.fixture
def sl_srv():
    """Server with mocked Slack adapter."""
    with patch("pinky_outreach.server.SlackAdapter") as MockSL:
        sl = MagicMock()
        MockSL.return_value = sl
        srv = create_server(slack_token="fake-sl-token")
        srv._sl = sl
    return srv


@pytest.fixture
def no_srv():
    """Server with no adapters configured."""
    return create_server()


def _tools(srv):
    return {t.name: t.fn for t in srv._tool_manager.list_tools()}


# ── No platforms configured ────────────────────────────────────────────────────

class TestNotConfigured:
    def test_no_tools_when_no_platforms(self, no_srv):
        """When no platforms are configured, no messaging tools are registered."""
        tools = _tools(no_srv)
        assert "send_message" not in tools
        assert "check_messages" not in tools
        assert "send_photo" not in tools
        assert "send_document" not in tools
        assert "get_chat_info" not in tools
        assert "add_reaction" not in tools
        assert "download_file" not in tools
        assert "bot_info" not in tools

    def test_check_messages_unsupported_platform(self):
        result = _tools(create_server(telegram_token="tok"))["check_messages"](platform="carrier_pigeon")
        assert "not available" in result.lower() or "error" in result.lower()

    def test_send_message_unsupported_platform(self):
        with patch("pinky_outreach.server.TelegramAdapter"):
            srv = create_server(telegram_token="tok")
        result = _tools(srv)["send_message"](content="hi", chat_id="123", platform="pigeon")
        assert "not available" in result.lower() or "error" in result.lower()


# ── Telegram send_message ──────────────────────────────────────────────────────

class TestTelegramSendMessage:
    def test_send_success(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.send_message.return_value = _msg(42)
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["send_message"](content="hello", chat_id="123")

        data = json.loads(result)
        assert data["sent"] is True
        assert data["message_id"] == 42
        assert data["platform"] == "telegram"

    def test_send_with_reply_to(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.send_message.return_value = _msg(99)
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["send_message"](
                content="reply", chat_id="123", reply_to="55"
            )

        tg.send_message.assert_called_once()
        call_kwargs = tg.send_message.call_args[1]
        assert call_kwargs["reply_to_message_id"] == 55

    def test_send_error(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.send_message.side_effect = TelegramError("rate limited")
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["send_message"](content="hi", chat_id="123")

        assert "rate limited" in result or "error" in result.lower()

    def test_send_silent(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.send_message.return_value = _msg()
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            _tools(srv)["send_message"](content="quiet", chat_id="c", silent=True)

        call_kwargs = tg.send_message.call_args[1]
        assert call_kwargs["disable_notification"] is True


# ── Discord send_message ───────────────────────────────────────────────────────

class TestDiscordSendMessage:
    def test_send_success(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            dc = MagicMock()
            dc.send_message.return_value = _msg(7)
            MockDC.return_value = dc
            srv = create_server(discord_token="tok")
            result = _tools(srv)["send_message"](content="hi", chat_id="ch1", platform="discord")

        data = json.loads(result)
        assert data["sent"] is True
        assert data["platform"] == "discord"

    def test_send_error(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            dc = MagicMock()
            dc.send_message.side_effect = DiscordError("forbidden")
            MockDC.return_value = dc
            srv = create_server(discord_token="tok")
            result = _tools(srv)["send_message"](content="hi", chat_id="ch1", platform="discord")

        assert "forbidden" in result or "error" in result.lower()


# ── Slack send_message ────────────────────────────────────────────────────────

class TestSlackSendMessage:
    def test_send_success(self):
        with patch("pinky_outreach.server.SlackAdapter") as MockSL:
            sl = MagicMock()
            sl.send_message.return_value = _msg(3)
            MockSL.return_value = sl
            srv = create_server(slack_token="tok")
            result = _tools(srv)["send_message"](content="hi", chat_id="C1", platform="slack")

        data = json.loads(result)
        assert data["sent"] is True
        assert data["platform"] == "slack"

    def test_send_error(self):
        with patch("pinky_outreach.server.SlackAdapter") as MockSL:
            sl = MagicMock()
            sl.send_message.side_effect = SlackError("channel_not_found")
            MockSL.return_value = sl
            srv = create_server(slack_token="tok")
            result = _tools(srv)["send_message"](content="hi", chat_id="C1", platform="slack")

        assert "channel_not_found" in result or "error" in result.lower()


# ── check_messages ────────────────────────────────────────────────────────────

class TestCheckMessages:
    def test_telegram_updates(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.get_updates.return_value = [_msg(1), _msg(2)]
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["check_messages"](platform="telegram")

        data = json.loads(result)
        assert data["count"] == 2
        assert data["platform"] == "telegram"

    def test_discord_messages(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            dc = MagicMock()
            dc.get_messages.return_value = [_msg(5)]
            MockDC.return_value = dc
            srv = create_server(discord_token="tok")
            result = _tools(srv)["check_messages"](chat_id="ch1", platform="discord")

        data = json.loads(result)
        assert data["count"] == 1

    def test_discord_requires_chat_id(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            MockDC.return_value = MagicMock()
            srv = create_server(discord_token="tok")
            result = _tools(srv)["check_messages"](chat_id="", platform="discord")

        assert "required" in result.lower() or "error" in result.lower()

    def test_slack_messages(self):
        with patch("pinky_outreach.server.SlackAdapter") as MockSL:
            sl = MagicMock()
            sl.get_history.return_value = [_msg(9)]
            MockSL.return_value = sl
            srv = create_server(slack_token="tok")
            result = _tools(srv)["check_messages"](chat_id="C1", platform="slack")

        data = json.loads(result)
        assert data["count"] == 1

    def test_slack_requires_chat_id(self):
        with patch("pinky_outreach.server.SlackAdapter") as MockSL:
            MockSL.return_value = MagicMock()
            srv = create_server(slack_token="tok")
            result = _tools(srv)["check_messages"](chat_id="", platform="slack")

        assert "required" in result.lower() or "error" in result.lower()

    def test_unsupported_platform(self):
        with patch("pinky_outreach.server.TelegramAdapter"):
            srv = create_server(telegram_token="tok")
        result = _tools(srv)["check_messages"](platform="carrier_pigeon")
        assert "not available" in result.lower() or "error" in result.lower()


# ── send_photo ────────────────────────────────────────────────────────────────

class TestSendPhoto:
    def test_telegram(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.send_photo.return_value = _msg(11)
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["send_photo"](chat_id="c", file_path="/tmp/img.jpg")

        assert json.loads(result)["sent"] is True

    def test_discord(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            dc = MagicMock()
            dc.send_file.return_value = _msg(12)
            MockDC.return_value = dc
            srv = create_server(discord_token="tok")
            result = _tools(srv)["send_photo"](chat_id="c", file_path="/tmp/img.jpg", platform="discord")

        assert json.loads(result)["sent"] is True

    def test_slack(self):
        with patch("pinky_outreach.server.SlackAdapter") as MockSL:
            sl = MagicMock()
            sl.upload_file.return_value = _msg(13)
            MockSL.return_value = sl
            srv = create_server(slack_token="tok")
            result = _tools(srv)["send_photo"](chat_id="c", file_path="/tmp/img.jpg", platform="slack")

        assert json.loads(result)["sent"] is True

    def test_unsupported(self):
        with patch("pinky_outreach.server.TelegramAdapter"):
            srv = create_server(telegram_token="tok")
        result = _tools(srv)["send_photo"](chat_id="c", file_path="/tmp/x", platform="fax")
        assert "not available" in result.lower() or "error" in result.lower()


# ── send_document ─────────────────────────────────────────────────────────────

class TestSendDocument:
    def test_telegram(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.send_document.return_value = _msg(20)
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["send_document"](chat_id="c", file_path="/tmp/doc.pdf")

        assert json.loads(result)["sent"] is True

    def test_discord(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            dc = MagicMock()
            dc.send_file.return_value = _msg(21)
            MockDC.return_value = dc
            srv = create_server(discord_token="tok")
            result = _tools(srv)["send_document"](chat_id="c", file_path="/tmp/doc.pdf", platform="discord")

        assert json.loads(result)["sent"] is True

    def test_slack(self):
        with patch("pinky_outreach.server.SlackAdapter") as MockSL:
            sl = MagicMock()
            sl.upload_file.return_value = _msg(22)
            MockSL.return_value = sl
            srv = create_server(slack_token="tok")
            result = _tools(srv)["send_document"](chat_id="c", file_path="/tmp/doc.pdf", platform="slack")

        assert json.loads(result)["sent"] is True


# ── get_chat_info ─────────────────────────────────────────────────────────────

class TestGetChatInfo:
    def test_telegram(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.get_chat.return_value = _chat()
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["get_chat_info"](chat_id="123")

        data = json.loads(result)
        assert data["id"] == "123"

    def test_discord(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            dc = MagicMock()
            dc.get_channel.return_value = _chat()
            MockDC.return_value = dc
            srv = create_server(discord_token="tok")
            result = _tools(srv)["get_chat_info"](chat_id="ch1", platform="discord")

        assert json.loads(result)["id"] == "123"

    def test_slack(self):
        with patch("pinky_outreach.server.SlackAdapter") as MockSL:
            sl = MagicMock()
            sl.get_channel_info.return_value = _chat()
            MockSL.return_value = sl
            srv = create_server(slack_token="tok")
            result = _tools(srv)["get_chat_info"](chat_id="C1", platform="slack")

        assert json.loads(result)["id"] == "123"


# ── add_reaction ──────────────────────────────────────────────────────────────

class TestAddReaction:
    def test_telegram(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["add_reaction"](chat_id="c", message_id="5", emoji="👍")

        assert json.loads(result)["reacted"] is True
        tg.set_reaction.assert_called_once_with("c", 5, "👍")

    def test_discord(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            dc = MagicMock()
            MockDC.return_value = dc
            srv = create_server(discord_token="tok")
            result = _tools(srv)["add_reaction"](chat_id="c", message_id="m1", emoji="👍", platform="discord")

        assert json.loads(result)["reacted"] is True

    def test_slack(self):
        with patch("pinky_outreach.server.SlackAdapter") as MockSL:
            sl = MagicMock()
            MockSL.return_value = sl
            srv = create_server(slack_token="tok")
            result = _tools(srv)["add_reaction"](chat_id="c", message_id="ts1", emoji="thumbsup", platform="slack")

        assert json.loads(result)["reacted"] is True


# ── download_file ─────────────────────────────────────────────────────────────

class TestDownloadFile:
    def test_telegram(self, tmp_path):
        fake_path = str(tmp_path / "file.jpg")
        open(fake_path, "w").close()
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.download_file.return_value = fake_path
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["download_file"](file_id="abc", dest_dir=str(tmp_path))

        data = json.loads(result)
        assert data["downloaded"] is True
        assert data["path"] == fake_path

    def test_discord_requires_url(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            MockDC.return_value = MagicMock()
            srv = create_server(discord_token="tok")
            result = _tools(srv)["download_file"](file_id="x", platform="discord", url="")

        assert "required" in result.lower() or "error" in result.lower()

    def test_slack_requires_url(self):
        with patch("pinky_outreach.server.SlackAdapter") as MockSL:
            MockSL.return_value = MagicMock()
            srv = create_server(slack_token="tok")
            result = _tools(srv)["download_file"](file_id="x", platform="slack", url="")

        assert "required" in result.lower() or "error" in result.lower()

    def test_unsupported(self):
        with patch("pinky_outreach.server.TelegramAdapter"):
            srv = create_server(telegram_token="tok")
        result = _tools(srv)["download_file"](file_id="x", platform="fax")
        assert "not available" in result.lower() or "error" in result.lower()


# ── bot_info ──────────────────────────────────────────────────────────────────

class TestBotInfo:
    def test_telegram(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.get_me.return_value = {"id": 1, "username": "mybot", "first_name": "Bot", "can_join_groups": True}
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["bot_info"]()

        data = json.loads(result)
        assert data["username"] == "mybot"
        assert data["platform"] == "telegram"

    def test_discord(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            dc = MagicMock()
            dc.get_me.return_value = {"id": "2", "username": "discordbot", "discriminator": "0001", "bot": True}
            MockDC.return_value = dc
            srv = create_server(discord_token="tok")
            result = _tools(srv)["bot_info"](platform="discord")

        data = json.loads(result)
        assert data["username"] == "discordbot"

    def test_unsupported(self):
        with patch("pinky_outreach.server.TelegramAdapter"):
            srv = create_server(telegram_token="tok")
        result = _tools(srv)["bot_info"](platform="fax")
        assert "not available" in result.lower() or "error" in result.lower()


# ── Error paths for all platforms ─────────────────────────────────────────────

class TestErrorPaths:
    def test_telegram_get_chat_error(self):
        with patch("pinky_outreach.server.TelegramAdapter") as MockTG:
            tg = MagicMock()
            tg.get_chat.side_effect = TelegramError("chat not found")
            MockTG.return_value = tg
            srv = create_server(telegram_token="tok")
            result = _tools(srv)["get_chat_info"](chat_id="bad")

        assert "chat not found" in result or "error" in result.lower()

    def test_discord_reaction_error(self):
        with patch("pinky_outreach.server.DiscordAdapter") as MockDC:
            dc = MagicMock()
            dc.add_reaction.side_effect = DiscordError("unknown emoji")
            MockDC.return_value = dc
            srv = create_server(discord_token="tok")
            result = _tools(srv)["add_reaction"](chat_id="c", message_id="m", emoji="🤔", platform="discord")

        assert "unknown emoji" in result or "error" in result.lower()

    def test_slack_chat_info_error(self):
        with patch("pinky_outreach.server.SlackAdapter") as MockSL:
            sl = MagicMock()
            sl.get_channel_info.side_effect = SlackError("channel_not_found")
            MockSL.return_value = sl
            srv = create_server(slack_token="tok")
            result = _tools(srv)["get_chat_info"](chat_id="bad", platform="slack")

        assert "channel_not_found" in result or "error" in result.lower()
