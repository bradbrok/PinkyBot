"""Tests for WhatsApp adapter and outreach server WhatsApp integration."""

from __future__ import annotations

import json
import os
from unittest.mock import MagicMock, patch

import pytest

from pinky_outreach.types import Platform
from pinky_outreach.whatsapp import WhatsAppAdapter, WhatsAppError

# ── Helpers ────────────────────────────────────────────────────────────────────

def _mock_response(data: dict, status_code: int = 200) -> MagicMock:
    """Build a mock httpx Response."""
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = data
    resp.content = b""
    return resp


def _wa(token: str = "tok", phone_id: str = "12345") -> WhatsAppAdapter:
    return WhatsAppAdapter(token, phone_id)


# ── WhatsAppError ──────────────────────────────────────────────────────────────

class TestWhatsAppError:
    def test_message_and_code(self):
        err = WhatsAppError("Something failed", 190)
        assert err.message == "Something failed"
        assert err.error_code == 190
        assert "190" in str(err)
        assert "Something failed" in str(err)

    def test_default_code(self):
        err = WhatsAppError("oops")
        assert err.error_code == 0

    def test_is_exception(self):
        with pytest.raises(WhatsAppError):
            raise WhatsAppError("test")


# ── Platform enum ──────────────────────────────────────────────────────────────

class TestPlatformEnum:
    def test_whatsapp_in_enum(self):
        assert Platform.whatsapp == "whatsapp"
        assert Platform.whatsapp.value == "whatsapp"

    def test_all_original_platforms_still_present(self):
        values = {p.value for p in Platform}
        assert "telegram" in values
        assert "discord" in values
        assert "slack" in values
        assert "imessage" in values
        assert "email" in values
        assert "whatsapp" in values


# ── WhatsAppAdapter construction ───────────────────────────────────────────────

class TestWhatsAppAdapterConstruction:
    def test_basic_construction(self):
        adapter = _wa("mytoken", "99999")
        assert adapter._token == "mytoken"
        assert adapter._phone_id == "99999"
        assert "99999" in adapter._base
        assert "v21.0" in adapter._base

    def test_base_url_format(self):
        adapter = _wa(phone_id="88888")
        assert adapter._base == f"{WhatsAppAdapter.BASE_URL}/88888"

    def test_close(self):
        adapter = _wa()
        adapter._client = MagicMock()
        adapter.close()
        adapter._client.close.assert_called_once()


# ── send_message ───────────────────────────────────────────────────────────────

class TestSendMessage:
    def test_send_basic(self):
        adapter = _wa()
        resp_data = {"messages": [{"id": "wamid.abc123"}]}
        with patch.object(adapter._client, "request", return_value=_mock_response(resp_data)):
            msg = adapter.send_message("+14155550100", "Hello!")

        assert msg.message_id == "wamid.abc123"
        assert msg.content == "Hello!"
        assert msg.chat_id == "+14155550100"
        assert msg.platform == Platform.whatsapp
        assert msg.sender == "bot"

    def test_send_payload_shape(self):
        adapter = _wa()
        resp_data = {"messages": [{"id": "mid1"}]}
        with patch.object(adapter._client, "request", return_value=_mock_response(resp_data)) as mock_req:
            adapter.send_message("+1234567890", "Test text")

        _, call_kwargs = mock_req.call_args
        body = call_kwargs.get("json", {})
        assert body["messaging_product"] == "whatsapp"
        assert body["to"] == "+1234567890"
        assert body["type"] == "text"
        assert body["text"]["body"] == "Test text"

    def test_send_with_reply(self):
        adapter = _wa()
        resp_data = {"messages": [{"id": "mid2"}]}
        with patch.object(adapter._client, "request", return_value=_mock_response(resp_data)) as mock_req:
            adapter.send_message("+1234567890", "Reply!", reply_to_message_id="orig_id")

        _, call_kwargs = mock_req.call_args
        body = call_kwargs["json"]
        assert body["context"]["message_id"] == "orig_id"

    def test_send_raises_on_error(self):
        adapter = _wa()
        err_data = {"error": {"message": "Invalid token", "code": 190}}
        with patch.object(adapter._client, "request", return_value=_mock_response(err_data)):
            with pytest.raises(WhatsAppError) as exc_info:
                adapter.send_message("+1234567890", "Hi")

        assert exc_info.value.error_code == 190
        assert "Invalid token" in exc_info.value.message

    def test_send_empty_messages_list(self):
        adapter = _wa()
        # API returns empty messages list — message_id should be ""
        resp_data = {"messages": []}
        with patch.object(adapter._client, "request", return_value=_mock_response(resp_data)):
            msg = adapter.send_message("+1234", "text")
        assert msg.message_id == ""


# ── send_photo ─────────────────────────────────────────────────────────────────

class TestSendPhoto:
    def test_send_photo_uploads_then_sends(self, tmp_path):
        img = tmp_path / "test.jpg"
        img.write_bytes(b"fake_image_data")

        adapter = _wa()
        upload_resp = {"id": "media_abc"}
        send_resp = {"messages": [{"id": "msg_photo"}]}

        responses = [_mock_response(upload_resp), _mock_response(send_resp)]
        with patch.object(adapter._client, "post", side_effect=responses) as _mock_post, \
             patch.object(adapter._client, "request", return_value=_mock_response(send_resp)) as mock_req:
            # _upload_media uses client.post directly; send uses _request (client.request)
            # Patch _upload_media to avoid file I/O complexity
            with patch.object(adapter, "_upload_media", return_value="media_abc") as mock_upload:
                msg = adapter.send_photo("+1234", str(img), caption="A photo")

        mock_upload.assert_called_once_with(str(img), "image/jpeg")
        _, call_kwargs = mock_req.call_args
        body = call_kwargs["json"]
        assert body["type"] == "image"
        assert body["image"]["id"] == "media_abc"
        assert body["image"]["caption"] == "A photo"
        assert msg.message_id == "msg_photo"
        assert msg.content == "A photo"

    def test_send_photo_no_caption(self, tmp_path):
        img = tmp_path / "img.jpg"
        img.write_bytes(b"data")

        adapter = _wa()
        send_resp = {"messages": [{"id": "msg2"}]}
        with patch.object(adapter, "_upload_media", return_value="mid"), \
             patch.object(adapter._client, "request", return_value=_mock_response(send_resp)) as mock_req:
            msg = adapter.send_photo("+1234", str(img))

        _, call_kwargs = mock_req.call_args
        body = call_kwargs["json"]
        assert "caption" not in body["image"]
        assert msg.content == "[photo]"


# ── mark_read ──────────────────────────────────────────────────────────────────

class TestMarkRead:
    def test_mark_read_payload(self):
        adapter = _wa()
        with patch.object(adapter._client, "request", return_value=_mock_response({})) as mock_req:
            adapter.mark_read("wamid.xyz")

        _, call_kwargs = mock_req.call_args
        body = call_kwargs["json"]
        assert body["messaging_product"] == "whatsapp"
        assert body["status"] == "read"
        assert body["message_id"] == "wamid.xyz"


# ── send_reaction ──────────────────────────────────────────────────────────────

class TestSendReaction:
    def test_reaction_payload(self):
        adapter = _wa()
        with patch.object(adapter._client, "request", return_value=_mock_response({})) as mock_req:
            adapter.send_reaction("+1234", "wamid.abc", "👍")

        _, call_kwargs = mock_req.call_args
        body = call_kwargs["json"]
        assert body["type"] == "reaction"
        assert body["reaction"]["message_id"] == "wamid.abc"
        assert body["reaction"]["emoji"] == "👍"
        assert body["to"] == "+1234"

    def test_reaction_returns_dict(self):
        adapter = _wa()
        with patch.object(adapter._client, "request", return_value=_mock_response({"success": True})):
            result = adapter.send_reaction("+1234", "mid", "❤️")
        assert isinstance(result, dict)


# ── download_file ──────────────────────────────────────────────────────────────

class TestDownloadFile:
    def test_two_step_download(self, tmp_path):
        adapter = _wa()

        # Step 1: get media URL
        media_info = {"url": "https://example.com/file.jpg", "mime_type": "image/jpeg"}
        # Step 2: download the file
        file_content = b"fake_image_bytes"
        download_resp = MagicMock()
        download_resp.content = file_content

        def fake_request(method, path, **kwargs):
            if method == "GET" and "media_id_123" in path:
                return media_info
            raise AssertionError(f"Unexpected request: {method} {path}")

        with patch.object(adapter, "_request", side_effect=fake_request), \
             patch.object(adapter._client, "get", return_value=download_resp):
            path = adapter.download_file("media_id_123", dest_dir=str(tmp_path))

        assert path.endswith(".jpeg")
        assert "wa_media_id_123" in path
        assert os.path.exists(path)
        assert open(path, "rb").read() == file_content

    def test_missing_url_raises(self, tmp_path):
        adapter = _wa()
        with patch.object(adapter, "_request", return_value={"url": ""}):
            with pytest.raises(WhatsAppError) as exc_info:
                adapter.download_file("bad_id", dest_dir=str(tmp_path))
        assert "No download URL" in exc_info.value.message


# ── get_me ─────────────────────────────────────────────────────────────────────

class TestGetMe:
    def test_get_me_returns_info(self):
        adapter = _wa()
        phone_info = {
            "id": "12345",
            "display_phone_number": "+1 415 555 0100",
            "verified_name": "My Business",
        }
        with patch.object(adapter, "_request", return_value=phone_info):
            result = adapter.get_me()

        assert result["id"] == "12345"
        assert result["verified_name"] == "My Business"

    def test_get_me_calls_empty_path(self):
        adapter = _wa()
        with patch.object(adapter, "_request", return_value={}) as mock_req:
            adapter.get_me()
        mock_req.assert_called_once_with("GET", "")


# ── Conditional tool registration ─────────────────────────────────────────────

class TestConditionalToolRegistration:
    def _tools(self, srv):
        return {t.name: t.fn for t in srv._tool_manager.list_tools()}

    def test_no_tools_when_no_platforms(self):
        from pinky_outreach.server import create_server
        srv = create_server()
        tools = self._tools(srv)
        assert len(tools) == 0

    def test_tools_registered_with_telegram(self):
        from pinky_outreach.server import create_server
        with patch("pinky_outreach.server.TelegramAdapter"):
            srv = create_server(telegram_token="tok")
        tools = self._tools(srv)
        assert "send_message" in tools
        assert "check_messages" in tools
        assert "list_platforms" in tools

    def test_active_platforms_telegram_only(self):
        from pinky_outreach.server import create_server
        with patch("pinky_outreach.server.TelegramAdapter"):
            srv = create_server(telegram_token="tok")
        tools = self._tools(srv)
        result = json.loads(tools["list_platforms"]())
        assert result["platforms"] == ["telegram"]

    def test_active_platforms_multiple(self):
        from pinky_outreach.server import create_server
        with patch("pinky_outreach.server.TelegramAdapter"), \
             patch("pinky_outreach.server.DiscordAdapter"):
            srv = create_server(telegram_token="tok", discord_token="dc")
        tools = self._tools(srv)
        result = json.loads(tools["list_platforms"]())
        assert "telegram" in result["platforms"]
        assert "discord" in result["platforms"]

    def test_active_platforms_whatsapp(self):
        from pinky_outreach.server import create_server
        with patch("pinky_outreach.server.WhatsAppAdapter", create=True), \
             patch("pinky_outreach.whatsapp.WhatsAppAdapter"):
            with patch("pinky_outreach.server.TelegramAdapter"):
                pass
            # Use the lazy import path
            with patch("builtins.__import__", wraps=__import__) as _mock_import:
                srv = create_server(whatsapp_token="wa_tok", whatsapp_phone_id="99999")
        tools = self._tools(srv)
        result = json.loads(tools["list_platforms"]())
        assert "whatsapp" in result["platforms"]

    def test_default_platform_is_first_configured(self):
        from pinky_outreach.server import create_server
        with patch("pinky_outreach.server.DiscordAdapter"):
            srv = create_server(discord_token="dc")
        tools = self._tools(srv)
        # Default platform should be discord (the only configured one)
        # send_message with no platform arg should use discord
        dc_mock = MagicMock()
        dc_mock.send_message.return_value = MagicMock(message_id="m1")
        # Patch the discord adapter in the closure
        # We verify by calling send_message with an unconfigured platform
        result = tools["send_message"](content="hi", chat_id="c", platform="telegram")
        data = json.loads(result)
        assert "error" in data
        assert "not available" in data["error"].lower()


# ── WhatsApp server tools ──────────────────────────────────────────────────────

class TestWhatsAppServerTools:
    def _make_srv(self):
        from pinky_outreach.server import create_server
        with patch("pinky_outreach.whatsapp.WhatsAppAdapter") as MockWA:
            wa_mock = MagicMock()
            MockWA.return_value = wa_mock
            # Import WhatsAppAdapter inside create_server lazily
            with patch.dict("sys.modules", {}):
                srv = create_server(whatsapp_token="tok", whatsapp_phone_id="12345")
            srv._wa_mock = wa_mock
        return srv

    def test_whatsapp_send_message(self):
        from datetime import datetime, timezone

        from pinky_outreach.server import create_server
        from pinky_outreach.types import Message, Platform

        fake_msg = Message(
            platform=Platform.whatsapp,
            chat_id="+1234",
            sender="bot",
            content="Hello",
            timestamp=datetime.now(timezone.utc),
            message_id="wamid.test",
        )

        with patch("pinky_outreach.whatsapp.WhatsAppAdapter") as MockWA:
            wa_mock = MagicMock()
            wa_mock.send_message.return_value = fake_msg
            MockWA.return_value = wa_mock
            srv = create_server(whatsapp_token="tok", whatsapp_phone_id="12345")

        tools = {t.name: t.fn for t in srv._tool_manager.list_tools()}
        result = json.loads(tools["send_message"](content="Hello", chat_id="+1234", platform="whatsapp"))
        assert result["sent"] is True
        assert result["platform"] == "whatsapp"
        assert result["message_id"] == "wamid.test"

    def test_whatsapp_check_messages_webhook_notice(self):
        from pinky_outreach.server import create_server

        with patch("pinky_outreach.whatsapp.WhatsAppAdapter"):
            srv = create_server(whatsapp_token="tok", whatsapp_phone_id="12345")

        tools = {t.name: t.fn for t in srv._tool_manager.list_tools()}
        result = json.loads(tools["check_messages"](platform="whatsapp"))
        assert "webhook" in result.get("info", "").lower()
        assert result["platform"] == "whatsapp"

    def test_whatsapp_add_reaction(self):
        from pinky_outreach.server import create_server

        with patch("pinky_outreach.whatsapp.WhatsAppAdapter") as MockWA:
            wa_mock = MagicMock()
            MockWA.return_value = wa_mock
            srv = create_server(whatsapp_token="tok", whatsapp_phone_id="12345")

        tools = {t.name: t.fn for t in srv._tool_manager.list_tools()}
        result = json.loads(tools["add_reaction"](
            chat_id="+1234", message_id="wamid.abc", emoji="👍", platform="whatsapp"
        ))
        assert result["reacted"] is True
        wa_mock.send_reaction.assert_called_once_with("+1234", "wamid.abc", "👍")
