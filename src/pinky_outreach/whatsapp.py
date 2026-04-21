"""WhatsApp Cloud API adapter using httpx."""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from pinky_outreach.types import Message, Platform


class WhatsAppError(Exception):
    """WhatsApp Cloud API error."""

    def __init__(self, message: str, error_code: int = 0):
        self.message = message
        self.error_code = error_code
        super().__init__(f"WhatsApp API error {error_code}: {message}")


class WhatsAppAdapter:
    """WhatsApp Cloud API adapter using httpx."""

    BASE_URL = "https://graph.facebook.com/v21.0"

    def __init__(self, access_token: str, phone_number_id: str, *, timeout: float = 30.0) -> None:
        self._token = access_token
        self._phone_id = phone_number_id
        self._base = f"{self.BASE_URL}/{phone_number_id}"
        self._client = httpx.Client(timeout=timeout)

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> dict:
        """Make a WhatsApp Cloud API request with auth header."""
        headers = {"Authorization": f"Bearer {self._token}", "Content-Type": "application/json"}
        url = f"{self.BASE_URL}{path}" if path.startswith("/") else f"{self._base}/{path}"
        resp = self._client.request(method, url, headers=headers, **kwargs)
        data = resp.json()
        if "error" in data:
            err = data["error"]
            raise WhatsAppError(err.get("message", "Unknown error"), err.get("code", 0))
        return data

    def send_message(
        self,
        chat_id: str,
        text: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> Message:
        """Send a text message. chat_id is phone number in E.164 format."""
        body: dict = {
            "messaging_product": "whatsapp",
            "to": chat_id,
            "type": "text",
            "text": {"body": text},
        }
        if reply_to_message_id:
            body["context"] = {"message_id": reply_to_message_id}
        result = self._request("POST", "messages", json=body)
        msgs = result.get("messages") or [{}]
        msg_id = msgs[0].get("id", "") if msgs else ""
        return Message(
            platform=Platform.whatsapp,
            chat_id=chat_id,
            sender="bot",
            content=text,
            timestamp=datetime.now(timezone.utc),
            message_id=msg_id,
        )

    def send_photo(
        self,
        chat_id: str,
        file_path: str,
        *,
        caption: str = "",
        reply_to_message_id: str | None = None,
    ) -> Message:
        """Upload image then send as image message."""
        import mimetypes

        mime = mimetypes.guess_type(file_path)[0] or "image/jpeg"
        media_id = self._upload_media(file_path, mime)
        body: dict = {
            "messaging_product": "whatsapp",
            "to": chat_id,
            "type": "image",
            "image": {"id": media_id, "caption": caption} if caption else {"id": media_id},
        }
        if reply_to_message_id:
            body["context"] = {"message_id": reply_to_message_id}
        result = self._request("POST", "messages", json=body)
        msg_id = ((result.get("messages") or [{}])[0]).get("id", "")
        return Message(
            platform=Platform.whatsapp,
            chat_id=chat_id,
            sender="bot",
            content=caption or "[photo]",
            timestamp=datetime.now(timezone.utc),
            message_id=msg_id,
        )

    def send_document(
        self,
        chat_id: str,
        file_path: str,
        *,
        caption: str = "",
        reply_to_message_id: str | None = None,
    ) -> Message:
        """Upload file then send as document message."""
        import mimetypes

        mime = mimetypes.guess_type(file_path)[0] or "application/octet-stream"
        media_id = self._upload_media(file_path, mime)
        filename = os.path.basename(file_path)
        body: dict = {
            "messaging_product": "whatsapp",
            "to": chat_id,
            "type": "document",
            "document": {"id": media_id, "filename": filename},
        }
        if caption:
            body["document"]["caption"] = caption
        if reply_to_message_id:
            body["context"] = {"message_id": reply_to_message_id}
        result = self._request("POST", "messages", json=body)
        msg_id = ((result.get("messages") or [{}])[0]).get("id", "")
        return Message(
            platform=Platform.whatsapp,
            chat_id=chat_id,
            sender="bot",
            content=caption or f"[document: {filename}]",
            timestamp=datetime.now(timezone.utc),
            message_id=msg_id,
        )

    def send_voice(
        self,
        chat_id: str,
        file_path: str,
        *,
        reply_to_message_id: str | None = None,
    ) -> Message:
        """Send audio as voice message."""
        import mimetypes

        mime = mimetypes.guess_type(file_path)[0] or "audio/ogg"
        media_id = self._upload_media(file_path, mime)
        body: dict = {
            "messaging_product": "whatsapp",
            "to": chat_id,
            "type": "audio",
            "audio": {"id": media_id},
        }
        if reply_to_message_id:
            body["context"] = {"message_id": reply_to_message_id}
        result = self._request("POST", "messages", json=body)
        msg_id = ((result.get("messages") or [{}])[0]).get("id", "")
        return Message(
            platform=Platform.whatsapp,
            chat_id=chat_id,
            sender="bot",
            content="[voice]",
            timestamp=datetime.now(timezone.utc),
            message_id=msg_id,
        )

    def send_video(
        self,
        chat_id: str,
        file_path: str,
        *,
        caption: str = "",
        reply_to_message_id: str | None = None,
    ) -> Message:
        """Send video/animation message."""
        media_id = self._upload_media(file_path, "video/mp4")
        body: dict = {
            "messaging_product": "whatsapp",
            "to": chat_id,
            "type": "video",
            "video": {"id": media_id},
        }
        if caption:
            body["video"]["caption"] = caption
        if reply_to_message_id:
            body["context"] = {"message_id": reply_to_message_id}
        result = self._request("POST", "messages", json=body)
        msg_id = ((result.get("messages") or [{}])[0]).get("id", "")
        return Message(
            platform=Platform.whatsapp,
            chat_id=chat_id,
            sender="bot",
            content=caption or "[video]",
            timestamp=datetime.now(timezone.utc),
            message_id=msg_id,
        )

    def send_reaction(self, chat_id: str, message_id: str, emoji: str) -> dict:
        """React to a message with an emoji."""
        body = {
            "messaging_product": "whatsapp",
            "to": chat_id,
            "type": "reaction",
            "reaction": {"message_id": message_id, "emoji": emoji},
        }
        return self._request("POST", "messages", json=body)

    def mark_read(self, message_id: str) -> None:
        """Mark a message as read (shows blue checkmarks)."""
        self._request("POST", "messages", json={
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        })

    def _upload_media(self, file_path: str, mime_type: str) -> str:
        """Upload a file to WhatsApp media endpoint, return media_id."""
        with open(file_path, "rb") as f:
            resp = self._client.post(
                f"{self._base}/media",
                headers={"Authorization": f"Bearer {self._token}"},
                files={"file": (os.path.basename(file_path), f, mime_type)},
                data={"messaging_product": "whatsapp", "type": mime_type},
            )
        data = resp.json()
        if "error" in data:
            raise WhatsAppError(
                data["error"].get("message", "Upload failed"),
                data["error"].get("code", 0),
            )
        return data["id"]

    def download_file(self, media_id: str, dest_dir: str = "/tmp/pinky_files") -> str:
        """Download media by ID. First get URL, then download."""
        os.makedirs(dest_dir, exist_ok=True)
        # Step 1: Get media URL
        data = self._request("GET", f"/{media_id}")
        url = data.get("url", "")
        if not url:
            raise WhatsAppError("No download URL for media", 0)
        # Step 2: Download the file
        resp = self._client.get(url, headers={"Authorization": f"Bearer {self._token}"})
        if resp.status_code >= 400:
            message = f"Media download failed with HTTP {resp.status_code}"
            code = resp.status_code
            try:
                error = resp.json().get("error", {})
            except Exception:
                error = {}
            if error:
                message = error.get("message", message)
                code = error.get("code", code)
            raise WhatsAppError(message, code)
        ext = data.get("mime_type", "application/octet-stream").split("/")[-1].split(";")[0]
        path = os.path.join(dest_dir, f"wa_{media_id}.{ext}")
        with open(path, "wb") as f:
            f.write(resp.content)
        return path

    def get_me(self) -> dict:
        """Get phone number info to verify connection."""
        return self._request("GET", "")
