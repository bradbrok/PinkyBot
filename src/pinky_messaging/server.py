"""Pinky Messaging — explicit outreach MCP for agent-to-user messaging."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP

from pinky_daemon.auth import build_internal_auth_headers
from pinky_daemon.shared_mcp import LazyAgentName, resolve_lazy


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

    agent_name = LazyAgentName(agent_name)
    mcp = FastMCP("pinky-messaging", host=host, port=port)

    def _api(method: str, path: str, body: dict | None = None) -> dict:
        """Call the PinkyBot API."""
        url = f"{api_url}{path}"
        data = json.dumps(resolve_lazy(body)).encode() if body else None
        headers = {"Content-Type": "application/json"} if data else {}
        secret = os.environ.get("PINKY_SESSION_SECRET", "")
        headers.update(build_internal_auth_headers(
            secret,
            agent_name=str(agent_name),
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
            error_body = e.read().decode("utf-8", errors="replace")
            return {"error": error_body, "status": e.code}
        except Exception as e:
            return {"error": str(e)}

    @mcp.tool()
    def thread(
        message_id: str,
        text: str,
        parse_mode: str = "",
    ) -> str:
        """Quote-reply to a specific inbound message by message_id.

        WHEN TO USE: You want your response to appear as a threaded reply
        linked to a specific message (the user sees it quoted). Requires
        a message_id from an inbound message.
        NOT FOR: Sending a standalone message (use send), or just
        acknowledging without text (use react)."""
        result = _api("POST", "/broker/thread", {
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
        """Send a new standalone message to a specific chat/channel.

        WHEN TO USE: You want to send a message that isn't a reply to anything —
        proactive outreach, scheduled updates, or responding in a channel.
        Requires chat_id and platform. Use chat IDs, not display names.
        NOT FOR: Replying to a specific message (use thread), reacting with
        emoji (use react), or messaging all channels (use broadcast)."""
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
        """Add an emoji reaction to a specific inbound message.

        WHEN TO USE: You want to acknowledge a message without a full text
        reply — thumbs up, heart, laugh, etc. Lightweight acknowledgment.
        NOT FOR: Sending a text response (use send or thread)."""
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
        """Send an image file to a chat, optionally as a reply.

        WHEN TO USE: You have an image file on disk to share. Provide
        message_id to send as a reply, or chat_id+platform to send standalone.
        NOT FOR: Text messages (use send), documents/PDFs (use send_document)."""
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
        """Send a file/document to a chat, optionally as a reply.

        WHEN TO USE: You have a non-image file to share (PDF, code, research
        export, etc.). Provide message_id to reply, or chat_id+platform standalone.
        NOT FOR: Images (use send_photo), text messages (use send)."""
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
        """Convert text to speech and send as a voice message.

        WHEN TO USE: You want to send an audio/voice message — the text is
        synthesized into speech via TTS. Good for personal greetings, voice
        notes, or when the user prefers audio.
        NOT FOR: Sending text (use send), or sending an existing audio file
        (use send_document)."""
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
        """Search Giphy for a GIF and send it to a chat.

        WHEN TO USE: You want to send a fun/expressive GIF reaction. Searches
        Giphy by query and sends the top result. Good for humor, celebration,
        or casual acknowledgment.
        NOT FOR: Sending a specific image file (use send_photo)."""
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
        """Send the same message to ALL your active channels at once.

        WHEN TO USE: You have an announcement, alert, or status update that
        should reach every configured channel simultaneously.
        NOT FOR: Messaging a specific user/chat (use send with chat_id)."""
        result = _api("POST", "/broker/broadcast", {
            "agent_name": agent_name,
            "content": text,
        })
        return json.dumps(result)

    return mcp
