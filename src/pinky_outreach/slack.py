"""Slack adapter — Web API via httpx.

Uses the Slack Web API directly. Supports sending messages, fetching
conversation history, reactions, and file uploads.

Requires a Slack Bot Token (xoxb-...) with appropriate scopes:
- chat:write
- channels:history / groups:history / im:history
- reactions:write
- files:write (for uploads)
- channels:read (for channel info)
"""

from __future__ import annotations

from datetime import datetime, timezone

import httpx

from pinky_outreach.types import Chat, Message, Platform


class SlackError(Exception):
    """Slack API error."""

    def __init__(self, error: str):
        self.error = error
        super().__init__(f"Slack API error: {error}")


class SlackAdapter:
    """Slack Web API adapter using httpx."""

    BASE_URL = "https://slack.com/api"

    def __init__(self, bot_token: str, *, timeout: float = 30.0) -> None:
        self._token = bot_token
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bearer {bot_token}",
                "Content-Type": "application/json; charset=utf-8",
            },
            timeout=timeout,
        )
        self._bot_info: dict | None = None

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, **params) -> dict:
        """Make a Slack Web API request."""
        params = {k: v for k, v in params.items() if v is not None}
        resp = self._client.post(f"/{method}", json=params)
        data = resp.json()

        if not data.get("ok"):
            raise SlackError(data.get("error", "unknown_error"))

        return data

    # ── Sending ──────────────────────────────────────────────

    def send_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str = "",
        reply_broadcast: bool = False,
    ) -> Message:
        """Send a message to a Slack channel or thread.

        Args:
            channel: Channel ID (C...), DM ID (D...), or group ID (G...).
            text: Message text (supports Slack mrkdwn formatting).
            thread_ts: Thread timestamp to reply in-thread.
            reply_broadcast: Also post to channel when replying to thread.
        """
        result = self._request(
            "chat.postMessage",
            channel=channel,
            text=text,
            thread_ts=thread_ts or None,
            reply_broadcast=reply_broadcast if thread_ts else None,
        )

        msg_data = result.get("message", {})
        ts = msg_data.get("ts", "")

        return Message(
            platform=Platform.slack,
            chat_id=channel,
            sender="bot",
            content=text,
            timestamp=datetime.fromtimestamp(float(ts), tz=timezone.utc) if ts else datetime.now(timezone.utc),
            message_id=ts,
            is_outbound=True,
            metadata={"thread_ts": thread_ts} if thread_ts else {},
        )

    def upload_file(
        self,
        channel: str,
        file_path: str,
        *,
        title: str = "",
        initial_comment: str = "",
    ) -> Message:
        """Upload a file to a Slack channel.

        Uses the files.uploadV2 flow: get upload URL, upload, then complete.
        """
        import os
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)

        # Step 1: Get upload URL
        resp = self._client.post(
            "/files.getUploadURLExternal",
            json={"filename": filename, "length": file_size},
        )
        url_data = resp.json()
        if not url_data.get("ok"):
            raise SlackError(url_data.get("error", "upload_url_failed"))

        upload_url = url_data["upload_url"]
        file_id = url_data["file_id"]

        # Step 2: Upload the file
        with open(file_path, "rb") as f:
            upload_resp = httpx.post(upload_url, content=f.read())
            if upload_resp.status_code >= 400:
                raise SlackError(f"File upload failed: {upload_resp.status_code}")

        # Step 3: Complete the upload
        self._request(
            "files.completeUploadExternal",
            files=[{"id": file_id, "title": title or filename}],
            channel_id=channel,
            initial_comment=initial_comment or None,
        )

        return Message(
            platform=Platform.slack,
            chat_id=channel,
            sender="bot",
            content=initial_comment or f"[file: {filename}]",
            timestamp=datetime.now(timezone.utc),
            message_id=file_id,
            is_outbound=True,
            metadata={"type": "file", "filename": filename},
        )

    # ── Receiving ────────────────────────────────────────────

    def get_history(
        self,
        channel: str,
        *,
        limit: int = 50,
        oldest: str = "",
        latest: str = "",
    ) -> list[Message]:
        """Fetch conversation history from a channel.

        Args:
            channel: Channel ID.
            limit: Max messages (1-200).
            oldest: Only messages after this timestamp.
            latest: Only messages before this timestamp.
        """
        result = self._request(
            "conversations.history",
            channel=channel,
            limit=min(limit, 200),
            oldest=oldest or None,
            latest=latest or None,
        )

        messages = []
        for msg_data in result.get("messages", []):
            ts = msg_data.get("ts", "")
            user = msg_data.get("user", msg_data.get("bot_id", "unknown"))
            is_bot = "bot_id" in msg_data or msg_data.get("subtype") == "bot_message"

            messages.append(Message(
                platform=Platform.slack,
                chat_id=channel,
                sender=user,
                content=msg_data.get("text", ""),
                timestamp=datetime.fromtimestamp(float(ts), tz=timezone.utc) if ts else datetime.now(timezone.utc),
                message_id=ts,
                reply_to=msg_data.get("thread_ts", ""),
                is_outbound=is_bot,
                metadata={
                    "user_id": user,
                    "is_bot": is_bot,
                    "subtype": msg_data.get("subtype", ""),
                },
            ))

        return messages

    # ── Info ─────────────────────────────────────────────────

    def get_bot_info(self) -> dict:
        """Get the bot's identity."""
        if not self._bot_info:
            result = self._request("auth.test")
            self._bot_info = {
                "user_id": result.get("user_id"),
                "bot_id": result.get("bot_id"),
                "team_id": result.get("team_id"),
                "team": result.get("team"),
                "user": result.get("user"),
                "url": result.get("url"),
            }
        return self._bot_info

    def get_channel_info(self, channel: str) -> Chat:
        """Get channel information."""
        result = self._request("conversations.info", channel=channel)
        ch = result.get("channel", {})

        # Determine type
        if ch.get("is_im"):
            chat_type = "dm"
        elif ch.get("is_mpim"):
            chat_type = "group_dm"
        elif ch.get("is_private"):
            chat_type = "private"
        else:
            chat_type = "channel"

        return Chat(
            platform=Platform.slack,
            chat_id=channel,
            title=ch.get("name", ""),
            chat_type=chat_type,
        )

    # ── Reactions ────────────────────────────────────────────

    def add_reaction(
        self,
        channel: str,
        timestamp: str,
        emoji: str,
    ) -> bool:
        """Add a reaction to a message.

        Args:
            channel: Channel containing the message.
            timestamp: Message timestamp (ts).
            emoji: Reaction name without colons (e.g. "thumbsup", "heart").
        """
        self._request(
            "reactions.add",
            channel=channel,
            timestamp=timestamp,
            name=emoji,
        )
        return True

    def remove_reaction(
        self,
        channel: str,
        timestamp: str,
        emoji: str,
    ) -> bool:
        """Remove a reaction from a message."""
        self._request(
            "reactions.remove",
            channel=channel,
            timestamp=timestamp,
            name=emoji,
        )
        return True
