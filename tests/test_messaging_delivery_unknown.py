"""Only send requests classify an outer timeout as uncertain delivery."""

from __future__ import annotations

import inspect
import json
import socket
import urllib.error
from io import BytesIO
from unittest.mock import MagicMock, patch

import pytest

from pinky_messaging.server import create_server

SEND_CASES = [
    ("send", {"chat_id": "12345", "platform": "telegram", "text": "hello"}),
    ("thread", {"message_id": "101", "text": "hello"}),
    ("send_photo", {"file_path": "photo.png"}),
    ("send_document", {"file_path": "document.pdf"}),
    ("send_video", {"file_path": "video.mp4"}),
    ("send_gif", {"query": "test"}),
    ("send_voice", {"text": "hello"}),
    ("broadcast", {"text": "hello"}),
]


def _tools():
    server = create_server(agent_name="sender")
    return {tool.name: tool.fn for tool in server._tool_manager.list_tools()}


@pytest.mark.parametrize("name,kwargs", SEND_CASES, ids=[case[0] for case in SEND_CASES])
@pytest.mark.parametrize(
    "error",
    [
        socket.timeout("timed out"),
        TimeoutError("deadline"),
        urllib.error.URLError(socket.timeout("read timed out")),
    ],
    ids=["socket", "timeout", "url-timeout"],
)
def test_t6_send_outer_timeout_is_unknown(name, kwargs, error):
    tool = _tools()[name]
    with patch("urllib.request.urlopen", side_effect=error) as request:
        result = json.loads(tool(**kwargs))
    request.assert_called_once()
    assert result.get("delivery") == "unknown", result
    assert result["error"].startswith("delivery unknown:")
    assert "may already be delivered" in result["error"]
    assert "do not resend" in result["error"]
    assert "check the chat first" in result["error"]


@pytest.mark.parametrize(
    "error", [TimeoutError("timed out"), urllib.error.URLError(TimeoutError("timed out"))]
)
def test_t6_non_send_timeout_keeps_existing_error(error):
    tools = _tools()
    # There is no registered read tool here; exercise GET on the same HTTP helper.
    api = inspect.getclosurevars(tools["send"]).nonlocals["_api"]
    with patch("urllib.request.urlopen", side_effect=error) as request:
        read_result = api("GET", "/broker/status")
        react_result = json.loads(tools["react"](message_id="101", emoji="👍"))
    assert request.call_count == 2
    assert read_result == {"error": str(error)}
    assert react_result == {"error": str(error)}


@pytest.mark.parametrize(
    "error", [urllib.error.URLError("connection refused"), ValueError("bad response")]
)
def test_other_outer_error_keeps_existing_shape(error):
    tool = _tools()["send"]
    with patch("urllib.request.urlopen", side_effect=error):
        result = json.loads(tool(chat_id="12345", platform="telegram", text="hello"))
    assert result == {"error": str(error)}


@pytest.mark.parametrize("json_body", [False, True])
def test_delivery_unknown_504_reaches_send_tool(json_body):
    detail = "delivery unknown: may already be delivered; do not resend; check the chat first"
    body = json.dumps({"detail": detail}) if json_body else detail
    error = urllib.error.HTTPError(
        "http://test", 504, "Gateway Timeout", {}, BytesIO(body.encode())
    )
    with patch("urllib.request.urlopen", side_effect=error):
        result = json.loads(
            _tools()["send"](
                chat_id="12345",
                platform="telegram",
                text="hello",
            )
        )
    assert result["status"] == 504
    assert detail in result["error"]


def test_broadcast_preserves_partial_unknown_result():
    payload = {
        "sent": True,
        "count": 2,
        "delivery_unknown": ["22222"],
        "deliveries": [
            {
                "chat_id": "22222",
                "delivery": "unknown",
                "error": "delivery unknown: do not resend; check the chat first",
            }
        ],
    }
    response = MagicMock()
    response.__enter__.return_value = response
    response.read.return_value = json.dumps(payload).encode()
    with patch("urllib.request.urlopen", return_value=response) as request:
        result = json.loads(_tools()["broadcast"](text="hello"))
    request.assert_called_once()
    assert result == payload
