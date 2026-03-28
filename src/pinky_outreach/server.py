"""Outreach MCP Server — multi-platform messaging for Claude Code.

Exposes messaging capabilities as MCP tools. Supports Telegram and Discord,
with Slack and iMessage planned.

Usage:
    python -m pinky_outreach --token $TELEGRAM_BOT_TOKEN --discord-token $DISCORD_BOT_TOKEN
"""

from __future__ import annotations

import json
import sys

from mcp.server.fastmcp import FastMCP

from pinky_outreach.discord import DiscordAdapter, DiscordError
from pinky_outreach.telegram import TelegramAdapter, TelegramError


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _not_configured(platform: str) -> str:
    token_map = {
        "telegram": "TELEGRAM_BOT_TOKEN",
        "discord": "DISCORD_BOT_TOKEN",
    }
    env_var = token_map.get(platform, f"{platform.upper()}_TOKEN")
    return _err(f"{platform.title()} not configured. Set {env_var}.")


SUPPORTED_PLATFORMS = ("telegram", "discord")


def create_server(
    telegram_token: str = "",
    discord_token: str = "",
    *,
    host: str = "127.0.0.1",
    port: int = 8101,
) -> FastMCP:
    mcp = FastMCP("pinky-outreach", host=host, port=port)

    # Initialize adapters based on available tokens
    telegram: TelegramAdapter | None = None
    discord: DiscordAdapter | None = None

    if telegram_token:
        telegram = TelegramAdapter(telegram_token)
        _log("outreach: Telegram adapter initialized")

    if discord_token:
        discord = DiscordAdapter(discord_token)
        _log("outreach: Discord adapter initialized")

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
            chat_id: Target chat/channel ID.
            platform: Platform to send on (telegram, discord).
            reply_to: Message ID to reply to (optional).
            parse_mode: Text formatting: HTML or Markdown (Telegram only).
            silent: Send without notification sound (Telegram only).
        """
        if platform == "telegram":
            if not telegram:
                return _not_configured("telegram")
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
                return _err(str(e))

        elif platform == "discord":
            if not discord:
                return _not_configured("discord")
            try:
                msg = discord.send_message(chat_id, content, reply_to=reply_to)
                _log(f"outreach: sent to discord:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id, "platform": "discord"})
            except DiscordError as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported. Available: {', '.join(SUPPORTED_PLATFORMS)}")

    @mcp.tool()
    def check_messages(
        chat_id: str = "",
        platform: str = "telegram",
        timeout: int = 0,
        limit: int = 20,
        after: str = "",
    ) -> str:
        """Poll for new inbound messages.

        Telegram: uses long polling (chat_id not required, returns all new messages).
        Discord: fetches recent messages from a channel (chat_id required).

        Args:
            chat_id: Channel ID (required for Discord, ignored for Telegram).
            platform: Platform to check (telegram, discord).
            timeout: Long poll timeout in seconds (Telegram only, 0 = instant, max 50).
            limit: Max messages to return (1-100).
            after: Only messages after this ID (Discord only).
        """
        if platform == "telegram":
            if not telegram:
                return _not_configured("telegram")
            try:
                messages = telegram.get_updates(timeout=min(timeout, 50), limit=limit)
                _log(f"outreach: checked telegram, {len(messages)} new messages")
                return json.dumps({
                    "platform": "telegram",
                    "count": len(messages),
                    "messages": [m.to_dict() for m in messages],
                })
            except TelegramError as e:
                return _err(str(e))

        elif platform == "discord":
            if not discord:
                return _not_configured("discord")
            if not chat_id:
                return _err("chat_id (channel ID) is required for Discord.")
            try:
                messages = discord.get_messages(chat_id, limit=limit, after=after)
                _log(f"outreach: checked discord:{chat_id}, {len(messages)} messages")
                return json.dumps({
                    "platform": "discord",
                    "count": len(messages),
                    "messages": [m.to_dict() for m in messages],
                })
            except DiscordError as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def send_photo(
        chat_id: str,
        file_path: str,
        caption: str = "",
        platform: str = "telegram",
    ) -> str:
        """Send a photo/image to a chat.

        Args:
            chat_id: Target chat/channel ID.
            file_path: Absolute path to the image file.
            caption: Optional caption text.
            platform: Platform (telegram, discord).
        """
        if platform == "telegram":
            if not telegram:
                return _not_configured("telegram")
            try:
                msg = telegram.send_photo(chat_id, file_path, caption=caption)
                _log(f"outreach: sent photo to telegram:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id})
            except TelegramError as e:
                return _err(str(e))

        elif platform == "discord":
            if not discord:
                return _not_configured("discord")
            try:
                msg = discord.send_file(chat_id, file_path, content=caption)
                _log(f"outreach: sent photo to discord:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id})
            except DiscordError as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def send_document(
        chat_id: str,
        file_path: str,
        caption: str = "",
        platform: str = "telegram",
    ) -> str:
        """Send a file/document to a chat.

        Args:
            chat_id: Target chat/channel ID.
            file_path: Absolute path to the file.
            caption: Optional caption text.
            platform: Platform (telegram, discord).
        """
        if platform == "telegram":
            if not telegram:
                return _not_configured("telegram")
            try:
                msg = telegram.send_document(chat_id, file_path, caption=caption)
                _log(f"outreach: sent document to telegram:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id})
            except TelegramError as e:
                return _err(str(e))

        elif platform == "discord":
            if not discord:
                return _not_configured("discord")
            try:
                msg = discord.send_file(chat_id, file_path, content=caption)
                _log(f"outreach: sent document to discord:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id})
            except DiscordError as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def get_chat_info(
        chat_id: str,
        platform: str = "telegram",
    ) -> str:
        """Get information about a chat/channel.

        Args:
            chat_id: Chat/channel ID to look up.
            platform: Platform (telegram, discord).
        """
        if platform == "telegram":
            if not telegram:
                return _not_configured("telegram")
            try:
                chat = telegram.get_chat(chat_id)
                _log(f"outreach: got chat info for telegram:{chat_id}")
                return json.dumps(chat.to_dict())
            except TelegramError as e:
                return _err(str(e))

        elif platform == "discord":
            if not discord:
                return _not_configured("discord")
            try:
                chat = discord.get_channel(chat_id)
                _log(f"outreach: got channel info for discord:{chat_id}")
                return json.dumps(chat.to_dict())
            except DiscordError as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def add_reaction(
        chat_id: str,
        message_id: str,
        emoji: str,
        platform: str = "telegram",
    ) -> str:
        """React to a message with an emoji.

        Args:
            chat_id: Chat/channel containing the message.
            message_id: Message to react to.
            emoji: Emoji to react with (e.g. "thumbsup", "heart").
            platform: Platform (telegram, discord).
        """
        if platform == "telegram":
            if not telegram:
                return _not_configured("telegram")
            try:
                telegram.set_reaction(chat_id, int(message_id), emoji)
                _log(f"outreach: reacted {emoji} to telegram:{chat_id}:{message_id}")
                return json.dumps({"reacted": True})
            except TelegramError as e:
                return _err(str(e))

        elif platform == "discord":
            if not discord:
                return _not_configured("discord")
            try:
                discord.add_reaction(chat_id, message_id, emoji)
                _log(f"outreach: reacted {emoji} to discord:{chat_id}:{message_id}")
                return json.dumps({"reacted": True})
            except DiscordError as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def bot_info(platform: str = "telegram") -> str:
        """Get info about the configured bot.

        Args:
            platform: Platform (telegram, discord).
        """
        if platform == "telegram":
            if not telegram:
                return _not_configured("telegram")
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
                return _err(str(e))

        elif platform == "discord":
            if not discord:
                return _not_configured("discord")
            try:
                info = discord.get_me()
                return json.dumps({
                    "platform": "discord",
                    "id": info.get("id"),
                    "username": info.get("username"),
                    "discriminator": info.get("discriminator"),
                    "bot": info.get("bot"),
                })
            except DiscordError as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    return mcp
