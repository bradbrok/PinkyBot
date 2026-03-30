"""Telegram adapter — Bot API via httpx.

Uses the Telegram Bot API directly (no heavy framework dependency).
Handles sending messages, polling for updates, and message history.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone

import httpx

from pinky_outreach.types import Chat, Message, Platform


class TelegramError(Exception):
    """Telegram API error."""

    def __init__(self, description: str, error_code: int = 0):
        self.description = description
        self.error_code = error_code
        super().__init__(f"Telegram API error {error_code}: {description}")


class TelegramAdapter:
    """Telegram Bot API adapter using httpx."""

    BASE_URL = "https://api.telegram.org/bot{token}"

    def __init__(self, bot_token: str, *, timeout: float = 30.0) -> None:
        self._token = bot_token
        self._base = self.BASE_URL.format(token=bot_token)
        self._client = httpx.Client(timeout=timeout)
        self._last_update_id: int = 0

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, **params) -> dict:
        """Make a Telegram Bot API request."""
        # Remove None values
        params = {k: v for k, v in params.items() if v is not None}

        url = f"{self._base}/{method}"
        resp = self._client.post(url, json=params)
        data = resp.json()

        if not data.get("ok"):
            raise TelegramError(
                data.get("description", "Unknown error"),
                data.get("error_code", 0),
            )

        return data.get("result", {})

    # ── Sending ──────────────────────────────────────────────

    def send_message(
        self,
        chat_id: str | int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
        parse_mode: str | None = None,
        disable_notification: bool = False,
    ) -> Message:
        """Send a text message."""
        result = self._request(
            "sendMessage",
            chat_id=chat_id,
            text=text,
            reply_to_message_id=reply_to_message_id,
            parse_mode=parse_mode,
            disable_notification=disable_notification,
        )

        return Message(
            platform=Platform.telegram,
            chat_id=str(result["chat"]["id"]),
            sender="bot",
            content=text,
            timestamp=datetime.fromtimestamp(result["date"], tz=timezone.utc),
            message_id=str(result["message_id"]),
            is_outbound=True,
        )

    def send_photo(
        self,
        chat_id: str | int,
        photo_path: str,
        *,
        caption: str = "",
    ) -> Message:
        """Send a photo from a local file path."""
        url = f"{self._base}/sendPhoto"
        with open(photo_path, "rb") as f:
            resp = self._client.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"photo": f},
            )
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(
                data.get("description", "Unknown error"),
                data.get("error_code", 0),
            )
        result = data["result"]
        return Message(
            platform=Platform.telegram,
            chat_id=str(result["chat"]["id"]),
            sender="bot",
            content=caption,
            timestamp=datetime.fromtimestamp(result["date"], tz=timezone.utc),
            message_id=str(result["message_id"]),
            is_outbound=True,
            metadata={"type": "photo"},
        )

    def send_document(
        self,
        chat_id: str | int,
        file_path: str,
        *,
        caption: str = "",
    ) -> Message:
        """Send a document/file."""
        url = f"{self._base}/sendDocument"
        with open(file_path, "rb") as f:
            resp = self._client.post(
                url,
                data={"chat_id": chat_id, "caption": caption},
                files={"document": f},
            )
        data = resp.json()
        if not data.get("ok"):
            raise TelegramError(
                data.get("description", "Unknown error"),
                data.get("error_code", 0),
            )
        result = data["result"]
        return Message(
            platform=Platform.telegram,
            chat_id=str(result["chat"]["id"]),
            sender="bot",
            content=caption,
            timestamp=datetime.fromtimestamp(result["date"], tz=timezone.utc),
            message_id=str(result["message_id"]),
            is_outbound=True,
            metadata={"type": "document"},
        )

    # ── Receiving ────────────────────────────────────────────

    def get_updates(
        self,
        *,
        timeout: int = 0,
        limit: int = 100,
    ) -> list[Message]:
        """Poll for new messages using long polling.

        Args:
            timeout: Long polling timeout in seconds (0 = no wait).
            limit: Max updates to fetch (1-100).
        """
        result = self._request(
            "getUpdates",
            offset=self._last_update_id + 1 if self._last_update_id else None,
            timeout=timeout,
            limit=limit,
            allowed_updates=["message"],
        )

        messages = []
        for update in result:
            self._last_update_id = max(self._last_update_id, update["update_id"])

            msg_data = update.get("message")
            if not msg_data:
                continue

            text = msg_data.get("text", "")
            caption = msg_data.get("caption", "")
            content = text or caption or ""

            sender_data = msg_data.get("from", {})
            sender_parts = [
                sender_data.get("first_name", ""),
                sender_data.get("last_name", ""),
            ]
            sender_name = " ".join(p for p in sender_parts if p) or "unknown"

            messages.append(Message(
                platform=Platform.telegram,
                chat_id=str(msg_data["chat"]["id"]),
                sender=sender_name,
                content=content,
                timestamp=datetime.fromtimestamp(msg_data["date"], tz=timezone.utc),
                message_id=str(msg_data["message_id"]),
                reply_to=str(msg_data["reply_to_message"]["message_id"])
                if msg_data.get("reply_to_message")
                else "",
                metadata={
                    "sender_id": str(sender_data.get("id", "")),
                    "username": sender_data.get("username", ""),
                    "chat_type": msg_data["chat"].get("type", ""),
                    "chat_title": msg_data["chat"].get("title", ""),
                    "reply_to_sender_id": str(msg_data.get("reply_to_message", {}).get("from", {}).get("id", "")),
                    "entities": msg_data.get("entities", []),
                },
            ))

        return messages

    # ── Actions ──────────────────────────────────────────────

    def send_chat_action(self, chat_id: str | int, action: str = "typing") -> None:
        """Send a chat action (e.g., 'typing' indicator)."""
        self._request("sendChatAction", chat_id=chat_id, action=action)

    # ── Info ─────────────────────────────────────────────────

    def get_me(self) -> dict:
        """Get bot info."""
        return self._request("getMe")

    def get_chat(self, chat_id: str | int) -> Chat:
        """Get chat info."""
        result = self._request("getChat", chat_id=chat_id)
        return Chat(
            platform=Platform.telegram,
            chat_id=str(result["id"]),
            title=result.get("title", result.get("first_name", "")),
            chat_type=result.get("type", ""),
            username=result.get("username", ""),
        )

    # ── Reactions ────────────────────────────────────────────

    def set_reaction(
        self,
        chat_id: str | int,
        message_id: int,
        emoji: str = "",
    ) -> bool:
        """Set a reaction on a message."""
        reaction = [{"type": "emoji", "emoji": emoji}] if emoji else []
        self._request(
            "setMessageReaction",
            chat_id=chat_id,
            message_id=message_id,
            reaction=reaction,
        )
        return True
