"""iMessage adapter — macOS-native via AppleScript + Messages.db.

Sending: Uses osascript to send via Messages.app.
Receiving: Polls ~/Library/Messages/chat.db (SQLite) for new messages.

Requirements:
- macOS with Messages.app signed into iMessage
- Full Disk Access granted to Python/Terminal for chat.db reading
"""

from __future__ import annotations

import os
import re
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from pinky_outreach.types import Chat, Message, Platform


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


class iMessageError(Exception):
    """iMessage operation error."""


class iMessageAdapter:
    """macOS iMessage adapter using AppleScript (send) + chat.db (receive)."""

    CHAT_DB = os.path.expanduser("~/Library/Messages/chat.db")

    # Apple's epoch: 2001-01-01 00:00:00 UTC
    APPLE_EPOCH_OFFSET = 978307200

    def __init__(self, *, db_path: str = "") -> None:
        self._db_path = db_path or self.CHAT_DB
        self._last_rowid: int = 0
        self._db: sqlite3.Connection | None = None
        self._db_available = False

        # Try to connect to chat.db
        self._init_db()

    def _init_db(self) -> None:
        """Initialize read-only connection to chat.db."""
        if not os.path.exists(self._db_path):
            _log(f"imessage: chat.db not found at {self._db_path}")
            return

        try:
            self._db = sqlite3.connect(
                f"file:{self._db_path}?mode=ro",
                uri=True,
                check_same_thread=False,
            )
            # Test read access
            self._db.execute("SELECT COUNT(*) FROM message").fetchone()
            self._db_available = True

            # Set last_rowid to current max so we only get new messages
            row = self._db.execute("SELECT MAX(ROWID) FROM message").fetchone()
            self._last_rowid = row[0] or 0
            _log(f"imessage: chat.db connected, last_rowid={self._last_rowid}")
        except (sqlite3.OperationalError, sqlite3.DatabaseError) as e:
            _log(f"imessage: chat.db not accessible (need Full Disk Access): {e}")
            self._db = None
            self._db_available = False

    def close(self) -> None:
        if self._db:
            self._db.close()
            self._db = None

    @property
    def can_receive(self) -> bool:
        """Whether we can poll for inbound messages."""
        return self._db_available

    # ── Sending ──────────────────────────────────────────────

    def send_message(
        self,
        chat_id: str,
        text: str,
        **kwargs,
    ) -> Message:
        """Send an iMessage via AppleScript.

        Args:
            chat_id: Phone number (+1...) or email address of recipient.
            text: Message text to send.
        """
        # Sanitize inputs for AppleScript
        safe_text = text.replace("\\", "\\\\").replace('"', '\\"')
        safe_id = chat_id.strip()

        # Use the buddy-based approach for phone/email
        script = f'''
tell application "Messages"
    set targetService to 1st account whose service type = iMessage
    set targetBuddy to participant "{safe_id}" of targetService
    send "{safe_text}" to targetBuddy
end tell
'''

        try:
            result = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                timeout=15,
            )

            if result.returncode != 0:
                error_msg = result.stderr.strip()
                # Try alternate approach using "buddy" instead of "participant"
                if "participant" in error_msg.lower() or "Can't get" in error_msg:
                    return self._send_via_buddy(safe_id, safe_text)
                raise iMessageError(f"AppleScript error: {error_msg}")

            _log(f"imessage: sent to {safe_id}")
            return Message(
                platform=Platform.imessage,
                chat_id=safe_id,
                sender="me",
                content=text,
                timestamp=datetime.now(timezone.utc),
                message_id=str(int(time.time() * 1000)),
                is_outbound=True,
            )

        except subprocess.TimeoutExpired:
            raise iMessageError("AppleScript timed out — is Messages.app running?")
        except FileNotFoundError:
            raise iMessageError("osascript not found — this only works on macOS")

    def _send_via_buddy(self, chat_id: str, safe_text: str) -> Message:
        """Fallback send using 'buddy' keyword."""
        script = f'''
tell application "Messages"
    set targetService to 1st account whose service type = iMessage
    set targetBuddy to buddy "{chat_id}" of targetService
    send "{safe_text}" to targetBuddy
end tell
'''
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=15,
        )

        if result.returncode != 0:
            raise iMessageError(f"AppleScript error: {result.stderr.strip()}")

        _log(f"imessage: sent to {chat_id} (buddy fallback)")
        return Message(
            platform=Platform.imessage,
            chat_id=chat_id,
            sender="me",
            content=safe_text.replace('\\"', '"').replace("\\\\", "\\"),
            timestamp=datetime.now(timezone.utc),
            message_id=str(int(time.time() * 1000)),
            is_outbound=True,
        )

    # ── Receiving ────────────────────────────────────────────

    def get_updates(self, limit: int = 50) -> list[Message]:
        """Get new inbound messages since last poll.

        Reads from ~/Library/Messages/chat.db. Requires Full Disk Access.
        Returns messages ordered by timestamp (oldest first).
        """
        if not self._db_available or not self._db:
            raise iMessageError(
                "chat.db not accessible. Grant Full Disk Access to Python "
                "in System Settings > Privacy & Security > Full Disk Access."
            )

        try:
            rows = self._db.execute(
                """
                SELECT
                    m.ROWID,
                    m.text,
                    m.date,
                    m.is_from_me,
                    m.date_read,
                    h.id AS handle_id,
                    c.chat_identifier,
                    c.display_name
                FROM message m
                LEFT JOIN handle h ON m.handle_id = h.ROWID
                LEFT JOIN chat_message_join cmj ON m.ROWID = cmj.message_id
                LEFT JOIN chat c ON cmj.chat_id = c.ROWID
                WHERE m.ROWID > ?
                  AND m.text IS NOT NULL
                  AND m.text != ''
                ORDER BY m.ROWID ASC
                LIMIT ?
                """,
                (self._last_rowid, limit),
            ).fetchall()
        except sqlite3.OperationalError as e:
            _log(f"imessage: chat.db query error: {e}")
            # Try reconnecting
            self._init_db()
            return []

        messages = []
        for row in rows:
            rowid, text, apple_date, is_from_me, date_read, handle_id, chat_id, display_name = row

            # Update tracking
            if rowid > self._last_rowid:
                self._last_rowid = rowid

            # Skip our own outbound messages
            if is_from_me:
                continue

            # Convert Apple timestamp (nanoseconds since 2001-01-01)
            ts = self._apple_ts_to_datetime(apple_date)

            # Use chat_identifier as chat_id, fall back to handle
            effective_chat_id = chat_id or handle_id or "unknown"
            sender = handle_id or "unknown"

            is_group = bool(chat_id and chat_id.startswith("chat"))

            messages.append(Message(
                platform=Platform.imessage,
                chat_id=str(effective_chat_id),
                sender=str(sender),
                content=text,
                timestamp=ts,
                message_id=str(rowid),
                is_outbound=False,
                metadata={
                    "display_name": display_name or "",
                    "handle_id": handle_id or "",
                    "is_group": is_group,
                    "chat_type": "group" if is_group else "private",
                },
            ))

        return messages

    def get_chat(self, chat_id: str) -> Chat:
        """Get info about a chat by identifier (phone/email or chat ID)."""
        if not self._db_available or not self._db:
            raise iMessageError("chat.db not accessible")

        row = self._db.execute(
            """
            SELECT chat_identifier, display_name, style
            FROM chat
            WHERE chat_identifier = ? OR chat_identifier LIKE ?
            LIMIT 1
            """,
            (chat_id, f"%{chat_id}%"),
        ).fetchone()

        if not row:
            return Chat(
                platform=Platform.imessage,
                chat_id=chat_id,
                title=chat_id,
                chat_type="private",
            )

        chat_identifier, display_name, style = row
        # style 43 = group, 45 = individual
        chat_type = "group" if style == 43 else "private"

        return Chat(
            platform=Platform.imessage,
            chat_id=chat_identifier,
            title=display_name or chat_identifier,
            chat_type=chat_type,
        )

    def get_recent_chats(self, limit: int = 20) -> list[Chat]:
        """List recent iMessage conversations."""
        if not self._db_available or not self._db:
            raise iMessageError("chat.db not accessible")

        rows = self._db.execute(
            """
            SELECT c.chat_identifier, c.display_name, c.style,
                   MAX(m.date) as last_msg
            FROM chat c
            LEFT JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
            LEFT JOIN message m ON cmj.message_id = m.ROWID
            GROUP BY c.ROWID
            ORDER BY last_msg DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        chats = []
        for chat_id, display_name, style, _ in rows:
            chat_type = "group" if style == 43 else "private"
            chats.append(Chat(
                platform=Platform.imessage,
                chat_id=chat_id,
                title=display_name or chat_id,
                chat_type=chat_type,
            ))
        return chats

    # ── Helpers ──────────────────────────────────────────────

    def _apple_ts_to_datetime(self, apple_date: int | None) -> datetime:
        """Convert Apple's CoreData timestamp to datetime.

        Apple timestamps are nanoseconds since 2001-01-01.
        Some older messages use seconds instead.
        """
        if not apple_date:
            return datetime.now(timezone.utc)

        # Newer macOS uses nanoseconds (>= 10^18 range)
        if apple_date > 1_000_000_000_000:
            unix_ts = (apple_date / 1_000_000_000) + self.APPLE_EPOCH_OFFSET
        else:
            unix_ts = apple_date + self.APPLE_EPOCH_OFFSET

        try:
            return datetime.fromtimestamp(unix_ts, tz=timezone.utc)
        except (ValueError, OSError):
            return datetime.now(timezone.utc)
