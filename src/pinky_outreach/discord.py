"""Discord adapter — Bot API via httpx.

Uses the Discord REST API directly for sending. For receiving,
uses the Gateway (WebSocket) for real-time events, or polling
via REST for simpler setups.

This adapter uses REST-only for v0.1 — no WebSocket gateway.
Suitable for outbound messaging and periodic message checking.
"""

from __future__ import annotations

import os
from datetime import datetime, timezone

import httpx

from pinky_outreach.types import Chat, Message, Platform


class DiscordError(Exception):
    """Discord API error."""

    def __init__(self, message: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(f"Discord API error {status_code}: {message}")


class DiscordAdapter:
    """Discord REST API adapter using httpx."""

    BASE_URL = "https://discord.com/api/v10"

    def __init__(self, bot_token: str, *, timeout: float = 30.0) -> None:
        self._token = bot_token
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bot {bot_token}",
                "Content-Type": "application/json",
            },
            timeout=timeout,
        )
        self._bot_user: dict | None = None

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        """Make a Discord API request."""
        resp = self._client.request(method, path, **kwargs)

        if resp.status_code == 204:
            return {}

        data = resp.json()

        if resp.status_code >= 400:
            msg = data.get("message", str(data))
            raise DiscordError(msg, resp.status_code)

        return data

    # ── Sending ──────────────────────────────────────────────

    def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        reply_to: str = "",
    ) -> Message:
        """Send a message to a Discord channel."""
        payload: dict = {"content": content}
        if reply_to:
            payload["message_reference"] = {"message_id": reply_to}

        result = self._request("POST", f"/channels/{channel_id}/messages", json=payload)

        return Message(
            platform=Platform.discord,
            chat_id=channel_id,
            sender="bot",
            content=content,
            timestamp=datetime.fromisoformat(result["timestamp"]),
            message_id=result["id"],
            is_outbound=True,
        )

    def send_file(
        self,
        channel_id: str,
        file_path: str,
        *,
        content: str = "",
    ) -> Message:
        """Send a file to a Discord channel."""
        with open(file_path, "rb") as f:
            filename = file_path.rsplit("/", 1)[-1]
            # Use multipart for file upload
            resp = self._client.post(
                f"/channels/{channel_id}/messages",
                data={"content": content} if content else {},
                files={"files[0]": (filename, f)},
                headers={"Content-Type": None},  # Let httpx set multipart headers
            )

        if resp.status_code >= 400:
            data = resp.json()
            raise DiscordError(data.get("message", str(data)), resp.status_code)

        result = resp.json()
        return Message(
            platform=Platform.discord,
            chat_id=channel_id,
            sender="bot",
            content=content,
            timestamp=datetime.fromisoformat(result["timestamp"]),
            message_id=result["id"],
            is_outbound=True,
            metadata={"type": "file"},
        )

    # ── Downloading ──────────────────────────────────────────

    def download_file(self, url: str, dest_dir: str = "/tmp/pinky_files") -> str:
        """Download a file from a Discord CDN URL. Returns local path."""
        os.makedirs(dest_dir, exist_ok=True)

        resp = self._client.get(url)
        if resp.status_code >= 400:
            raise DiscordError(f"File download failed: {resp.status_code}", resp.status_code)

        # Extract filename from URL, fall back to generic name
        filename = url.rsplit("/", 1)[-1].split("?")[0] if "/" in url else "file"
        local_path = os.path.join(dest_dir, filename)

        with open(local_path, "wb") as f:
            f.write(resp.content)

        return local_path

    # ── Receiving ────────────────────────────────────────────

    def get_messages(
        self,
        channel_id: str,
        *,
        limit: int = 50,
        after: str = "",
        before: str = "",
    ) -> list[Message]:
        """Fetch recent messages from a channel.

        Args:
            channel_id: Channel to fetch from.
            limit: Max messages (1-100).
            after: Only messages after this message ID.
            before: Only messages before this message ID.
        """
        params: dict = {"limit": min(limit, 100)}
        if after:
            params["after"] = after
        if before:
            params["before"] = before

        results = self._request("GET", f"/channels/{channel_id}/messages", params=params)

        messages = []
        for msg_data in results:
            author = msg_data.get("author", {})
            is_bot = author.get("bot", False)

            ref = msg_data.get("message_reference", {})
            reply_to_id = ref.get("message_id", "") if ref else ""

            metadata = {
                "author_id": author.get("id", ""),
                "discriminator": author.get("discriminator", ""),
                "is_bot": is_bot,
            }

            # Detect file attachments
            raw_attachments = msg_data.get("attachments", [])
            if raw_attachments:
                metadata["attachments"] = [
                    {
                        "type": "file",
                        "file_id": att["id"],
                        "file_name": att.get("filename", ""),
                        "url": att.get("url", ""),
                        "mime_type": att.get("content_type", ""),
                        "file_size": att.get("size", 0),
                    }
                    for att in raw_attachments
                ]

            messages.append(Message(
                platform=Platform.discord,
                chat_id=channel_id,
                sender=author.get("username", "unknown"),
                content=msg_data.get("content", ""),
                timestamp=datetime.fromisoformat(msg_data["timestamp"]),
                message_id=msg_data["id"],
                reply_to=reply_to_id,
                is_outbound=is_bot,
                metadata=metadata,
            ))

        return messages

    # ── Info ─────────────────────────────────────────────────

    def get_me(self) -> dict:
        """Get the bot's user info."""
        if not self._bot_user:
            self._bot_user = self._request("GET", "/users/@me")
        return self._bot_user

    def get_channel(self, channel_id: str) -> Chat:
        """Get channel info."""
        result = self._request("GET", f"/channels/{channel_id}")

        # Determine chat type
        type_map = {0: "text", 1: "dm", 2: "voice", 4: "category", 5: "announcement", 13: "stage"}
        chat_type = type_map.get(result.get("type", 0), "unknown")

        return Chat(
            platform=Platform.discord,
            chat_id=channel_id,
            title=result.get("name", ""),
            chat_type=chat_type,
        )

    def get_guild_channels(self, guild_id: str) -> list[Chat]:
        """List all channels in a guild/server."""
        results = self._request("GET", f"/guilds/{guild_id}/channels")
        return [
            Chat(
                platform=Platform.discord,
                chat_id=ch["id"],
                title=ch.get("name", ""),
                chat_type=str(ch.get("type", 0)),
            )
            for ch in results
        ]

    # ── Reactions ────────────────────────────────────────────

    def add_reaction(
        self,
        channel_id: str,
        message_id: str,
        emoji: str,
    ) -> bool:
        """Add a reaction to a message.

        Args:
            channel_id: Channel containing the message.
            message_id: Message to react to.
            emoji: Unicode emoji (e.g. "👍") or custom format "name:id".
        """
        # URL-encode the emoji
        import urllib.parse
        encoded = urllib.parse.quote(emoji)

        self._request(
            "PUT",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        )
        return True

    def remove_reaction(
        self,
        channel_id: str,
        message_id: str,
        emoji: str,
    ) -> bool:
        """Remove the bot's reaction from a message."""
        import urllib.parse
        encoded = urllib.parse.quote(emoji)

        self._request(
            "DELETE",
            f"/channels/{channel_id}/messages/{message_id}/reactions/{encoded}/@me",
        )
        return True
