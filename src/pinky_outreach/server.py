"""Outreach MCP Server — multi-platform messaging for Claude Code.

Exposes messaging capabilities as MCP tools. Start with Telegram,
extend to Discord/Slack/iMessage later.

Usage:
    python -m pinky_outreach --token $TELEGRAM_BOT_TOKEN
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

from mcp.server.fastmcp import FastMCP

from pinky_outreach.telegram import TelegramAdapter, TelegramError
from pinky_outreach.types import Platform


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def create_server(
    telegram_token: str = "",
    *,
    host: str = "127.0.0.1",
    port: int = 8101,
) -> FastMCP:
    mcp = FastMCP("pinky-outreach", host=host, port=port)

    # Initialize adapters based on available tokens
    telegram: TelegramAdapter | None = None
    if telegram_token:
        telegram = TelegramAdapter(telegram_token)
        _log("outreach: Telegram adapter initialized")

    @mcp.tool()
    def send_message(
        content: str,
        chat_id: str,
        platform: str = "telegram",
        reply_to: str = "",
        parse_mode: str = "",
        silent: bool = False,
    ) -> str:
        """Send a message to a chat on any configured platform.

        Args:
            content: Message text to send.
            chat_id: Target chat ID.
            platform: Platform to send on (telegram, discord, slack).
            reply_to: Message ID to reply to (optional).
            parse_mode: Text formatting: HTML or Markdown (Telegram only).
            silent: Send without notification sound.
        """
        if platform == "telegram":
            if not telegram:
                return json.dumps({"error": "Telegram not configured. Set TELEGRAM_BOT_TOKEN."})
            try:
                msg = telegram.send_message(
                    chat_id,
                    content,
                    reply_to_message_id=int(reply_to) if reply_to else None,
                    parse_mode=parse_mode or None,
                    disable_notification=silent,
                )
                _log(f"outreach: sent to telegram:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id, "platform": "telegram"})
            except TelegramError as e:
                return json.dumps({"error": str(e)})
        else:
            return json.dumps({"error": f"Platform '{platform}' not yet supported. Available: telegram"})

    @mcp.tool()
    def check_messages(
        platform: str = "telegram",
        timeout: int = 0,
        limit: int = 20,
    ) -> str:
        """Poll for new inbound messages.

        Uses long polling for Telegram. Returns new messages since last check.

        Args:
            platform: Platform to check (telegram).
            timeout: Long poll timeout in seconds (0 = instant, max 50).
            limit: Max messages to return (1-100).
        """
        if platform == "telegram":
            if not telegram:
                return json.dumps({"error": "Telegram not configured."})
            try:
                messages = telegram.get_updates(timeout=min(timeout, 50), limit=limit)
                _log(f"outreach: checked telegram, {len(messages)} new messages")
                return json.dumps({
                    "platform": "telegram",
                    "count": len(messages),
                    "messages": [m.to_dict() for m in messages],
                })
            except TelegramError as e:
                return json.dumps({"error": str(e)})
        else:
            return json.dumps({"error": f"Platform '{platform}' not supported."})

    @mcp.tool()
    def send_photo(
        chat_id: str,
        file_path: str,
        caption: str = "",
        platform: str = "telegram",
    ) -> str:
        """Send a photo to a chat.

        Args:
            chat_id: Target chat ID.
            file_path: Absolute path to the image file.
            caption: Optional caption text.
            platform: Platform (telegram).
        """
        if platform == "telegram":
            if not telegram:
                return json.dumps({"error": "Telegram not configured."})
            try:
                msg = telegram.send_photo(chat_id, file_path, caption=caption)
                _log(f"outreach: sent photo to telegram:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id})
            except TelegramError as e:
                return json.dumps({"error": str(e)})
        else:
            return json.dumps({"error": f"Platform '{platform}' not supported."})

    @mcp.tool()
    def send_document(
        chat_id: str,
        file_path: str,
        caption: str = "",
        platform: str = "telegram",
    ) -> str:
        """Send a file/document to a chat.

        Args:
            chat_id: Target chat ID.
            file_path: Absolute path to the file.
            caption: Optional caption text.
            platform: Platform (telegram).
        """
        if platform == "telegram":
            if not telegram:
                return json.dumps({"error": "Telegram not configured."})
            try:
                msg = telegram.send_document(chat_id, file_path, caption=caption)
                _log(f"outreach: sent document to telegram:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id})
            except TelegramError as e:
                return json.dumps({"error": str(e)})
        else:
            return json.dumps({"error": f"Platform '{platform}' not supported."})

    @mcp.tool()
    def get_chat_info(
        chat_id: str,
        platform: str = "telegram",
    ) -> str:
        """Get information about a chat.

        Args:
            chat_id: Chat ID to look up.
            platform: Platform (telegram).
        """
        if platform == "telegram":
            if not telegram:
                return json.dumps({"error": "Telegram not configured."})
            try:
                chat = telegram.get_chat(chat_id)
                _log(f"outreach: got chat info for telegram:{chat_id}")
                return json.dumps(chat.to_dict())
            except TelegramError as e:
                return json.dumps({"error": str(e)})
        else:
            return json.dumps({"error": f"Platform '{platform}' not supported."})

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
            message_id: Message to react to.
            emoji: Emoji to react with (e.g. "👍", "❤️").
            platform: Platform (telegram).
        """
        if platform == "telegram":
            if not telegram:
                return json.dumps({"error": "Telegram not configured."})
            try:
                telegram.set_reaction(chat_id, int(message_id), emoji)
                _log(f"outreach: reacted {emoji} to telegram:{chat_id}:{message_id}")
                return json.dumps({"reacted": True})
            except TelegramError as e:
                return json.dumps({"error": str(e)})
        else:
            return json.dumps({"error": f"Platform '{platform}' not supported."})

    @mcp.tool()
    def bot_info(platform: str = "telegram") -> str:
        """Get info about the configured bot.

        Args:
            platform: Platform (telegram).
        """
        if platform == "telegram":
            if not telegram:
                return json.dumps({"error": "Telegram not configured."})
            try:
                info = telegram.get_me()
                return json.dumps({
                    "platform": "telegram",
                    "id": info.get("id"),
                    "username": info.get("username"),
                    "first_name": info.get("first_name"),
                    "can_join_groups": info.get("can_join_groups"),
                })
            except TelegramError as e:
                return json.dumps({"error": str(e)})
        else:
            return json.dumps({"error": f"Platform '{platform}' not supported."})

    return mcp
