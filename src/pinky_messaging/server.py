"""Pinky Messaging — lightweight MCP server for agent-to-user messaging.

Routes messages through PinkyBot's broker API so agents can send
outbound messages to users without direct platform access. The broker
handles platform routing, markdown formatting, and delivery.

Usage:
    python -m pinky_messaging --agent barsik --api-url http://localhost:8888
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def create_server(
    *,
    agent_name: str = "",
    api_url: str = "http://localhost:8888",
    host: str = "127.0.0.1",
    port: int = 8102,
) -> FastMCP:
    """Create the pinky-messaging MCP server."""

    mcp = FastMCP("pinky-messaging", host=host, port=port)

    def _api(method: str, path: str, body: dict | None = None) -> dict:
        """Call the PinkyBot API."""
        url = f"{api_url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode()
            return {"error": error_body, "status": e.code}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def send_message(
        content: str,
        chat_id: str,
        platform: str = "telegram",
    ) -> str:
        """Send a message to a user through the Pinky messaging layer.

        Messages are routed through the broker which handles platform
        formatting, markdown conversion, and delivery.

        Args:
            content: Message text to send.
            chat_id: Target chat/user ID on the platform.
            platform: Platform to send on (telegram, discord, slack).
        """
        result = _api("POST", "/broker/send", {
            "agent_name": agent_name,
            "platform": platform,
            "chat_id": chat_id,
            "content": content,
        })
        if "error" in result:
            _log(f"messaging[{agent_name}]: send failed: {result['error']}")
        else:
            _log(f"messaging[{agent_name}]: sent to {platform}:{chat_id}")
        return json.dumps(result)

    @mcp.tool()
    def send_photo(
        chat_id: str,
        file_path: str,
        caption: str = "",
        platform: str = "telegram",
    ) -> str:
        """Send a photo to a user through the Pinky messaging layer.

        Args:
            chat_id: Target chat/user ID.
            file_path: Absolute path to the image file.
            caption: Optional caption text.
            platform: Platform (telegram, discord, slack).
        """
        result = _api("POST", "/broker/send-photo", {
            "agent_name": agent_name,
            "platform": platform,
            "chat_id": chat_id,
            "file_path": file_path,
            "caption": caption,
        })
        return json.dumps(result)

    @mcp.tool()
    def send_document(
        chat_id: str,
        file_path: str,
        caption: str = "",
        platform: str = "telegram",
    ) -> str:
        """Send a file/document to a user through the Pinky messaging layer.

        Args:
            chat_id: Target chat/user ID.
            file_path: Absolute path to the file.
            caption: Optional caption text.
            platform: Platform (telegram, discord, slack).
        """
        result = _api("POST", "/broker/send-document", {
            "agent_name": agent_name,
            "platform": platform,
            "chat_id": chat_id,
            "file_path": file_path,
            "caption": caption,
        })
        return json.dumps(result)

    @mcp.tool()
    def add_reaction(
        chat_id: str,
        message_id: str,
        emoji: str,
        platform: str = "telegram",
    ) -> str:
        """React to a message with an emoji.

        Args:
            chat_id: Chat containing the message.
            message_id: Message ID to react to.
            emoji: Emoji to react with.
            platform: Platform (telegram, discord, slack).
        """
        result = _api("POST", "/broker/react", {
            "agent_name": agent_name,
            "platform": platform,
            "chat_id": chat_id,
            "message_id": message_id,
            "emoji": emoji,
        })
        return json.dumps(result)

    return mcp
