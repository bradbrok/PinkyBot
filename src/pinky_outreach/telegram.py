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

    EMOJI_SHORTCUTS: dict[str, str] = {
        "+1": "\U0001f44d", "thumbsup": "\U0001f44d", "thumbs_up": "\U0001f44d",
        "-1": "\U0001f44e", "thumbsdown": "\U0001f44e", "thumbs_down": "\U0001f44e",
        "heart": "\u2764", "love": "\u2764",
        "fire": "\U0001f525", "lit": "\U0001f525",
        "clap": "\U0001f44f", "applause": "\U0001f44f",
        "laugh": "\U0001f601", "grin": "\U0001f601",
        "think": "\U0001f914", "thinking": "\U0001f914", "hmm": "\U0001f914",
        "mind_blown": "\U0001f92f", "exploding_head": "\U0001f92f",
        "scream": "\U0001f631",
        "cry": "\U0001f622", "sad": "\U0001f622",
        "sob": "\U0001f62d",
        "party": "\U0001f389", "celebrate": "\U0001f389", "tada": "\U0001f389",
        "star_eyes": "\U0001f929", "starstruck": "\U0001f929",
        "puke": "\U0001f92e", "vomit": "\U0001f92e",
        "poo": "\U0001f4a9", "poop": "\U0001f4a9",
        "pray": "\U0001f64f", "thanks": "\U0001f64f", "please": "\U0001f64f",
        "ok": "\U0001f44c", "ok_hand": "\U0001f44c",
        "peace": "\U0001f54a", "dove": "\U0001f54a",
        "clown": "\U0001f921",
        "yawn": "\U0001f971",
        "drunk": "\U0001f974",
        "heart_eyes": "\U0001f60d",
        "whale": "\U0001f433",
        "100": "\U0001f4af",
        "rofl": "\U0001f923", "lmao": "\U0001f923",
        "zap": "\u26a1", "lightning": "\u26a1",
        "banana": "\U0001f34c",
        "trophy": "\U0001f3c6", "win": "\U0001f3c6",
        "broken_heart": "\U0001f494",
        "raised_eyebrow": "\U0001f928", "sus": "\U0001f928",
        "neutral": "\U0001f610", "meh": "\U0001f610",
        "strawberry": "\U0001f353",
        "champagne": "\U0001f37e", "cheers": "\U0001f37e",
        "kiss": "\U0001f48b",
        "middle_finger": "\U0001f595", "flip": "\U0001f595",
        "devil": "\U0001f608", "evil": "\U0001f608",
        "sleep": "\U0001f634", "zzz": "\U0001f634",
        "nerd": "\U0001f913",
        "ghost": "\U0001f47b", "boo": "\U0001f47b",
        "technologist": "\U0001f468\u200d\U0001f4bb", "coder": "\U0001f468\u200d\U0001f4bb",
        "eyes": "\U0001f440", "look": "\U0001f440",
        "pumpkin": "\U0001f383",
        "see_no_evil": "\U0001f648",
        "angel": "\U0001f607", "halo": "\U0001f607",
        "fear": "\U0001f628",
        "handshake": "\U0001f91d", "deal": "\U0001f91d",
        "writing": "\u270d",
        "hug": "\U0001f917", "hugs": "\U0001f917",
        "salute": "\U0001fae1",
        "santa": "\U0001f385",
        "xmas_tree": "\U0001f384",
        "snowman": "\u2603",
        "nail_polish": "\U0001f485", "slay": "\U0001f485",
        "crazy": "\U0001f92a", "zany": "\U0001f92a",
        "moai": "\U0001f5ff", "stone": "\U0001f5ff",
        "cool": "\U0001f192",
        "cupid": "\U0001f498",
        "hear_no_evil": "\U0001f649",
        "unicorn": "\U0001f984",
        "kiss_face": "\U0001f618", "muah": "\U0001f618",
        "pill": "\U0001f48a",
        "speak_no_evil": "\U0001f64a",
        "sunglasses": "\U0001f60e", "cool_face": "\U0001f60e",
        "alien": "\U0001f47e", "space_invader": "\U0001f47e",
        "shrug": "\U0001f937",
        "angry": "\U0001f621", "rage": "\U0001f621",
    }

    def _resolve_emoji(self, emoji: str) -> str:
        """Resolve a shortcode or pass through a unicode emoji."""
        return self.EMOJI_SHORTCUTS.get(emoji.lower().strip(": "), emoji)

    def set_reaction(
        self,
        chat_id: str | int,
        message_id: int,
        emoji: str = "",
    ) -> bool:
        """Set a reaction on a message."""
        resolved = self._resolve_emoji(emoji) if emoji else ""
        reaction = [{"type": "emoji", "emoji": resolved}] if resolved else []
        self._request(
            "setMessageReaction",
            chat_id=chat_id,
            message_id=message_id,
            reaction=reaction,
        )
        return True
