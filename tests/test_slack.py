"""Tests for pinky_outreach Slack adapter."""

from __future__ import annotations

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
        assert adapter._dm_conversation_ids == {}
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

    def test_send_message_keeps_user_id_path_unchanged(self):
        """Slack already accepts U-prefixed IDs on chat.postMessage."""
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "channel": "D12345",
            "message": {"text": "Hello", "ts": "1711584000.000100"},
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        msg = adapter.send_message("U12345", "Hello")

        assert msg.chat_id == "U12345"
        assert adapter._client.post.call_args[0][0] == "/chat.postMessage"
        assert adapter._client.post.call_args.kwargs["json"]["channel"] == "U12345"
        assert adapter._dm_conversation_ids == {}
        adapter.close()

    def test_get_user_info_success(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "user": {
                "id": "U123", "name": "alice", "real_name": "Alice Adams",
                "is_bot": False,
                "profile": {
                    "display_name": "Alice", "real_name": "Alice Adams",
                    "email": "alice@example.com",
                },
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)

        info = adapter.get_user_info("U123")
        assert info["user_id"] == "U123"
        assert info["display_name"] == "Alice"
        assert info["real_name"] == "Alice Adams"
        assert info["email"] == "alice@example.com"
        assert info["is_bot"] is False
        # called users.info with the user id, FORM-encoded: Slack read methods
        # ignore a JSON body, so _request_form must send data= (not json=) with
        # an x-www-form-urlencoded Content-Type. Regression guard for the bug
        # where JSON-posted lookups returned user_not_found / invalid_arguments.
        assert adapter._client.post.call_args[0][0] == "/users.info"
        assert "json" not in adapter._client.post.call_args.kwargs
        payload = adapter._client.post.call_args.kwargs["data"]
        assert payload["user"] == "U123"
        assert (
            adapter._client.post.call_args.kwargs["headers"]["Content-Type"]
            == "application/x-www-form-urlencoded"
        )
        adapter.close()

    def test_get_user_info_display_name_falls_back_to_real_then_handle(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "user": {"id": "U9", "name": "bob", "real_name": "Bob B", "profile": {}},
        }
        adapter._client.post = MagicMock(return_value=mock_response)
        info = adapter.get_user_info("U9")
        assert info["display_name"] == "Bob B"  # no profile.display_name -> real_name
        adapter.close()

    def test_get_user_info_missing_scope_raises(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "error": "missing_scope"}
        adapter._client.post = MagicMock(return_value=mock_response)
        with pytest.raises(SlackError) as exc:
            adapter.get_user_info("U123")
        assert "missing_scope" in str(exc.value)
        adapter.close()

    def test_lookup_user_by_email_success(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "user": {
                "id": "U777", "name": "carol", "real_name": "Carol C",
                "profile": {"display_name": "Carol", "email": "carol@example.com"},
            },
        }
        adapter._client.post = MagicMock(return_value=mock_response)
        info = adapter.lookup_user_by_email("carol@example.com")
        assert info["user_id"] == "U777"
        assert info["display_name"] == "Carol"
        assert info["email"] == "carol@example.com"
        assert adapter._client.post.call_args[0][0] == "/users.lookupByEmail"
        assert "json" not in adapter._client.post.call_args.kwargs
        payload = adapter._client.post.call_args.kwargs["data"]
        assert payload["email"] == "carol@example.com"
        adapter.close()

    def test_lookup_user_by_email_not_found_raises(self):
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {"ok": False, "error": "users_not_found"}
        adapter._client.post = MagicMock(return_value=mock_response)
        with pytest.raises(SlackError):
            adapter.lookup_user_by_email("nobody@example.com")
        adapter.close()

    def test_get_channel_info_uses_form_encoding(self):
        """conversations.info is a read method — must be form-encoded, not JSON.

        Regression guard for the bug where a JSON body made Slack return
        invalid_arguments (the channel arg never arrived), so chat_title fell
        back to the raw C... id on every inbound message.
        """
        adapter = self._make_adapter()
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ok": True,
            "channel": {"id": "C123", "name": "orders-and-equipment"},
        }
        adapter._client.post = MagicMock(return_value=mock_response)
        chat = adapter.get_channel_info("C123")
        assert chat.title == "orders-and-equipment"
        assert adapter._client.post.call_args[0][0] == "/conversations.info"
        assert "json" not in adapter._client.post.call_args.kwargs
        assert adapter._client.post.call_args.kwargs["data"]["channel"] == "C123"
        assert (
            adapter._client.post.call_args.kwargs["headers"]["Content-Type"]
            == "application/x-www-form-urlencoded"
        )
        adapter.close()

    def test_upload_file_uses_form_encoding(self, tmp_path, monkeypatch):
        """files.getUploadURLExternal + completeUploadExternal are form/query
        methods: a JSON body makes Slack ignore the args and return
        invalid_arguments, so uploads failed even on a minimal file. Regression
        guard — both must be form-encoded (data=, not json=) with the uploadV2
        argument shape (filename+length, then a JSON-encoded `files` array)."""
        import json as _json

        adapter = self._make_adapter()
        f = tmp_path / "note.txt"
        f.write_text("hello")  # 5 bytes

        url_resp = MagicMock()
        url_resp.json.return_value = {
            "ok": True,
            "upload_url": "https://files.slack.com/upload/xyz",
            "file_id": "F123",
        }
        complete_resp = MagicMock()
        complete_resp.json.return_value = {"ok": True, "files": [{"id": "F123"}]}
        adapter._client.post = MagicMock(side_effect=[url_resp, complete_resp])

        # Step 2 streams bytes to the returned upload_url via module-level httpx.
        upload_resp = MagicMock(status_code=200)
        monkeypatch.setattr(
            "pinky_outreach.slack.httpx.post", MagicMock(return_value=upload_resp)
        )

        msg = adapter.upload_file("C123", str(f), title="Note", initial_comment="here")

        calls = adapter._client.post.call_args_list
        # Step 1: getUploadURLExternal — form-encoded, filename + length.
        assert calls[0][0][0] == "/files.getUploadURLExternal"
        assert "json" not in calls[0].kwargs
        assert (
            calls[0].kwargs["headers"]["Content-Type"]
            == "application/x-www-form-urlencoded"
        )
        assert calls[0].kwargs["data"]["filename"] == "note.txt"
        assert calls[0].kwargs["data"]["length"] == "5"
        # Step 3: completeUploadExternal — form-encoded; files is a JSON string.
        assert calls[1][0][0] == "/files.completeUploadExternal"
        assert "json" not in calls[1].kwargs
        assert (
            calls[1].kwargs["headers"]["Content-Type"]
            == "application/x-www-form-urlencoded"
        )
        assert calls[1].kwargs["data"]["channel_id"] == "C123"
        files_arg = calls[1].kwargs["data"]["files"]
        assert isinstance(files_arg, str)
        assert _json.loads(files_arg) == [{"id": "F123", "title": "Note"}]

        assert msg.message_id == "F123"
        adapter.close()

    def test_upload_file_threads_when_thread_ts(self, tmp_path, monkeypatch):
        """A file uploaded with thread_ts must thread under that parent (a Slack
        reply), not spawn a new root. Regression guard: the file-upload path
        silently dropped reply_to, so send_photo/document/video replies posted a
        new root instead of threading (Pi-fleet RMA screenshot flow, 2026-07)."""
        adapter = self._make_adapter()
        f = tmp_path / "shot.png"
        f.write_bytes(b"\x89PNG\r\n")

        url_resp = MagicMock()
        url_resp.json.return_value = {
            "ok": True,
            "upload_url": "https://files.slack.com/upload/xyz",
            "file_id": "F777",
        }
        complete_resp = MagicMock()
        complete_resp.json.return_value = {"ok": True, "files": [{"id": "F777"}]}
        adapter._client.post = MagicMock(side_effect=[url_resp, complete_resp])
        monkeypatch.setattr(
            "pinky_outreach.slack.httpx.post",
            MagicMock(return_value=MagicMock(status_code=200)),
        )

        msg = adapter.upload_file(
            "C123", str(f), initial_comment="approved",
            thread_ts="1784133332.936979",
        )

        # completeUploadExternal must carry thread_ts so Slack threads the file.
        complete_call = adapter._client.post.call_args_list[1]
        assert complete_call[0][0] == "/files.completeUploadExternal"
        assert complete_call.kwargs["data"]["thread_ts"] == "1784133332.936979"
        # and the returned Message records the thread it landed in.
        assert msg.metadata.get("thread_ts") == "1784133332.936979"
        adapter.close()

    def test_upload_file_no_thread_ts_omits_it(self, tmp_path, monkeypatch):
        """Standalone upload (no thread_ts) must NOT send a thread_ts arg —
        _request_form drops None, so the file posts as a new root as before."""
        adapter = self._make_adapter()
        f = tmp_path / "note.txt"
        f.write_text("hi")

        url_resp = MagicMock()
        url_resp.json.return_value = {
            "ok": True,
            "upload_url": "https://files.slack.com/upload/xyz",
            "file_id": "F888",
        }
        complete_resp = MagicMock()
        complete_resp.json.return_value = {"ok": True, "files": [{"id": "F888"}]}
        adapter._client.post = MagicMock(side_effect=[url_resp, complete_resp])
        monkeypatch.setattr(
            "pinky_outreach.slack.httpx.post",
            MagicMock(return_value=MagicMock(status_code=200)),
        )

        msg = adapter.upload_file("C123", str(f), initial_comment="hi")

        complete_call = adapter._client.post.call_args_list[1]
        assert "thread_ts" not in complete_call.kwargs["data"]
        assert "thread_ts" not in msg.metadata
        adapter.close()

    def test_upload_file_resolves_user_id_before_upload(self, tmp_path, monkeypatch):
        """U-prefixed destinations are opened as D-prefixed conversations first."""
        adapter = self._make_adapter()
        f = tmp_path / "rma.png"
        f.write_bytes(b"\x89PNG\r\n")

        open_resp = MagicMock()
        open_resp.json.return_value = {"ok": True, "channel": {"id": "D456"}}
        url_resp = MagicMock()
        url_resp.json.return_value = {
            "ok": True,
            "upload_url": "https://files.slack.com/upload/xyz",
            "file_id": "F123",
        }
        complete_resp = MagicMock()
        complete_resp.json.return_value = {"ok": True, "files": [{"id": "F123"}]}
        adapter._client.post = MagicMock(
            side_effect=[open_resp, url_resp, complete_resp]
        )
        monkeypatch.setattr(
            "pinky_outreach.slack.httpx.post",
            MagicMock(return_value=MagicMock(status_code=200)),
        )

        msg = adapter.upload_file("U123", str(f), initial_comment="RMA")

        calls = adapter._client.post.call_args_list
        assert [call.args[0] for call in calls] == [
            "/conversations.open",
            "/files.getUploadURLExternal",
            "/files.completeUploadExternal",
        ]
        assert calls[0].kwargs["data"] == {"users": "U123"}
        assert (
            calls[0].kwargs["headers"]["Content-Type"]
            == "application/x-www-form-urlencoded"
        )
        assert calls[2].kwargs["data"]["channel_id"] == "D456"
        assert adapter._dm_conversation_ids == {"U123": "D456"}
        assert msg.chat_id == "U123"
        adapter.close()

    def test_upload_file_threads_after_user_id_resolution(self, tmp_path, monkeypatch):
        """The U→D resolution also covers photo/document replies in a DM."""
        adapter = self._make_adapter()
        f = tmp_path / "reply.png"
        f.write_bytes(b"\x89PNG\r\n")

        open_resp = MagicMock()
        open_resp.json.return_value = {"ok": True, "channel": {"id": "D456"}}
        url_resp = MagicMock()
        url_resp.json.return_value = {
            "ok": True,
            "upload_url": "https://files.slack.com/upload/xyz",
            "file_id": "F456",
        }
        complete_resp = MagicMock()
        complete_resp.json.return_value = {"ok": True, "files": [{"id": "F456"}]}
        adapter._client.post = MagicMock(
            side_effect=[open_resp, url_resp, complete_resp]
        )
        monkeypatch.setattr(
            "pinky_outreach.slack.httpx.post",
            MagicMock(return_value=MagicMock(status_code=200)),
        )

        msg = adapter.upload_file(
            "U123",
            str(f),
            thread_ts="1784133332.936979",
        )

        complete_call = adapter._client.post.call_args_list[2]
        assert complete_call.kwargs["data"]["channel_id"] == "D456"
        assert complete_call.kwargs["data"]["thread_ts"] == "1784133332.936979"
        assert msg.metadata["thread_ts"] == "1784133332.936979"
        adapter.close()

    def test_user_id_resolution_is_cached_per_adapter(self):
        adapter = self._make_adapter()
        open_resp = MagicMock()
        open_resp.json.return_value = {"ok": True, "channel": {"id": "D456"}}
        adapter._client.post = MagicMock(return_value=open_resp)

        assert adapter._ensure_conversation_id("U123") == "D456"
        assert adapter._ensure_conversation_id("U123") == "D456"

        adapter._client.post.assert_called_once()
        adapter.close()

    def test_user_id_resolution_missing_scope_fails_before_upload(
        self,
        tmp_path,
        monkeypatch,
    ):
        adapter = self._make_adapter()
        f = tmp_path / "rma.png"
        f.write_bytes(b"\x89PNG\r\n")
        missing_scope = MagicMock()
        missing_scope.json.return_value = {
            "ok": False,
            "error": "missing_scope",
            "needed": "im:write",
        }
        adapter._client.post = MagicMock(return_value=missing_scope)
        upload_post = MagicMock()
        monkeypatch.setattr("pinky_outreach.slack.httpx.post", upload_post)

        with pytest.raises(SlackError, match="im:write"):
            adapter.upload_file("U123", str(f))

        adapter._client.post.assert_called_once()
        assert adapter._client.post.call_args.args[0] == "/conversations.open"
        upload_post.assert_not_called()
        assert adapter._dm_conversation_ids == {}
        adapter.close()

    def test_user_id_resolution_rejects_missing_dm_id(self):
        adapter = self._make_adapter()
        invalid_response = MagicMock()
        invalid_response.json.return_value = {"ok": True, "channel": {}}
        adapter._client.post = MagicMock(return_value=invalid_response)

        with pytest.raises(SlackError, match="no valid DM conversation ID"):
            adapter._ensure_conversation_id("U123")

        assert adapter._dm_conversation_ids == {}
        adapter.close()

    @pytest.mark.parametrize("channel", ["D123", "G123"])
    def test_upload_file_preserves_existing_conversation_ids(
        self,
        channel,
        tmp_path,
        monkeypatch,
    ):
        """D/G uploads bypass conversations.open; C is covered above."""
        adapter = self._make_adapter()
        f = tmp_path / "note.txt"
        f.write_text("hello")
        url_resp = MagicMock()
        url_resp.json.return_value = {
            "ok": True,
            "upload_url": "https://files.slack.com/upload/xyz",
            "file_id": "F123",
        }
        complete_resp = MagicMock()
        complete_resp.json.return_value = {"ok": True, "files": [{"id": "F123"}]}
        adapter._client.post = MagicMock(side_effect=[url_resp, complete_resp])
        monkeypatch.setattr(
            "pinky_outreach.slack.httpx.post",
            MagicMock(return_value=MagicMock(status_code=200)),
        )

        adapter.upload_file(channel, str(f))

        calls = adapter._client.post.call_args_list
        assert [call.args[0] for call in calls] == [
            "/files.getUploadURLExternal",
            "/files.completeUploadExternal",
        ]
        assert calls[1].kwargs["data"]["channel_id"] == channel
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
            "send_document", "send_video", "get_chat_info", "add_reaction", "download_file", "bot_info",
            "list_platforms",
        }
        assert expected == tool_names


class TestUploadFileTransportErrors:
    def test_presigned_upload_transport_error_raises_slack_error(self, tmp_path):
        """Step-2 httpx failures (timeouts, connect errors) must surface as
        SlackError so callers' except SlackError handlers catch them."""
        import httpx

        from pinky_outreach import slack as slack_mod

        file_path = tmp_path / "report.pdf"
        file_path.write_bytes(b"PDF")

        adapter = SlackAdapter("xoxb-fake-slack-token")
        step1 = MagicMock()
        step1.json.return_value = {
            "ok": True,
            "upload_url": "https://files.slack.com/upload/abc",
            "file_id": "F123",
        }
        adapter._client.post = MagicMock(return_value=step1)

        def _post(url, content=None, timeout=None):
            assert timeout == 30.0  # not httpx's 5s module-level default
            raise httpx.ReadTimeout("stalled")

        original_post = slack_mod.httpx.post
        slack_mod.httpx.post = _post
        try:
            with pytest.raises(SlackError, match="File upload failed"):
                adapter.upload_file("C12345", str(file_path))
        finally:
            slack_mod.httpx.post = original_post
            adapter.close()
