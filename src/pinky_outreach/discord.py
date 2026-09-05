"""Discord adapter — Bot API via httpx.

Uses the Discord REST API directly for sending. For receiving,
uses the Gateway (WebSocket) for real-time events, or polling
via REST for simpler setups.

This adapter uses REST-only for v0.1 — no WebSocket gateway.
Suitable for outbound messaging and periodic message checking.
"""

from __future__ import annotations

import os
import time
from datetime import datetime

import httpx

from pinky_outreach.types import Chat, Message, Platform

# Discord requires a User-Agent that follows the convention; default urllib/httpx
# UAs trigger Cloudflare 403s on some endpoints. See
# https://discord.com/developers/docs/reference#user-agent
_DEFAULT_USER_AGENT = "DiscordBot (https://github.com/olegbrok/PinkyBot, 0.1.0)"


class DiscordError(Exception):
    """Discord API error."""

    def __init__(self, message: str, status_code: int = 0):
        self.status_code = status_code
        super().__init__(f"Discord API error {status_code}: {message}")


class DiscordRateLimited(DiscordError):  # noqa: N818 — name reads naturally at call sites
    """Discord 429 — caller may retry after `retry_after` seconds."""

    def __init__(self, retry_after: float, message: str = "rate limited"):
        self.retry_after = retry_after
        super().__init__(message, status_code=429)


class DiscordAdapter:
    """Discord REST API adapter using httpx.

    Synchronous: uses `httpx.Client` and `time.sleep` for 429 backoff. Callers
    in async contexts must wrap calls in `loop.run_in_executor` to avoid
    blocking the event loop. The bundled `BrokerDiscordPoller` does this
    correctly; ad-hoc async callers must too. A future v0.2 may swap to
    `httpx.AsyncClient` + `asyncio.sleep` to remove the requirement.
    """

    BASE_URL = "https://discord.com/api/v10"

    def __init__(
        self,
        bot_token: str,
        *,
        timeout: float = 30.0,
        user_agent: str = _DEFAULT_USER_AGENT,
        max_429_retries: int = 1,
    ) -> None:
        self._token = bot_token
        self._max_429_retries = max_429_retries
        self._client = httpx.Client(
            base_url=self.BASE_URL,
            headers={
                "Authorization": f"Bot {bot_token}",
                "User-Agent": user_agent,
            },
            timeout=timeout,
        )
        self._bot_user: dict | None = None
        # Cache user_id → DM channel_id resolutions so we only call
        # POST /users/@me/channels once per recipient.
        self._dm_channel_cache: dict[str, str] = {}

    def close(self) -> None:
        self._client.close()

    def _request(self, method: str, path: str, **kwargs) -> dict | list:
        """Make a Discord API request, with bounded 429 retry.

        Synchronous: blocks via `time.sleep` on 429 backoff. Async callers
        must wrap in `run_in_executor` (see class docstring).
        """
        attempts = 0
        while True:
            resp = self._client.request(method, path, **kwargs)

            if resp.status_code == 429:
                # Discord returns retry_after either as a JSON body field (seconds)
                # or in the X-RateLimit-Reset-After header. Prefer the body.
                retry_after = 1.0
                try:
                    body = resp.json()
                    retry_after = float(body.get("retry_after", retry_after))
                except Exception:
                    header = resp.headers.get("X-RateLimit-Reset-After")
                    if header:
                        try:
                            retry_after = float(header)
                        except ValueError:
                            pass

                if attempts >= self._max_429_retries:
                    raise DiscordRateLimited(retry_after)
                attempts += 1
                time.sleep(min(retry_after, 5.0))
                continue

            if resp.status_code == 204:
                return {}

            try:
                data = resp.json()
            except Exception:
                if resp.status_code >= 400:
                    raise DiscordError(resp.text[:200], resp.status_code)
                return {}

            if resp.status_code >= 400:
                msg = data.get("message", str(data)) if isinstance(data, dict) else str(data)
                raise DiscordError(msg, resp.status_code)

            return data

    # ── Actions ──────────────────────────────────────────────

    def send_typing(self, channel_id: str) -> None:
        """Send a typing indicator to a Discord channel."""
        self._request("POST", f"/channels/{channel_id}/typing")

    # ── Sending ──────────────────────────────────────────────

    def open_dm_channel(self, user_id: str) -> str:
        """Open (or look up cached) a DM channel with a user. Returns channel_id.

        Discord requires explicitly opening a DM via POST /users/@me/channels
        with `{recipient_id: user_id}`. The result is a channel_id that can
        then be used with send_message / etc. The mapping is stable per bot,
        so we cache it.
        """
        cached = self._dm_channel_cache.get(user_id)
        if cached:
            return cached
        result = self._request(
            "POST", "/users/@me/channels", json={"recipient_id": user_id},
        )
        channel_id = result["id"]
        self._dm_channel_cache[user_id] = channel_id
        return channel_id

    def send_message(
        self,
        channel_id: str,
        content: str,
        *,
        reply_to: str = "",
    ) -> Message:
        """Send a message to a Discord channel.

        If `channel_id` looks like a user_id (Discord returns 404 Unknown
        Channel), transparently opens a DM channel with that user and
        retries — this lets callers pass user_ids for owner-notify flows
        where they only know the recipient's user_id.
        """
        payload: dict = {"content": content}
        if reply_to:
            payload["message_reference"] = {"message_id": reply_to}

        try:
            result = self._request(
                "POST", f"/channels/{channel_id}/messages", json=payload,
            )
        except DiscordError as e:
            if e.status_code == 404 and "Unknown Channel" in str(e):
                # Treat channel_id as a user_id and try DMing them.
                # Bounded — open_dm_channel will surface its own error
                # if user_id is also invalid; we don't loop again.
                dm_channel_id = self.open_dm_channel(channel_id)
                result = self._request(
                    "POST", f"/channels/{dm_channel_id}/messages", json=payload,
                )
                channel_id = dm_channel_id
            else:
                raise

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

    def get_me(self, *, http_timeout: float | None = None) -> dict:
        """Get the bot's user info."""
        if not self._bot_user:
            options = {"timeout": http_timeout} if http_timeout is not None else {}
            identity = self._request("GET", "/users/@me", **options)
            # Let the poller classify malformed responses as failed probes;
            # caching one would prevent subsequent probes from recovering.
            if isinstance(identity, dict) and "id" in identity:
                self._bot_user = identity
            return identity
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

    def get_my_guilds(self) -> list[dict]:
        """List guilds (servers) the bot is currently a member of.

        Returns minimal guild dicts: {id, name, owner, permissions, ...}.
        Used by the inbound poller to auto-discover channels to watch.
        """
        result = self._request("GET", "/users/@me/guilds")
        return result if isinstance(result, list) else []

    def discover_text_channels(self) -> list[str]:
        """Auto-discover all text channels (type 0) the bot can access across all guilds.

        Returns a list of channel IDs. Used as a default watch-set when no
        explicit channel list is configured in the bot token settings.
        """
        # Discord channel types: 0=GUILD_TEXT, 5=GUILD_ANNOUNCEMENT.
        # We treat both as message-bearing for inbound polling purposes.
        watchable_types = {0, 5}
        channel_ids: list[str] = []
        for guild in self.get_my_guilds():
            try:
                raw = self._request("GET", f"/guilds/{guild['id']}/channels")
            except DiscordError:
                # Skip guilds where listing fails (e.g., transient permission issues).
                continue
            if not isinstance(raw, list):
                continue
            for ch in raw:
                if ch.get("type") in watchable_types:
                    channel_ids.append(ch["id"])
        return channel_ids

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
