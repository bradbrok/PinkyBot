"""Outreach MCP Server — multi-platform messaging for Claude Code.

Exposes messaging capabilities as MCP tools. Supports Telegram, Discord, Slack,
iMessage, and WhatsApp. Tools are only registered when at least one platform is
configured — if no adapters are active, no tools are registered at all.

Usage:
    python -m pinky_outreach --token $TELEGRAM_BOT_TOKEN --discord-token $DISCORD_BOT_TOKEN
"""

from __future__ import annotations

import json
import sys

from mcp.server.fastmcp import FastMCP

from pinky_outreach.discord import DiscordAdapter, DiscordError
from pinky_outreach.imessage import iMessageAdapter, iMessageError
from pinky_outreach.slack import SlackAdapter, SlackError
from pinky_outreach.telegram import TelegramAdapter, TelegramError


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _not_configured(platform: str) -> str:
    token_map = {
        "telegram": "TELEGRAM_BOT_TOKEN",
        "discord": "DISCORD_BOT_TOKEN",
        "slack": "SLACK_BOT_TOKEN",
        "whatsapp": "WHATSAPP_ACCESS_TOKEN + WHATSAPP_PHONE_NUMBER_ID",
    }
    env_var = token_map.get(platform, f"{platform.upper()}_TOKEN")
    return _err(f"{platform.title()} not configured. Set {env_var}.")


SUPPORTED_PLATFORMS = ("telegram", "discord", "slack", "imessage", "whatsapp")


def create_server(
    telegram_token: str = "",
    discord_token: str = "",
    slack_token: str = "",
    imessage_enabled: bool = False,
    whatsapp_token: str = "",
    whatsapp_phone_id: str = "",
    *,
    host: str = "127.0.0.1",
    port: int = 8101,
) -> FastMCP:
    mcp = FastMCP("pinky-outreach", host=host, port=port)

    # Initialize adapters based on available tokens
    telegram: TelegramAdapter | None = None
    discord: DiscordAdapter | None = None
    slack: SlackAdapter | None = None
    imessage: iMessageAdapter | None = None
    whatsapp = None  # WhatsAppAdapter | None

    if telegram_token:
        telegram = TelegramAdapter(telegram_token)
        _log("outreach: Telegram adapter initialized")

    if discord_token:
        discord = DiscordAdapter(discord_token)
        _log("outreach: Discord adapter initialized")

    if slack_token:
        slack = SlackAdapter(slack_token)
        _log("outreach: Slack adapter initialized")

    if imessage_enabled:
        imessage = iMessageAdapter()
        _log(f"outreach: iMessage adapter initialized (receive: {imessage.can_receive})")

    if whatsapp_token and whatsapp_phone_id:
        from pinky_outreach.whatsapp import WhatsAppAdapter
        whatsapp = WhatsAppAdapter(whatsapp_token, whatsapp_phone_id)
        _log("outreach: WhatsApp adapter initialized")

    # Build list of active platforms
    active_platforms: list[str] = []
    if telegram:
        active_platforms.append("telegram")
    if discord:
        active_platforms.append("discord")
    if slack:
        active_platforms.append("slack")
    if imessage:
        active_platforms.append("imessage")
    if whatsapp:
        active_platforms.append("whatsapp")

    if not active_platforms:
        _log("outreach: no platforms configured, skipping tool registration")
        return mcp

    platform_list = ", ".join(active_platforms)
    default_platform = active_platforms[0]

    @mcp.tool()
    def list_platforms() -> str:
        """List configured messaging platforms."""
        return json.dumps({"platforms": active_platforms})

    @mcp.tool()
    def send_message(
        content: str,
        chat_id: str,
        platform: str = default_platform,
        reply_to: str = "",
        parse_mode: str = "",
        silent: bool = False,
    ) -> str:
        f"""Send a message to a chat on any configured platform.

        Available platforms: {platform_list}

        Args:
            content: Message text to send.
            chat_id: Target chat/channel ID or phone number (E.164 for WhatsApp).
            platform: Platform to send on ({platform_list}).
            reply_to: Message ID to reply to (optional). For Slack, this is a thread_ts.
            parse_mode: Text formatting: HTML or Markdown (Telegram only).
            silent: Send without notification sound (Telegram only).
        """
        if platform not in active_platforms:
            return _err(f"Platform '{platform}' not available. Configured: {platform_list}")

        if platform == "telegram":
            if not telegram:
                return _not_configured("telegram")
            try:
                try:
                    telegram.send_chat_action(chat_id, "typing")
                except Exception:
                    pass
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
                try:
                    discord.send_typing(chat_id)
                except Exception:
                    pass
                msg = discord.send_message(chat_id, content, reply_to=reply_to)
                _log(f"outreach: sent to discord:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id, "platform": "discord"})
            except DiscordError as e:
                return _err(str(e))

        elif platform == "slack":
            if not slack:
                return _not_configured("slack")
            try:
                msg = slack.send_message(chat_id, content, thread_ts=reply_to)
                _log(f"outreach: sent to slack:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id, "platform": "slack"})
            except SlackError as e:
                return _err(str(e))

        elif platform == "imessage":
            if not imessage:
                return _err("iMessage not enabled. Enable it in agent settings.")
            try:
                msg = imessage.send_message(chat_id, content)
                _log(f"outreach: sent to imessage:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id, "platform": "imessage"})
            except iMessageError as e:
                return _err(str(e))

        elif platform == "whatsapp":
            if not whatsapp:
                return _not_configured("whatsapp")
            try:
                from pinky_outreach.whatsapp import WhatsAppError
                msg = whatsapp.send_message(chat_id, content, reply_to_message_id=reply_to or None)
                _log(f"outreach: sent to whatsapp:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id, "platform": "whatsapp"})
            except Exception as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported. Available: {platform_list}")

    @mcp.tool()
    def check_messages(
        chat_id: str = "",
        platform: str = default_platform,
        timeout: int = 0,
        limit: int = 20,
        after: str = "",
    ) -> str:
        f"""Poll for new inbound messages.

        Telegram: uses long polling (chat_id not required, returns all new messages).
        Discord/Slack: fetches recent messages from a channel (chat_id required).
        WhatsApp: messages arrive via webhook only, not polling.

        Available platforms: {platform_list}

        Args:
            chat_id: Channel ID (required for Discord/Slack, ignored for Telegram/WhatsApp).
            platform: Platform to check ({platform_list}).
            timeout: Long poll timeout in seconds (Telegram only, 0 = instant, max 50).
            limit: Max messages to return (1-100).
            after: Only messages after this ID/timestamp (Discord/Slack).
        """
        if platform not in active_platforms:
            return _err(f"Platform '{platform}' not available. Configured: {platform_list}")

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

        elif platform == "slack":
            if not slack:
                return _not_configured("slack")
            if not chat_id:
                return _err("chat_id (channel ID) is required for Slack.")
            try:
                messages = slack.get_history(chat_id, limit=limit, oldest=after)
                _log(f"outreach: checked slack:{chat_id}, {len(messages)} messages")
                return json.dumps({
                    "platform": "slack",
                    "count": len(messages),
                    "messages": [m.to_dict() for m in messages],
                })
            except SlackError as e:
                return _err(str(e))

        elif platform == "imessage":
            if not imessage:
                return _err("iMessage not enabled.")
            try:
                messages = imessage.get_updates(limit=limit)
                _log(f"outreach: checked imessage, {len(messages)} new messages")
                return json.dumps({
                    "platform": "imessage",
                    "count": len(messages),
                    "messages": [m.to_dict() for m in messages],
                })
            except iMessageError as e:
                return _err(str(e))

        elif platform == "whatsapp":
            return json.dumps({
                "platform": "whatsapp",
                "info": "WhatsApp messages are received via webhook, not polling. "
                        "Messages arrive automatically.",
            })

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def send_photo(
        chat_id: str,
        file_path: str,
        caption: str = "",
        platform: str = default_platform,
    ) -> str:
        f"""Send a photo/image to a chat.

        Available platforms: {platform_list}

        Args:
            chat_id: Target chat/channel ID or phone number (E.164 for WhatsApp).
            file_path: Absolute path to the image file.
            caption: Optional caption text.
            platform: Platform ({platform_list}).
        """
        if platform not in active_platforms:
            return _err(f"Platform '{platform}' not available. Configured: {platform_list}")

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

        elif platform == "slack":
            if not slack:
                return _not_configured("slack")
            try:
                msg = slack.upload_file(chat_id, file_path, initial_comment=caption)
                _log(f"outreach: sent photo to slack:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id})
            except SlackError as e:
                return _err(str(e))

        elif platform == "whatsapp":
            if not whatsapp:
                return _not_configured("whatsapp")
            try:
                msg = whatsapp.send_photo(chat_id, file_path, caption=caption)
                _log(f"outreach: sent photo to whatsapp:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id})
            except Exception as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def send_document(
        chat_id: str,
        file_path: str,
        caption: str = "",
        platform: str = default_platform,
    ) -> str:
        f"""Send a file/document to a chat.

        Available platforms: {platform_list}

        Args:
            chat_id: Target chat/channel ID or phone number (E.164 for WhatsApp).
            file_path: Absolute path to the file.
            caption: Optional caption text.
            platform: Platform ({platform_list}).
        """
        if platform not in active_platforms:
            return _err(f"Platform '{platform}' not available. Configured: {platform_list}")

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

        elif platform == "slack":
            if not slack:
                return _not_configured("slack")
            try:
                msg = slack.upload_file(chat_id, file_path, initial_comment=caption)
                _log(f"outreach: sent document to slack:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id})
            except SlackError as e:
                return _err(str(e))

        elif platform == "whatsapp":
            if not whatsapp:
                return _not_configured("whatsapp")
            try:
                msg = whatsapp.send_document(chat_id, file_path, caption=caption)
                _log(f"outreach: sent document to whatsapp:{chat_id}")
                return json.dumps({"sent": True, "message_id": msg.message_id})
            except Exception as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def get_chat_info(
        chat_id: str,
        platform: str = default_platform,
    ) -> str:
        f"""Get information about a chat/channel.

        Available platforms: {platform_list}

        Args:
            chat_id: Chat/channel ID to look up.
            platform: Platform ({platform_list}).
        """
        if platform not in active_platforms:
            return _err(f"Platform '{platform}' not available. Configured: {platform_list}")

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

        elif platform == "slack":
            if not slack:
                return _not_configured("slack")
            try:
                chat = slack.get_channel_info(chat_id)
                _log(f"outreach: got channel info for slack:{chat_id}")
                return json.dumps(chat.to_dict())
            except SlackError as e:
                return _err(str(e))

        elif platform == "imessage":
            if not imessage:
                return _err("iMessage not enabled.")
            try:
                chat = imessage.get_chat(chat_id)
                _log(f"outreach: got chat info for imessage:{chat_id}")
                return json.dumps(chat.to_dict())
            except iMessageError as e:
                return _err(str(e))

        elif platform == "whatsapp":
            if not whatsapp:
                return _not_configured("whatsapp")
            try:
                info = whatsapp.get_me()
                _log(f"outreach: got phone info for whatsapp (target: {chat_id})")
                return json.dumps({"platform": "whatsapp", "phone_number_id": info})
            except Exception as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def add_reaction(
        chat_id: str,
        message_id: str,
        emoji: str,
        platform: str = default_platform,
    ) -> str:
        f"""React to a message with an emoji.

        Available platforms: {platform_list}

        Args:
            chat_id: Chat/channel containing the message.
            message_id: Message to react to. For Slack, this is the message timestamp (ts).
            emoji: Emoji to react with. For Slack, use name without colons (e.g. "thumbsup").
            platform: Platform ({platform_list}).
        """
        if platform not in active_platforms:
            return _err(f"Platform '{platform}' not available. Configured: {platform_list}")

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

        elif platform == "slack":
            if not slack:
                return _not_configured("slack")
            try:
                slack.add_reaction(chat_id, message_id, emoji)
                _log(f"outreach: reacted {emoji} to slack:{chat_id}:{message_id}")
                return json.dumps({"reacted": True})
            except SlackError as e:
                return _err(str(e))

        elif platform == "whatsapp":
            if not whatsapp:
                return _not_configured("whatsapp")
            try:
                whatsapp.send_reaction(chat_id, message_id, emoji)
                _log(f"outreach: reacted {emoji} to whatsapp:{chat_id}:{message_id}")
                return json.dumps({"reacted": True})
            except Exception as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def download_file(
        file_id: str,
        platform: str = default_platform,
        url: str = "",
        dest_dir: str = "/tmp/pinky_files",
    ) -> str:
        f"""Download a file attachment from any platform.

        Available platforms: {platform_list}

        Args:
            file_id: File ID (Telegram/WhatsApp media ID) or attachment ID (Discord/Slack).
            platform: Platform the file is from ({platform_list}).
            url: Direct download URL (required for Discord/Slack).
            dest_dir: Local directory to save files to.
        """
        import os

        if platform not in active_platforms:
            return _err(f"Platform '{platform}' not available. Configured: {platform_list}")

        if platform == "telegram":
            if not telegram:
                return _not_configured("telegram")
            try:
                path = telegram.download_file(file_id, dest_dir=dest_dir)
                size = os.path.getsize(path)
                _log(f"outreach: downloaded telegram file {file_id} -> {path}")
                return json.dumps({"downloaded": True, "path": path, "size": size})
            except TelegramError as e:
                return _err(str(e))

        elif platform == "discord":
            if not discord:
                return _not_configured("discord")
            if not url:
                return _err("url is required for Discord file downloads.")
            try:
                path = discord.download_file(url, dest_dir=dest_dir)
                size = os.path.getsize(path)
                _log(f"outreach: downloaded discord file {file_id} -> {path}")
                return json.dumps({"downloaded": True, "path": path, "size": size})
            except DiscordError as e:
                return _err(str(e))

        elif platform == "slack":
            if not slack:
                return _not_configured("slack")
            if not url:
                return _err("url is required for Slack file downloads.")
            try:
                path = slack.download_file(url, dest_dir=dest_dir)
                size = os.path.getsize(path)
                _log(f"outreach: downloaded slack file {file_id} -> {path}")
                return json.dumps({"downloaded": True, "path": path, "size": size})
            except SlackError as e:
                return _err(str(e))

        elif platform == "whatsapp":
            if not whatsapp:
                return _not_configured("whatsapp")
            try:
                path = whatsapp.download_file(file_id, dest_dir=dest_dir)
                size = os.path.getsize(path)
                _log(f"outreach: downloaded whatsapp file {file_id} -> {path}")
                return json.dumps({"downloaded": True, "path": path, "size": size})
            except Exception as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    @mcp.tool()
    def bot_info(platform: str = default_platform) -> str:
        f"""Get info about the configured bot.

        Available platforms: {platform_list}

        Args:
            platform: Platform ({platform_list}).
        """
        if platform not in active_platforms:
            return _err(f"Platform '{platform}' not available. Configured: {platform_list}")

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

        elif platform == "slack":
            if not slack:
                return _not_configured("slack")
            try:
                info = slack.get_bot_info()
                return json.dumps({
                    "platform": "slack",
                    "user_id": info.get("user_id"),
                    "bot_id": info.get("bot_id"),
                    "team": info.get("team"),
                    "user": info.get("user"),
                })
            except SlackError as e:
                return _err(str(e))

        elif platform == "whatsapp":
            if not whatsapp:
                return _not_configured("whatsapp")
            try:
                info = whatsapp.get_me()
                return json.dumps({
                    "platform": "whatsapp",
                    "phone_number_id": info.get("id"),
                    "display_phone_number": info.get("display_phone_number"),
                    "verified_name": info.get("verified_name"),
                })
            except Exception as e:
                return _err(str(e))

        else:
            return _err(f"Platform '{platform}' not supported.")

    return mcp
