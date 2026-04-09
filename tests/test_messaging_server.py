"""Tests for pinky_messaging MCP server tools.

All HTTP calls are mocked — no live API needed.
Pattern: patch urllib.request.urlopen, call tools via _tools(srv)["tool_name"](...).
"""
from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import MagicMock, patch

from pinky_messaging.server import create_server

# ── Helpers ────────────────────────────────────────────────────────────────────

def _tools(srv):
    return {t.name: t.fn for t in srv._tool_manager.list_tools()}


def _mock_response(payload: dict, status: int = 200):
    """Return a mock urllib response context manager."""
    resp = MagicMock()
    resp.read.return_value = json.dumps(payload).encode()
    resp.status = status
    resp.__enter__ = lambda s: s
    resp.__exit__ = MagicMock(return_value=False)
    return resp


def _srv():
    return create_server(agent_name="test-agent", api_url="http://localhost:8888")


# ── send ───────────────────────────────────────────────────────────────────────

class TestSend:
    def test_send_success(self):
        payload = {"ok": True, "message_id": "42"}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            result = _tools(srv)["send"](
                chat_id="6770805286",
                platform="telegram",
                text="hello",
            )
        data = json.loads(result)
        assert data["ok"] is True
        mock_open.assert_called_once()

    def test_send_with_parse_mode(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["send"](
                chat_id="123",
                platform="telegram",
                text="*bold*",
                parse_mode="Markdown",
            )
        assert json.loads(result)["ok"] is True

    def test_send_http_error(self):
        import urllib.error
        err = urllib.error.HTTPError(
            url="http://x", code=500, msg="Internal Server Error",
            hdrs=None, fp=BytesIO(b'{"detail": "server error"}'),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            srv = _srv()
            result = _tools(srv)["send"](
                chat_id="123", platform="telegram", text="hi"
            )
        data = json.loads(result)
        assert data["status"] == 500

    def test_send_generic_exception(self):
        with patch("urllib.request.urlopen", side_effect=Exception("connection refused")):
            srv = _srv()
            result = _tools(srv)["send"](
                chat_id="123", platform="telegram", text="hi"
            )
        data = json.loads(result)
        assert "connection refused" in data["error"]


# ── thread ─────────────────────────────────────────────────────────────────────

class TestThread:
    def test_thread_success(self):
        payload = {"ok": True, "message_id": "99"}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["thread"](
                message_id="55",
                text="reply text",
            )
        data = json.loads(result)
        assert data["ok"] is True

    def test_thread_with_parse_mode(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["thread"](
                message_id="55",
                text="<b>html</b>",
                parse_mode="HTML",
            )
        assert json.loads(result)["ok"] is True

    def test_thread_calls_correct_endpoint(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            _tools(srv)["thread"](message_id="55", text="reply")
        req = mock_open.call_args[0][0]
        assert "/broker/thread" in req.full_url

    def test_thread_error(self):
        import urllib.error
        err = urllib.error.HTTPError(
            url="http://x", code=404, msg="Not Found",
            hdrs=None, fp=BytesIO(b"not found"),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            srv = _srv()
            result = _tools(srv)["thread"](message_id="bad", text="hi")
        data = json.loads(result)
        assert data["status"] == 404


# ── react ──────────────────────────────────────────────────────────────────────

class TestReact:
    def test_react_success(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["react"](message_id="77", emoji="👍")
        assert json.loads(result)["ok"] is True

    def test_react_calls_correct_endpoint(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            _tools(srv)["react"](message_id="77", emoji="❤️")
        req = mock_open.call_args[0][0]
        assert "/broker/react" in req.full_url

    def test_react_error(self):
        with patch("urllib.request.urlopen", side_effect=Exception("timeout")):
            srv = _srv()
            result = _tools(srv)["react"](message_id="77", emoji="👍")
        data = json.loads(result)
        assert "timeout" in data["error"]


# ── send_photo ─────────────────────────────────────────────────────────────────

class TestSendPhoto:
    def test_send_photo_success(self):
        payload = {"ok": True, "file_id": "abc123"}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["send_photo"](
                file_path="/tmp/test.jpg",
                caption="A photo",
                chat_id="123",
                platform="telegram",
            )
        assert json.loads(result)["ok"] is True

    def test_send_photo_as_reply(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["send_photo"](
                file_path="/tmp/test.jpg",
                message_id="55",
            )
        assert json.loads(result)["ok"] is True

    def test_send_photo_endpoint(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            _tools(srv)["send_photo"](file_path="/tmp/img.png")
        req = mock_open.call_args[0][0]
        assert "/broker/send-photo" in req.full_url


# ── send_document ──────────────────────────────────────────────────────────────

class TestSendDocument:
    def test_send_document_success(self):
        payload = {"ok": True, "file_id": "doc456"}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["send_document"](
                file_path="/tmp/report.pdf",
                caption="Here's the report",
                chat_id="123",
                platform="telegram",
            )
        assert json.loads(result)["ok"] is True

    def test_send_document_as_reply(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["send_document"](
                file_path="/tmp/data.csv",
                message_id="88",
            )
        assert json.loads(result)["ok"] is True

    def test_send_document_endpoint(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            _tools(srv)["send_document"](file_path="/tmp/doc.pdf")
        req = mock_open.call_args[0][0]
        assert "/broker/send-document" in req.full_url


# ── send_voice ─────────────────────────────────────────────────────────────────

class TestSendVoice:
    def test_send_voice_success(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["send_voice"](
                text="Hello there",
                chat_id="123",
                platform="telegram",
            )
        assert json.loads(result)["ok"] is True

    def test_send_voice_with_provider_and_voice(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["send_voice"](
                text="Good morning",
                chat_id="123",
                provider="openai",
                voice="alloy",
                model="tts-1",
            )
        assert json.loads(result)["ok"] is True

    def test_send_voice_as_reply(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["send_voice"](
                text="Voice reply",
                message_id="55",
            )
        assert json.loads(result)["ok"] is True

    def test_send_voice_endpoint(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            _tools(srv)["send_voice"](text="test")
        req = mock_open.call_args[0][0]
        assert "/broker/send-voice" in req.full_url


# ── send_gif ───────────────────────────────────────────────────────────────────

class TestSendGif:
    def test_send_gif_success(self):
        payload = {"ok": True, "gif_url": "https://media.giphy.com/xxx.gif"}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["send_gif"](
                query="celebration",
                chat_id="123",
                platform="telegram",
            )
        assert json.loads(result)["ok"] is True

    def test_send_gif_with_caption_and_reply(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["send_gif"](
                query="funny cat",
                caption="lol",
                message_id="55",
            )
        assert json.loads(result)["ok"] is True

    def test_send_gif_endpoint(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            _tools(srv)["send_gif"](query="wow")
        req = mock_open.call_args[0][0]
        assert "/broker/send-gif" in req.full_url


# ── broadcast ──────────────────────────────────────────────────────────────────

class TestBroadcast:
    def test_broadcast_success(self):
        payload = {"ok": True, "sent_to": ["telegram:123", "telegram:456"]}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)):
            srv = _srv()
            result = _tools(srv)["broadcast"](text="System update!")
        data = json.loads(result)
        assert data["ok"] is True

    def test_broadcast_endpoint(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            _tools(srv)["broadcast"](text="Announcement")
        req = mock_open.call_args[0][0]
        assert "/broker/broadcast" in req.full_url

    def test_broadcast_error(self):
        import urllib.error
        err = urllib.error.HTTPError(
            url="http://x", code=503, msg="Service Unavailable",
            hdrs=None, fp=BytesIO(b"unavailable"),
        )
        with patch("urllib.request.urlopen", side_effect=err):
            srv = _srv()
            result = _tools(srv)["broadcast"](text="hello")
        data = json.loads(result)
        assert data["status"] == 503


# ── Deprecated aliases ─────────────────────────────────────────────────────────

class TestDeprecatedAliases:
    def test_send_message_delegates_to_send(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            result = _tools(srv)["send_message"](
                content="hi there",
                chat_id="123",
                platform="telegram",
            )
        data = json.loads(result)
        assert data["ok"] is True
        # Verify it made a request to /broker/send
        req = mock_open.call_args[0][0]
        assert "/broker/send" in req.full_url

    def test_add_reaction_delegates_to_react(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            result = _tools(srv)["add_reaction"](
                chat_id="123",
                message_id="55",
                emoji="👍",
                platform="telegram",
            )
        data = json.loads(result)
        assert data["ok"] is True
        req = mock_open.call_args[0][0]
        assert "/broker/react" in req.full_url

    def test_reply_delegates_to_thread(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            result = _tools(srv)["reply"](
                message_id="55",
                text="reply text",
            )
        data = json.loads(result)
        assert data["ok"] is True
        req = mock_open.call_args[0][0]
        assert "/broker/thread" in req.full_url

    def test_send_voice_note_delegates_to_send_voice(self):
        payload = {"ok": True}
        with patch("urllib.request.urlopen", return_value=_mock_response(payload)) as mock_open:
            srv = _srv()
            result = _tools(srv)["send_voice_note"](
                text="note text",
                chat_id="123",
                platform="telegram",
            )
        data = json.loads(result)
        assert data["ok"] is True
        req = mock_open.call_args[0][0]
        assert "/broker/send-voice" in req.full_url


# ── Agent name in requests ─────────────────────────────────────────────────────

class TestAgentNameInjected:
    def test_agent_name_in_request_body(self):
        payload = {"ok": True}
        captured_bodies = []

        def fake_urlopen(req, timeout=None):
            body = json.loads(req.data.decode())
            captured_bodies.append(body)
            return _mock_response(payload)

        with patch("urllib.request.urlopen", side_effect=fake_urlopen):
            srv = create_server(agent_name="barsik", api_url="http://localhost:8888")
            _tools(srv)["send"](chat_id="123", platform="telegram", text="hi")

        assert captured_bodies[0]["agent_name"] == "barsik"


# ── create_server defaults ─────────────────────────────────────────────────────

class TestCreateServer:
    def test_server_has_expected_tools(self):
        srv = create_server()
        tool_names = {t.name for t in srv._tool_manager.list_tools()}
        expected = {
            "send", "thread", "react", "send_photo", "send_document",
            "send_voice", "send_gif", "broadcast",
            "send_message", "add_reaction", "reply", "send_voice_note",
        }
        assert expected.issubset(tool_names)

    def test_create_server_custom_params(self):
        srv = create_server(
            agent_name="test",
            api_url="http://remote:9999",
            host="0.0.0.0",
            port=9000,
        )
        assert srv is not None
