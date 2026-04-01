"""Pinky Messaging — lightweight MCP server for agent-to-user messaging.

Routes messages through PinkyBot's broker API so agents can send
outbound messages to users without direct platform access. The broker
handles platform routing, markdown formatting, and delivery.

Usage:
    python -m pinky_messaging --agent barsik --api-url http://localhost:8888
"""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

from pinky_daemon.auth import build_internal_auth_headers


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
        headers = {"Content-Type": "application/json"} if data else {}
        secret = os.environ.get("PINKY_SESSION_SECRET", "")
        headers.update(build_internal_auth_headers(
            secret,
            agent_name=agent_name,
            method=method,
            path=path,
        ))
        req = urllib.request.Request(
            url, data=data, method=method,
            headers=headers,
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

    @mcp.tool()
    def send_voice_note(
        text: str,
        chat_id: str,
        platform: str = "telegram",
        provider: str = "openai",
        voice: str = "",
        model: str = "",
    ) -> str:
        """Generate a voice note from text (TTS) and send it as a voice message.

        Converts text to speech using the specified provider, then sends the
        audio through the Pinky messaging layer as a voice message.

        Args:
            text: Text to convert to speech.
            chat_id: Target chat/user ID on the platform.
            platform: Platform to send on (telegram, discord, slack).
            provider: TTS provider — "elevenlabs", "openai", or "deepgram".
            voice: Voice ID or name. Provider-specific:
                   - elevenlabs: voice ID (e.g. "21m00Tcm4TlvDq8ikWAM")
                   - openai: voice name (alloy, echo, fable, onyx, nova, shimmer)
                   - deepgram: model name (aura-asteria-en, aura-luna-en, etc.)
            model: Model override (provider-specific). Defaults:
                   - elevenlabs: eleven_turbo_v2_5
                   - openai: tts-1
                   - deepgram: aura-asteria-en
        """
        result = _api("POST", "/broker/send-voice", {
            "agent_name": agent_name,
            "platform": platform,
            "chat_id": chat_id,
            "text": text,
            "provider": provider,
            "voice": voice,
            "model": model,
        })
        if "error" in result:
            _log(f"messaging[{agent_name}]: voice note failed: {result['error']}")
        else:
            _log(f"messaging[{agent_name}]: sent voice to {platform}:{chat_id} (provider={provider})")
        return json.dumps(result)

    @mcp.tool()
    def send_gif(
        query: str,
        chat_id: str,
        caption: str = "",
        platform: str = "telegram",
    ) -> str:
        """Search Giphy and send the best matching GIF to a chat.

        Searches Giphy for the given query and sends it as an animation
        through the Pinky messaging layer. The Giphy API key is read from
        system settings (configure via the Settings panel).

        Args:
            query: Search term (e.g. "happy dance", "mind blown").
            chat_id: Target chat/user ID on the platform.
            caption: Optional caption text.
            platform: Platform to send on (telegram, discord, slack).
        """
        result = _api("POST", "/broker/send-gif", {
            "agent_name": agent_name,
            "platform": platform,
            "chat_id": chat_id,
            "query": query,
            "caption": caption,
        })
        if "error" in result:
            _log(f"messaging[{agent_name}]: send_gif failed: {result['error']}")
        else:
            _log(f"messaging[{agent_name}]: sent gif to {platform}:{chat_id} (query={query!r})")
        return json.dumps(result)

    return mcp
