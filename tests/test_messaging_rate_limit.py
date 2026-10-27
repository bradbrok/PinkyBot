"""Messaging tools preserve Telegram throttling seconds in caller-visible errors."""

import json
import urllib.error
from io import BytesIO
from unittest.mock import patch

import pytest

from pinky_messaging.server import create_server
from tests.test_messaging_delivery_unknown import SEND_CASES


@pytest.mark.parametrize("name,kwargs", SEND_CASES, ids=[case[0] for case in SEND_CASES])
@pytest.mark.parametrize("json_body", [False, True], ids=["text-body", "json-detail"])
def test_rate_limit_delay_reaches_agent_tool_output(name, kwargs, json_body):
    detail = "rate limited by Telegram, retry after 8 s"
    body = json.dumps({"detail": detail}) if json_body else detail
    error = urllib.error.HTTPError(
        "http://example.invalid/broker/send", 429, "Too Many Requests",
        {"Retry-After": "8"}, BytesIO(body.encode()),
    )
    server = create_server(agent_name="sender", api_url="http://example.invalid")
    tools = {tool.name: tool.fn for tool in server._tool_manager.list_tools()}
    with patch("urllib.request.urlopen", side_effect=error) as request:
        result = json.loads(tools[name](**kwargs))
    request.assert_called_once()
    assert result == {"error": body, "status": 429}
    assert "rate limited by Telegram" in result["error"]
    assert "retry after 8 s" in result["error"]
    assert result.get("delivery") != "unknown"
