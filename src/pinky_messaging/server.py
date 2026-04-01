"""Pinky Messaging — explicit outreach MCP for agent-to-user messaging."""

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
    def reply(
        message_id: str,
        text: str,
        parse_mode: str = "",
    ) -> str:
        """Reply to a specific inbound message using Pinky's message context."""
        result = _api("POST", "/broker/reply", {
            "agent_name": agent_name,
            "message_id": message_id,
            "content": text,
            "parse_mode": parse_mode,
        })
        return json.dumps(result)

    @mcp.tool()
    def send(
        chat_id: str,
        platform: str,
        text: str,
        parse_mode: str = "",
    ) -> str:
        """Send a proactive text message to any configured channel."""
        result = _api("POST", "/broker/send", {
            "agent_name": agent_name,
            "platform": platform,
            "chat_id": chat_id,
            "content": text,
            "parse_mode": parse_mode,
        })
        return json.dumps(result)

    @mcp.tool()
    def react(
        message_id: str,
        emoji: str,
    ) -> str:
        """React to a specific inbound message."""
        result = _api("POST", "/broker/react", {
            "agent_name": agent_name,
            "message_id": message_id,
            "emoji": emoji,
        })
        return json.dumps(result)

    @mcp.tool()
    def send_photo(
        file_path: str,
        caption: str = "",
        message_id: str = "",
        chat_id: str = "",
        platform: str = "telegram",
    ) -> str:
        """Send or reply with a photo."""
        result = _api("POST", "/broker/send-photo", {
            "agent_name": agent_name,
            "message_id": message_id,
            "platform": platform,
            "chat_id": chat_id,
            "file_path": file_path,
            "caption": caption,
        })
        return json.dumps(result)

    @mcp.tool()
    def send_document(
        file_path: str,
        caption: str = "",
        message_id: str = "",
        chat_id: str = "",
        platform: str = "telegram",
    ) -> str:
        """Send or reply with a document."""
        result = _api("POST", "/broker/send-document", {
            "agent_name": agent_name,
            "message_id": message_id,
            "platform": platform,
            "chat_id": chat_id,
            "file_path": file_path,
            "caption": caption,
        })
        return json.dumps(result)

    @mcp.tool()
    def send_voice(
        text: str,
        message_id: str = "",
        chat_id: str = "",
        platform: str = "telegram",
        provider: str = "openai",
        voice: str = "",
        model: str = "",
    ) -> str:
        """Generate TTS audio and send it as a voice message."""
        result = _api("POST", "/broker/send-voice", {
            "agent_name": agent_name,
            "message_id": message_id,
            "platform": platform,
            "chat_id": chat_id,
            "text": text,
            "provider": provider,
            "voice": voice,
            "model": model,
        })
        return json.dumps(result)

    @mcp.tool()
    def send_gif(
        query: str,
        caption: str = "",
        message_id: str = "",
        chat_id: str = "",
        platform: str = "telegram",
    ) -> str:
        """Search Giphy and send the best matching GIF."""
        result = _api("POST", "/broker/send-gif", {
            "agent_name": agent_name,
            "message_id": message_id,
            "platform": platform,
            "chat_id": chat_id,
            "query": query,
            "caption": caption,
        })
        return json.dumps(result)

    @mcp.tool()
    def broadcast(
        text: str,
    ) -> str:
        """Broadcast a message to all active channels for this agent."""
        result = _api("POST", "/broker/broadcast", {
            "agent_name": agent_name,
            "content": text,
        })
        return json.dumps(result)

    @mcp.tool()
    def send_message(
        content: str,
        chat_id: str,
        platform: str = "telegram",
    ) -> str:
        """Deprecated alias for send()."""
        return send(chat_id=chat_id, platform=platform, text=content)

    @mcp.tool()
    def add_reaction(
        chat_id: str,
        message_id: str,
        emoji: str,
        platform: str = "telegram",
    ) -> str:
        """Deprecated alias for react()."""
        del chat_id, platform
        return react(message_id=message_id, emoji=emoji)

    @mcp.tool()
    def send_voice_note(
        text: str,
        chat_id: str,
        platform: str = "telegram",
        provider: str = "openai",
        voice: str = "",
        model: str = "",
    ) -> str:
        """Deprecated alias for proactive send_voice()."""
        return send_voice(
            text=text,
            chat_id=chat_id,
            platform=platform,
            provider=provider,
            voice=voice,
            model=model,
        )

    return mcp
