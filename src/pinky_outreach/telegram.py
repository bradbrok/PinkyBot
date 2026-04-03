"""Telegram adapter — Bot API via httpx.

Uses the Telegram Bot API directly (no heavy framework dependency).
Handles sending messages, polling for updates, and message history.
"""

from __future__ import annotations

import os
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
        reply_to_message_id: int | None = None,
    ) -> Message:
        """Send a photo from a local file path."""
        url = f"{self._base}/sendPhoto"
        with open(photo_path, "rb") as f:
            resp = self._client.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "reply_to_message_id": reply_to_message_id},
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
        reply_to_message_id: int | None = None,
    ) -> Message:
        """Send a document/file."""
        url = f"{self._base}/sendDocument"
        with open(file_path, "rb") as f:
            resp = self._client.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "reply_to_message_id": reply_to_message_id},
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

    def send_animation(
        self,
        chat_id: str | int,
        file_path: str,
        *,
        caption: str = "",
        reply_to_message_id: int | None = None,
    ) -> Message:
        """Send an animation (GIF) from a local file path."""
        url = f"{self._base}/sendAnimation"
        with open(file_path, "rb") as f:
            resp = self._client.post(
                url,
                data={"chat_id": chat_id, "caption": caption, "reply_to_message_id": reply_to_message_id},
                files={"animation": f},
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
            metadata={"type": "animation"},
        )

    def send_voice(
        self,
        chat_id: str | int,
        file_path: str,
        *,
        caption: str = "",
        duration: int = 0,
        reply_to_message_id: int | None = None,
    ) -> Message:
        """Send a voice message (.ogg opus) from a local file path."""
        url = f"{self._base}/sendVoice"
        data: dict = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if duration:
            data["duration"] = duration
        if reply_to_message_id is not None:
            data["reply_to_message_id"] = reply_to_message_id
        with open(file_path, "rb") as f:
            resp = self._client.post(url, data=data, files={"voice": f})
        result_data = resp.json()
        if not result_data.get("ok"):
            raise TelegramError(
                result_data.get("description", "Unknown error"),
                result_data.get("error_code", 0),
            )
        result = result_data["result"]
        return Message(
            platform=Platform.telegram,
            chat_id=str(result["chat"]["id"]),
            sender="bot",
            content=caption,
            timestamp=datetime.fromtimestamp(result["date"], tz=timezone.utc),
            message_id=str(result["message_id"]),
            is_outbound=True,
            metadata={"type": "voice"},
        )

    # ── Downloading ──────────────────────────────────────────

    def download_file(self, file_id: str, dest_dir: str = "/tmp/pinky_files") -> str:
        """Download a file from Telegram by file_id. Returns local path."""
        os.makedirs(dest_dir, exist_ok=True)

        # Step 1: Get file path from Telegram
        result = self._request("getFile", file_id=file_id)
        file_path = result.get("file_path", "")
        if not file_path:
            raise TelegramError("No file_path returned from getFile")

        # Step 2: Download the file
        download_url = f"https://api.telegram.org/file/bot{self._token}/{file_path}"
        resp = self._client.get(download_url)
        if resp.status_code >= 400:
            raise TelegramError(f"File download failed: {resp.status_code}")

        # Use file_id + original filename to avoid collisions
        original_name = file_path.rsplit("/", 1)[-1] if "/" in file_path else file_path
        local_name = f"{file_id}_{original_name}"
        local_path = os.path.join(dest_dir, local_name)

        with open(local_path, "wb") as f:
            f.write(resp.content)

        return local_path

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

            metadata = {
                "sender_id": str(sender_data.get("id", "")),
                "username": sender_data.get("username", ""),
                "chat_type": msg_data["chat"].get("type", ""),
                "chat_title": msg_data["chat"].get("title", ""),
                "reply_to_sender_id": str(msg_data.get("reply_to_message", {}).get("from", {}).get("id", "")),
                "entities": msg_data.get("entities", []),
            }

            # Detect file attachments
            attachments = []
            attachment_types = [
                ("photo", None),
                ("document", None),
                ("voice", None),
                ("video", None),
                ("audio", None),
                ("sticker", None),
            ]
            for att_type, _ in attachment_types:
                att_data = msg_data.get(att_type)
                if not att_data:
                    continue

                if att_type == "photo":
                    # photo is an array of PhotoSize, pick largest by file_size
                    largest = max(att_data, key=lambda p: p.get("file_size", 0))
                    attachments.append({
                        "type": "photo",
                        "file_id": largest["file_id"],
                        "file_size": largest.get("file_size", 0),
                    })
                else:
                    att_info: dict = {
                        "type": att_type,
                        "file_id": att_data["file_id"],
                    }
                    if "file_name" in att_data:
                        att_info["file_name"] = att_data["file_name"]
                    if "mime_type" in att_data:
                        att_info["mime_type"] = att_data["mime_type"]
                    if "file_size" in att_data:
                        att_info["file_size"] = att_data["file_size"]
                    attachments.append(att_info)

            if attachments:
                metadata["attachments"] = attachments

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
                metadata=metadata,
            ))

        return messages

    # ── Actions ──────────────────────────────────────────────

    def send_chat_action(self, chat_id: str | int, action: str = "typing") -> None:
        """Send a chat action (e.g., 'typing' indicator)."""
        self._request("sendChatAction", chat_id=chat_id, action=action)

    def edit_message_text(self, chat_id: str | int, message_id: int, text: str) -> dict:
        """Edit the text of a previously sent message."""
        try:
            return self._request(
                "editMessageText",
                chat_id=chat_id,
                message_id=message_id,
                text=text,
            )
        except Exception:
            return {}

    def delete_message(self, chat_id: str | int, message_id: int) -> bool:
        """Delete a message."""
        try:
            self._request("deleteMessage", chat_id=chat_id, message_id=message_id)
            return True
        except Exception:
            return False

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
