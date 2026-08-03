"""Pinky Messaging — explicit outreach MCP for agent-to-user messaging."""

from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from collections.abc import Callable

from mcp.server.fastmcp import FastMCP

from pinky_daemon.auth import build_internal_auth_headers, resolve_request_signing_secret
from pinky_daemon.shared_mcp import LazyAgentName, resolve_lazy


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


# Timeout for the MCP tool's HTTP call to the PinkyBot API (the "outer" leg).
# This MUST exceed the API's inner platform-delivery timeout (the Telegram
# adapter's httpx client, default 30s) plus API/DB/network overhead. When the
# two were equal (both 30s), a slow platform leg could make this outer call
# time out *while the delivery was still in flight* — the tool reported a
# timeout, the agent retried, and the user got the message twice (issue #113).
# Giving the outer leg headroom means the tool waits for the real result
# (success or a genuine error) instead of preempting it. Tunable via
# PINKY_MCP_HTTP_TIMEOUT.
try:
    _API_HTTP_TIMEOUT = float(os.environ.get("PINKY_MCP_HTTP_TIMEOUT", "45"))
except (TypeError, ValueError):
    _API_HTTP_TIMEOUT = 45.0


def create_server(
    *,
    agent_name: str = "",
    api_url: str = "http://localhost:8888",
    host: str = "127.0.0.1",
    port: int = 8102,
    signing_key_resolver: Callable[[str], str | None] | None = None,
) -> FastMCP:
    """Create the pinky-messaging MCP server.

    ``signing_key_resolver`` (#623 shared-SSE per-request key binding):
    ``agent_name -> per-agent signing key | None``. In shared mode the daemon
    passes this to look up the request's resolved agent's key (the process has
    no PINKY_AGENT_KEY env); stdio mode passes None and uses the env key
    (increment 2). Both fall back to the global secret (dual-accept).
    """

    agent_name = LazyAgentName(agent_name)
    mcp = FastMCP("pinky-messaging", host=host, port=port)

    def _api(method: str, path: str, body: dict | None = None) -> dict:
        """Call the PinkyBot API."""
        url = f"{api_url}{path}"
        data = json.dumps(resolve_lazy(body)).encode() if body else None
        headers = {"Content-Type": "application/json"} if data else {}
        agent = str(agent_name)
        headers.update(build_internal_auth_headers(
            resolve_request_signing_secret(agent, signing_key_resolver),
            agent_name=agent,
            method=method,
            path=path,
        ))
        req = urllib.request.Request(
            url, data=data, method=method,
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=_API_HTTP_TIMEOUT) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            return {"error": error_body, "status": e.code}
        except Exception as e:
            return {"error": str(e)}

    def _link_preview_options(
        disable_link_preview: bool,
        prefer_large_media: bool,
        link_preview_above: bool,
    ) -> dict | None:
        """Build a Telegram LinkPreviewOptions object from convenience flags.

        Returns None when no flag is set so the broker leaves Telegram's
        default preview behaviour untouched.
        """
        opts: dict = {}
        if disable_link_preview:
            opts["is_disabled"] = True
        if prefer_large_media:
            opts["prefer_large_media"] = True
        if link_preview_above:
            opts["show_above_text"] = True
        return opts or None

    @mcp.tool()
    def thread(
        message_id: str,
        text: str,
        parse_mode: str = "",
        disable_link_preview: bool = False,
        prefer_large_media: bool = False,
        link_preview_above: bool = False,
        quote: str = "",
    ) -> str:
        """Quote-reply to a specific inbound message. The user sees your response linked to the original.

        Link-preview flags (Telegram): disable_link_preview suppresses the URL
        preview card; prefer_large_media forces a big thumbnail; link_preview_above
        floats the preview above your text.

        quote (Telegram): an exact substring of the message you're replying to;
        the reply then visibly quotes just that passage instead of the whole
        message. Must match the original text exactly, or Telegram rejects it.
        """
        result = _api("POST", "/broker/thread", {
            "agent_name": agent_name,
            "message_id": message_id,
            "content": text,
            "parse_mode": parse_mode,
            "link_preview_options": _link_preview_options(
                disable_link_preview, prefer_large_media, link_preview_above
            ),
            "quote": quote,
        })
        return json.dumps(result)

    @mcp.tool()
    def send(
        chat_id: str,
        platform: str,
        text: str,
        parse_mode: str = "",
        disable_link_preview: bool = False,
        prefer_large_media: bool = False,
        link_preview_above: bool = False,
        blocks: str = "",
        reply_to: str = "",
    ) -> str:
        """Send a message to a chat/channel. Use chat IDs, not display names.

        Slack threading: reply_to is the thread root ts. When an inbound header
        includes thread_root_ts and is_thread_reply:true, default to replying in
        that thread (thread() already does this for an inbound message_id). Omit
        reply_to to explicitly break out of the thread and post at the channel root.

        Link-preview flags (Telegram): disable_link_preview suppresses the URL
        preview card; prefer_large_media forces a big thumbnail; link_preview_above
        floats the preview above your text.

        blocks (Slack only): a JSON-encoded array of Block Kit block objects
        (e.g. an interactive approval card with buttons). Pass the `slack_blocks`
        value returned by a tool verbatim as a JSON string. `text` is still sent
        as the notification fallback. Ignored on non-Slack platforms; if the JSON
        is malformed it is dropped and the message is sent text-only.
        """
        result = _api("POST", "/broker/send", {
            "agent_name": agent_name,
            "platform": platform,
            "chat_id": chat_id,
            "content": text,
            "reply_to": reply_to,
            "parse_mode": parse_mode,
            "link_preview_options": _link_preview_options(
                disable_link_preview, prefer_large_media, link_preview_above
            ),
            "blocks": blocks or None,
        })
        return json.dumps(result)

    @mcp.tool()
    def react(
        message_id: str,
        emoji: str,
    ) -> str:
        """React to an inbound message with an emoji. Lightweight acknowledgment without a text reply."""
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
        spoiler: bool = False,
        caption_above: bool = False,
    ) -> str:
        """Send an image file. Provide message_id to reply, or chat_id+platform for standalone.

        Telegram: spoiler blurs the image behind a tap-to-reveal mask;
        caption_above renders the caption above the image. The caption is
        formatted (MarkdownV2) like a normal message.
        """
        result = _api("POST", "/broker/send-photo", {
            "agent_name": agent_name,
            "message_id": message_id,
            "platform": platform,
            "chat_id": chat_id,
            "file_path": file_path,
            "caption": caption,
            "has_spoiler": spoiler,
            "show_caption_above_media": caption_above,
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
        """Send a file/document (PDF, code, etc.). Provide message_id to reply, or chat_id+platform for standalone."""
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
    def send_video(
        file_path: str,
        caption: str = "",
        message_id: str = "",
        chat_id: str = "",
        platform: str = "telegram",
        spoiler: bool = False,
        caption_above: bool = False,
    ) -> str:
        """Send a video that plays inline (autoplay/streaming), not as a file download. Use for .mp4 clips/screen recordings. Provide message_id to reply, or chat_id+platform for standalone.

        Telegram: spoiler blurs the video behind a tap-to-reveal mask;
        caption_above renders the caption above the player. The caption is
        formatted (MarkdownV2) like a normal message.
        """
        result = _api("POST", "/broker/send-video", {
            "agent_name": agent_name,
            "message_id": message_id,
            "platform": platform,
            "chat_id": chat_id,
            "file_path": file_path,
            "caption": caption,
            "has_spoiler": spoiler,
            "show_caption_above_media": caption_above,
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
        """Convert text to speech via TTS and send as a voice message."""
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
        """Search Giphy and send the top result as a GIF."""
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
        """Send the same message to ALL active channels at once."""
        result = _api("POST", "/broker/broadcast", {
            "agent_name": agent_name,
            "content": text,
        })
        return json.dumps(result)

    return mcp
