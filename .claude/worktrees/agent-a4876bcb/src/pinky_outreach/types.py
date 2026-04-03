"""Outreach types — shared data models for messaging."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum


class Platform(str, Enum):
    telegram = "telegram"
    discord = "discord"
    slack = "slack"
    imessage = "imessage"
    email = "email"


@dataclass
class Message:
    """A single message from any platform."""

    platform: Platform
    chat_id: str
    sender: str
    content: str
    timestamp: datetime
    message_id: str = ""
    reply_to: str = ""
    is_outbound: bool = False
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "sender": self.sender,
            "content": self.content,
            "timestamp": self.timestamp.isoformat(),
            "message_id": self.message_id,
            "reply_to": self.reply_to,
            "is_outbound": self.is_outbound,
        }


@dataclass
class Chat:
    """A chat/conversation on any platform."""

    platform: Platform
    chat_id: str
    title: str = ""
    chat_type: str = ""  # private, group, supergroup, channel
    username: str = ""

    def to_dict(self) -> dict:
        return {
            "platform": self.platform.value,
            "chat_id": self.chat_id,
            "title": self.title,
            "chat_type": self.chat_type,
            "username": self.username,
        }
