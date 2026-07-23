"""Slack adapter — Web API via httpx.

Uses the Slack Web API directly. Supports sending messages, fetching
conversation history, reactions, and file uploads.

Requires a Slack Bot Token (xoxb-...) with appropriate scopes:
- chat:write
- channels:history / groups:history / im:history
- reactions:write
- files:write (for uploads)
- im:write (resolve U-prefixed user IDs to DM conversations for file uploads)
- channels:read (for channel info)
- users:read (resolve user id -> display name)
- users:read.email (resolve user id -> email; email lookup-by-email routing)
"""

from __future__ import annotations

import json
import os
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
        self._dm_conversation_ids: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    @property
    def bot_token(self) -> str:
        """The bot token (xoxb-) — e.g. to build a Socket Mode AsyncWebClient."""
        return self._token

    def _request(self, method: str, **params) -> dict:
        """Make a Slack Web API request (JSON body).

        Fine for write methods like chat.postMessage that accept a JSON body.
        Read/info methods (users.info, conversations.info, users.lookupByEmail)
        must use ``_request_form`` instead — see that method's docstring.
        """
        params = {k: v for k, v in params.items() if v is not None}
        resp = self._client.post(f"/{method}", json=params)
        data = resp.json()

        if not data.get("ok"):
            raise SlackError(data.get("error", "unknown_error"))

        return data

    def _request_form(self, method: str, **params) -> dict:
        """Make a Slack Web API request with form-encoded args.

        Required for Slack's read/info methods (users.info, conversations.info,
        users.lookupByEmail): those ignore a JSON request body, so a JSON call
        arrives argument-less and Slack returns ``invalid_arguments`` or
        ``*_not_found`` even with a valid token and scopes. They only read args
        from the query string or an ``application/x-www-form-urlencoded`` body.
        The client's default Content-Type is JSON, so override it per-request.
        """
        params = {k: v for k, v in params.items() if v is not None}
        resp = self._client.post(
            f"/{method}",
            data=params,
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        data = resp.json()

        if not data.get("ok"):
            raise SlackError(data.get("error", "unknown_error"))

        return data

    # ── Actions ──────────────────────────────────────────────

    def send_typing(self, channel: str) -> None:
        """Send a typing indicator to a Slack channel (requires chat:write)."""
        # Slack doesn't have a dedicated typing API for bots in the same way,
        # but we can approximate with a no-op. Slack shows typing automatically
        # when using the Events API with socket mode. For REST bots, there's no
        # standard typing indicator endpoint.
        pass

    # ── Sending ──────────────────────────────────────────────

    def send_message(
        self,
        channel: str,
        text: str,
        *,
        thread_ts: str = "",
        reply_broadcast: bool = False,
        blocks: list | None = None,
        unfurl_links: bool | None = None,
        unfurl_media: bool | None = None,
    ) -> Message:
        """Send a message to a Slack channel or thread.

        Args:
            channel: Channel ID (C...), DM ID (D...), or group ID (G...).
            text: Message text (supports Slack mrkdwn formatting). When ``blocks``
                are present this is the notification fallback text.
            thread_ts: Thread timestamp to reply in-thread.
            reply_broadcast: Also post to channel when replying to thread.
            blocks: optional Block Kit blocks (list of block dicts) for rich/
                interactive messages. Sent via the JSON body (chat.postMessage);
                ``_request`` drops it when None.
            unfurl_links / unfurl_media: Slack's link-preview controls
                (text-content unfurls / media unfurls). None leaves Slack's
                defaults untouched; pass False to suppress preview cards.
        """
        result = self._request(
            "chat.postMessage",
            channel=channel,
            text=text,
            thread_ts=thread_ts or None,
            reply_broadcast=reply_broadcast if thread_ts else None,
            blocks=blocks or None,
            unfurl_links=unfurl_links,
            unfurl_media=unfurl_media,
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

    def respond_via_url(self, response_url: str, payload: dict) -> None:
        """POST to a Slack interactive ``response_url`` to update/replace a message.

        ``response_url`` is a short-lived (30-min, 5-use) capability URL Slack
        delivers WITH an interaction payload (e.g. a block_actions button click).
        It needs no bot token and no extra scope, and is the simplest way to
        update the source message after a click (vs chat.update). ``payload`` is
        a Slack message-response dict, e.g.
        ``{"replace_original": True, "text": ..., "blocks": [...]}`` to replace
        the card, or ``{"response_type": "ephemeral", "text": ...}`` to show the
        clicker a private note without touching the card.
        """
        if not response_url:
            return
        # No auth header — the URL itself is the capability. Don't reuse the
        # bot-token client; post directly.
        resp = httpx.post(response_url, json=payload, timeout=10.0)
        if resp.status_code >= 400:
            raise SlackError(f"response_url post failed: {resp.status_code}")

    def _ensure_conversation_id(self, channel: str) -> str:
        """Resolve a U-prefixed user ID to the D-prefixed DM conversation ID.

        ``chat.postMessage`` accepts a user ID and opens the DM implicitly, but
        ``files.completeUploadExternal(channel_id=...)`` requires a conversation
        ID. Resolve before starting the external upload so a missing ``im:write``
        scope fails without creating an upload ticket or transferring bytes.
        Successful U→D mappings are stable and cached for this adapter/token.
        Existing C/D/G conversation IDs pass through without an API call.
        """
        if not channel.startswith("U"):
            return channel

        cached = self._dm_conversation_ids.get(channel)
        if cached:
            return cached

        try:
            result = self._request_form("conversations.open", users=channel)
        except SlackError as exc:
            if exc.error == "missing_scope":
                raise SlackError(
                    "im:write is required to resolve U-prefixed user IDs "
                    "for Slack DM file uploads"
                ) from exc
            raise

        conversation_id = str((result.get("channel") or {}).get("id") or "")
        if not conversation_id.startswith("D"):
            raise SlackError(
                "conversations.open returned no valid DM conversation ID"
            )
        self._dm_conversation_ids[channel] = conversation_id
        return conversation_id

    def upload_file(
        self,
        channel: str,
        file_path: str,
        *,
        title: str = "",
        initial_comment: str = "",
        thread_ts: str = "",
    ) -> Message:
        """Upload a file to a Slack channel.

        Uses the files.uploadV2 flow: get upload URL, upload, then complete.

        ``thread_ts`` threads the file under an existing message (a Slack reply);
        empty posts it as a new root. Mirrors ``send_message``'s threading so
        send_photo/send_document/send_video with a ``message_id`` reply in-thread
        instead of spawning a new root message.
        """
        import os
        filename = os.path.basename(file_path)
        file_size = os.path.getsize(file_path)
        conversation_id = self._ensure_conversation_id(channel)

        # Step 1: Get an upload URL. files.getUploadURLExternal is a form/query
        # method — like users.info / conversations.info it ignores a JSON request
        # body and returns invalid_arguments, so it must be form-encoded (same
        # root cause as #808). Sending JSON here was why uploads failed even on a
        # minimal file.
        url_data = self._request_form(
            "files.getUploadURLExternal",
            filename=filename,
            length=str(file_size),
        )
        upload_url = url_data["upload_url"]
        file_id = url_data["file_id"]

        # Step 2: Upload the file (stream from disk; match the client's 30s
        # timeout instead of httpx's 5s module-level default)
        with open(file_path, "rb") as f:
            try:
                upload_resp = httpx.post(upload_url, content=f, timeout=30.0)
            except httpx.HTTPError as e:
                raise SlackError(f"File upload failed: {e}") from e
            if upload_resp.status_code >= 400:
                raise SlackError(f"File upload failed: {upload_resp.status_code}")

        # Step 3: Complete the upload. files.completeUploadExternal also takes
        # form-encoded args; `files` is a JSON-encoded array string (the
        # canonical uploadV2 shape), not a native JSON-body field.
        self._request_form(
            "files.completeUploadExternal",
            files=json.dumps([{"id": file_id, "title": title or filename}]),
            channel_id=conversation_id,
            initial_comment=initial_comment or None,
            thread_ts=thread_ts or None,
        )

        return Message(
            platform=Platform.slack,
            chat_id=channel,
            sender="bot",
            content=initial_comment or f"[file: {filename}]",
            timestamp=datetime.now(timezone.utc),
            message_id=file_id,
            is_outbound=True,
            metadata={"type": "file", "filename": filename,
                      **({"thread_ts": thread_ts} if thread_ts else {})},
        )

    # ── Downloading ──────────────────────────────────────────

    def download_file(self, url: str, dest_dir: str = "/tmp/pinky_files") -> str:
        """Download a file from Slack. Returns local path.

        Uses the bot token for authorization on private URLs.
        """
        os.makedirs(dest_dir, exist_ok=True)

        # Slack private URLs need the Authorization header
        resp = self._client.get(url)
        if resp.status_code >= 400:
            raise SlackError(f"File download failed: {resp.status_code}")

        # Extract filename from URL, fall back to generic name
        filename = url.rsplit("/", 1)[-1].split("?")[0] if "/" in url else "file"
        local_path = os.path.join(dest_dir, filename)

        with open(local_path, "wb") as f:
            f.write(resp.content)

        return local_path

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

            metadata = {
                "user_id": user,
                "is_bot": is_bot,
                "subtype": msg_data.get("subtype", ""),
            }

            # Detect file attachments
            raw_files = msg_data.get("files", [])
            if raw_files:
                metadata["attachments"] = [
                    {
                        "type": "file",
                        "file_id": f.get("id", ""),
                        "file_name": f.get("name", ""),
                        "url": f.get("url_private_download", ""),
                        "mime_type": f.get("mimetype", ""),
                        "file_size": f.get("size", 0),
                    }
                    for f in raw_files
                ]

            messages.append(Message(
                platform=Platform.slack,
                chat_id=channel,
                sender=user,
                content=msg_data.get("text", ""),
                timestamp=datetime.fromtimestamp(float(ts), tz=timezone.utc) if ts else datetime.now(timezone.utc),
                message_id=ts,
                reply_to=msg_data.get("thread_ts", ""),
                is_outbound=is_bot,
                metadata=metadata,
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
        result = self._request_form("conversations.info", channel=channel)
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

    def get_user_info(self, user_id: str) -> dict:
        """Resolve a Slack user id (U...) to profile info.

        Requires the ``users:read`` scope; ``email`` is only populated when
        ``users:read.email`` is also granted (else it's an empty string).
        Raises SlackError on API failure (e.g. ``missing_scope``,
        ``user_not_found``) — callers should treat resolution as best-effort.

        Returns a dict: user_id, name (handle), real_name, display_name
        (best human label, never empty when the user resolves), email, is_bot.
        """
        result = self._request_form("users.info", user=user_id)
        u = result.get("user", {}) or {}
        prof = u.get("profile", {}) or {}
        display = (
            prof.get("display_name")
            or prof.get("real_name")
            or u.get("real_name")
            or u.get("name")
            or user_id
        )
        return {
            "user_id": user_id,
            "name": u.get("name", ""),
            "real_name": u.get("real_name", "") or prof.get("real_name", ""),
            "display_name": display,
            "email": prof.get("email", ""),
            "is_bot": bool(u.get("is_bot", False)),
        }

    def lookup_user_by_email(self, email: str) -> dict:
        """Find a Slack user by email (requires ``users:read.email``).

        Returns the same shape as ``get_user_info``. Raises SlackError on
        failure (notably ``users_not_found`` when no member matches) — the
        natural tool for routing a nudge to the right person by their Zoho/
        directory email.
        """
        result = self._request_form("users.lookupByEmail", email=email)
        u = result.get("user", {}) or {}
        prof = u.get("profile", {}) or {}
        display = (
            prof.get("display_name")
            or prof.get("real_name")
            or u.get("real_name")
            or u.get("name")
            or u.get("id", "")
        )
        return {
            "user_id": u.get("id", ""),
            "name": u.get("name", ""),
            "real_name": u.get("real_name", "") or prof.get("real_name", ""),
            "display_name": display,
            "email": prof.get("email", "") or email,
            "is_bot": bool(u.get("is_bot", False)),
        }

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
