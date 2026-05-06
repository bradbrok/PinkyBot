"""Tests for pinky_outreach Discord adapter."""

from __future__ import annotations

from unittest.mock import MagicMock

import httpx
import pytest

from pinky_outreach.discord import DiscordAdapter, DiscordError, DiscordRateLimited
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

    def test_open_dm_channel_caches(self):
        """open_dm_channel calls /users/@me/channels once per recipient and caches."""
        adapter = self._make_adapter()
        resp = MagicMock()
        resp.status_code = 200
        resp.json.return_value = {"id": "dm-channel-abc", "type": 1}
        adapter._client.request = MagicMock(return_value=resp)

        ch1 = adapter.open_dm_channel("user-754")
        ch2 = adapter.open_dm_channel("user-754")

        assert ch1 == "dm-channel-abc"
        assert ch2 == "dm-channel-abc"
        # Only one HTTP call thanks to the cache
        assert adapter._client.request.call_count == 1
        # Verify it hit the right endpoint with the right body
        call = adapter._client.request.call_args
        assert call.args[0] == "POST"
        assert call.args[1] == "/users/@me/channels"
        assert call.kwargs["json"] == {"recipient_id": "user-754"}
        adapter.close()

    def test_send_message_falls_back_to_dm_on_404_unknown_channel(self):
        """If channel_id is actually a user_id, send_message opens a DM and retries."""
        adapter = self._make_adapter()

        send_404 = MagicMock()
        send_404.status_code = 404
        send_404.json.return_value = {"message": "Unknown Channel", "code": 10003}

        open_dm_ok = MagicMock()
        open_dm_ok.status_code = 200
        open_dm_ok.json.return_value = {"id": "dm-channel-xyz", "type": 1}

        send_ok = MagicMock()
        send_ok.status_code = 200
        send_ok.json.return_value = {
            "id": "msg-9999",
            "channel_id": "dm-channel-xyz",
            "content": "hi owner",
            "timestamp": "2026-05-05T19:00:00+00:00",
        }

        adapter._client.request = MagicMock(side_effect=[send_404, open_dm_ok, send_ok])

        msg = adapter.send_message("754027672526389310", "hi owner")
        assert msg.message_id == "msg-9999"
        # chat_id should reflect the resolved DM channel, not the user_id
        assert msg.chat_id == "dm-channel-xyz"

        # Three calls: failed POST → open DM → retry POST
        assert adapter._client.request.call_count == 3
        calls = adapter._client.request.call_args_list
        assert calls[0].args == ("POST", "/channels/754027672526389310/messages")
        assert calls[1].args == ("POST", "/users/@me/channels")
        assert calls[1].kwargs["json"] == {"recipient_id": "754027672526389310"}
        assert calls[2].args == ("POST", "/channels/dm-channel-xyz/messages")

        # Cache populated for next time
        assert adapter._dm_channel_cache["754027672526389310"] == "dm-channel-xyz"
        adapter.close()

    def test_send_message_404_other_message_does_not_fallback(self):
        """404 with a different message (e.g. Unknown Message) should NOT trigger DM open."""
        adapter = self._make_adapter()

        resp = MagicMock()
        resp.status_code = 404
        resp.json.return_value = {"message": "Unknown Message", "code": 10008}
        adapter._client.request = MagicMock(return_value=resp)

        with pytest.raises(DiscordError) as exc:
            adapter.send_message("99999", "hi", reply_to="missing-msg-id")
        assert exc.value.status_code == 404
        assert "Unknown Message" in str(exc.value)
        # No DM resolution attempted
        assert adapter._client.request.call_count == 1
        adapter.close()

    def test_send_message_dm_fallback_uses_cached_channel(self):
        """Second send to the same user_id reuses the cached DM channel — no /users/@me/channels call."""
        adapter = self._make_adapter()
        # Pre-populate the cache as if a previous send had resolved the DM
        adapter._dm_channel_cache["user-754"] = "dm-channel-abc"

        send_404 = MagicMock()
        send_404.status_code = 404
        send_404.json.return_value = {"message": "Unknown Channel", "code": 10003}

        send_ok = MagicMock()
        send_ok.status_code = 200
        send_ok.json.return_value = {
            "id": "msg-2",
            "channel_id": "dm-channel-abc",
            "content": "second msg",
            "timestamp": "2026-05-05T19:01:00+00:00",
        }

        adapter._client.request = MagicMock(side_effect=[send_404, send_ok])

        msg = adapter.send_message("user-754", "second msg")
        assert msg.message_id == "msg-2"
        # Only the failed initial POST + the retry — no /users/@me/channels lookup
        assert adapter._client.request.call_count == 2
        calls = adapter._client.request.call_args_list
        assert "/users/@me/channels" not in (calls[0].args[1], calls[1].args[1])
        adapter.close()

    def test_send_file_success_uses_httpx_multipart_header(self, tmp_path):
        """Regression: passing Content-Type=None makes httpx raise before sending."""
        adapter = self._make_adapter()
        file_path = tmp_path / "note.txt"
        file_path.write_text("hello discord")
        seen_requests = []

        def handler(request: httpx.Request) -> httpx.Response:
            seen_requests.append(request)
            body = request.read()
            assert request.method == "POST"
            assert request.url.path.endswith("/channels/99999/messages")
            assert request.headers["content-type"].startswith("multipart/form-data; boundary=")
            assert b'name="files[0]"' in body
            assert b'filename="note.txt"' in body
            assert b"hello discord" in body
            return httpx.Response(
                200,
                json={
                    "id": "file-msg-1",
                    "channel_id": "99999",
                    "content": "upload caption",
                    "timestamp": "2026-03-27T12:02:00+00:00",
                },
            )

        adapter._client.close()
        adapter._client = httpx.Client(
            base_url=adapter.BASE_URL,
            headers={"Authorization": "Bot fake-discord-token"},
            transport=httpx.MockTransport(handler),
        )

        msg = adapter.send_file("99999", str(file_path), content="upload caption")
        assert msg.message_id == "file-msg-1"
        assert msg.content == "upload caption"
        assert len(seen_requests) == 1
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

    def test_user_agent_header_sent(self):
        """Discord 403s requests with the default httpx UA on some endpoints."""
        adapter = self._make_adapter()
        ua = adapter._client.headers["User-Agent"]
        # Must follow Discord's "DiscordBot (..., version)" convention
        assert ua.startswith("DiscordBot (")
        adapter.close()

    def test_429_retry_then_success(self, monkeypatch):
        """A single 429 should sleep `retry_after` then retry transparently."""
        adapter = self._make_adapter()
        slept: list[float] = []
        monkeypatch.setattr(
            "pinky_outreach.discord.time.sleep",
            lambda s: slept.append(s),
        )

        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.json.return_value = {"retry_after": 0.5, "message": "rate limited"}

        success = MagicMock()
        success.status_code = 200
        success.json.return_value = {
            "id": "1234567890",
            "channel_id": "99999",
            "content": "ok",
            "timestamp": "2026-03-27T12:00:00+00:00",
        }
        adapter._client.request = MagicMock(side_effect=[rate_limited, success])

        msg = adapter.send_message("99999", "ok")
        assert msg.message_id == "1234567890"
        assert slept and slept[0] == pytest.approx(0.5)
        adapter.close()

    def test_429_retry_exhausted_raises_rate_limited(self, monkeypatch):
        """After `max_429_retries` attempts a 429 must surface as DiscordRateLimited."""
        adapter = DiscordAdapter("fake-discord-token", max_429_retries=1)
        monkeypatch.setattr("pinky_outreach.discord.time.sleep", lambda s: None)

        rate_limited = MagicMock()
        rate_limited.status_code = 429
        rate_limited.json.return_value = {"retry_after": 2.5}

        adapter._client.request = MagicMock(return_value=rate_limited)
        with pytest.raises(DiscordRateLimited) as exc:
            adapter.send_message("99999", "ok")
        assert exc.value.retry_after == pytest.approx(2.5)
        adapter.close()

    def test_get_my_guilds(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.json.return_value = [
            {"id": "g1", "name": "Server One"},
            {"id": "g2", "name": "Server Two"},
        ]
        adapter._client.request = MagicMock(return_value=mock_response)

        guilds = adapter.get_my_guilds()
        assert len(guilds) == 2
        assert guilds[0]["id"] == "g1"
        adapter.close()

    def test_discover_text_channels_filters_voice_and_categories(self):
        """Channel discovery should include text(0) + announcement(5), drop the rest."""
        adapter = self._make_adapter()

        guilds_resp = MagicMock()
        guilds_resp.status_code = 200
        guilds_resp.json.return_value = [{"id": "g1", "name": "Server One"}]

        channels_resp = MagicMock()
        channels_resp.status_code = 200
        channels_resp.json.return_value = [
            {"id": "ch-text", "name": "general", "type": 0},
            {"id": "ch-voice", "name": "Lounge", "type": 2},
            {"id": "ch-cat", "name": "Category", "type": 4},
            {"id": "ch-announce", "name": "news", "type": 5},
        ]
        adapter._client.request = MagicMock(side_effect=[guilds_resp, channels_resp])

        ids = adapter.discover_text_channels()
        assert ids == ["ch-text", "ch-announce"]
        adapter.close()

    def test_discover_text_channels_skips_unreadable_guilds(self):
        """If listing one guild's channels fails, others should still be returned."""
        adapter = self._make_adapter()

        guilds_resp = MagicMock()
        guilds_resp.status_code = 200
        guilds_resp.json.return_value = [
            {"id": "g1", "name": "Server One"},
            {"id": "g2", "name": "Server Two"},
        ]

        forbidden_resp = MagicMock()
        forbidden_resp.status_code = 403
        forbidden_resp.json.return_value = {"message": "Missing Access"}

        ok_channels_resp = MagicMock()
        ok_channels_resp.status_code = 200
        ok_channels_resp.json.return_value = [
            {"id": "ch-ok", "name": "general", "type": 0},
        ]
        adapter._client.request = MagicMock(
            side_effect=[guilds_resp, forbidden_resp, ok_channels_resp],
        )

        ids = adapter.discover_text_channels()
        assert ids == ["ch-ok"]
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
