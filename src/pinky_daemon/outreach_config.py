"""Outreach configuration store — SQLite-backed platform management.

Manage platform configurations (tokens, settings, enabled state)
via API instead of editing YAML files. Tokens are stored in the DB
and can be rotated at runtime.

Storage: SQLite with one table:
  - platforms: platform configs (name, token, enabled, settings)
"""

from __future__ import annotations

import json
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


SUPPORTED_PLATFORMS = ("telegram", "discord", "slack")


@dataclass
class PlatformConfig:
    """Configuration for a messaging platform."""

    platform: str
    enabled: bool = False
    token_set: bool = False  # Whether a token is configured (never expose the actual token)
    settings: dict = field(default_factory=dict)
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_dict(self) -> dict:
        return {
            "platform": self.platform,
            "enabled": self.enabled,
            "token_set": self.token_set,
            "settings": self.settings,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }


class OutreachConfigStore:
    """SQLite-backed outreach platform configuration."""

    def __init__(self, db_path: str = "data/outreach_config.db") -> None:
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(db_path, check_same_thread=False)
        self._db.execute("PRAGMA journal_mode=WAL")
        self._init_tables()

    def _init_tables(self) -> None:
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS platforms (
                platform TEXT PRIMARY KEY,
                token TEXT NOT NULL DEFAULT '',
                enabled INTEGER NOT NULL DEFAULT 0,
                settings TEXT NOT NULL DEFAULT '{}',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL
            );
        """)
        self._db.commit()

    def configure(
        self,
        platform: str,
        *,
        token: str = "",
        enabled: bool | None = None,
        settings: dict | None = None,
    ) -> PlatformConfig:
        """Configure or update a platform.

        Only updates fields that are explicitly provided.
        """
        if platform not in SUPPORTED_PLATFORMS:
            raise ValueError(f"Unsupported platform '{platform}'. Supported: {', '.join(SUPPORTED_PLATFORMS)}")

        now = time.time()
        existing = self.get(platform)

        if existing:
            updates = []
            params: list = []

            if token:
                updates.append("token=?")
                params.append(token)
            if enabled is not None:
                updates.append("enabled=?")
                params.append(int(enabled))
            if settings is not None:
                updates.append("settings=?")
                params.append(json.dumps(settings))

            if updates:
                updates.append("updated_at=?")
                params.append(now)
                params.append(platform)
                self._db.execute(
                    f"UPDATE platforms SET {', '.join(updates)} WHERE platform=?",
                    params,
                )
                self._db.commit()
        else:
            self._db.execute(
                """INSERT INTO platforms (platform, token, enabled, settings, created_at, updated_at)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (platform, token, int(enabled if enabled is not None else bool(token)),
                 json.dumps(settings or {}), now, now),
            )
            self._db.commit()

        _log(f"outreach config: {'updated' if existing else 'configured'} {platform}")
        return self.get(platform)  # type: ignore

    def get(self, platform: str) -> PlatformConfig | None:
        """Get platform configuration (token is never exposed)."""
        row = self._db.execute(
            "SELECT platform, token, enabled, settings, created_at, updated_at FROM platforms WHERE platform=?",
            (platform,),
        ).fetchone()
        if not row:
            return None
        return PlatformConfig(
            platform=row[0],
            enabled=bool(row[2]),
            token_set=bool(row[1]),
            settings=json.loads(row[3]),
            created_at=row[4],
            updated_at=row[5],
        )

    def get_token(self, platform: str) -> str:
        """Get the raw token for a platform (internal use only)."""
        row = self._db.execute(
            "SELECT token FROM platforms WHERE platform=?",
            (platform,),
        ).fetchone()
        return row[0] if row else ""

    def list(self) -> list[PlatformConfig]:
        """List all configured platforms."""
        rows = self._db.execute(
            "SELECT platform, token, enabled, settings, created_at, updated_at FROM platforms ORDER BY platform"
        ).fetchall()
        return [
            PlatformConfig(
                platform=r[0], enabled=bool(r[2]), token_set=bool(r[1]),
                settings=json.loads(r[3]), created_at=r[4], updated_at=r[5],
            )
            for r in rows
        ]

    def enable(self, platform: str) -> bool:
        """Enable a platform."""
        return self._set_enabled(platform, True)

    def disable(self, platform: str) -> bool:
        """Disable a platform."""
        return self._set_enabled(platform, False)

    def _set_enabled(self, platform: str, enabled: bool) -> bool:
        cursor = self._db.execute(
            "UPDATE platforms SET enabled=?, updated_at=? WHERE platform=?",
            (int(enabled), time.time(), platform),
        )
        self._db.commit()
        return cursor.rowcount > 0

    def delete(self, platform: str) -> bool:
        """Remove a platform configuration entirely."""
        cursor = self._db.execute("DELETE FROM platforms WHERE platform=?", (platform,))
        self._db.commit()
        if cursor.rowcount > 0:
            _log(f"outreach config: deleted {platform}")
            return True
        return False

    def test_connection(self, platform: str) -> dict:
        """Test connectivity to a platform.

        Returns a status dict with success/error info.
        """
        config = self.get(platform)
        if not config:
            return {"platform": platform, "success": False, "error": "Not configured"}

        if not config.token_set:
            return {"platform": platform, "success": False, "error": "No token configured"}

        token = self.get_token(platform)

        if platform == "telegram":
            return self._test_telegram(token)
        elif platform == "discord":
            return self._test_discord(token)
        elif platform == "slack":
            return self._test_slack(token)
        else:
            return {"platform": platform, "success": False, "error": f"Unknown platform: {platform}"}

    def _test_telegram(self, token: str) -> dict:
        """Test Telegram bot token via getMe API."""
        try:
            from pinky_outreach.telegram import TelegramAdapter
            adapter = TelegramAdapter(token)
            info = adapter.get_me()
            return {
                "platform": "telegram",
                "success": True,
                "bot_username": info.get("username", ""),
                "bot_id": info.get("id"),
            }
        except Exception as e:
            return {"platform": "telegram", "success": False, "error": str(e)}

    def _test_discord(self, token: str) -> dict:
        """Test Discord bot token via getCurrentUser."""
        try:
            from pinky_outreach.discord import DiscordAdapter
            adapter = DiscordAdapter(token)
            info = adapter.get_me()
            return {
                "platform": "discord",
                "success": True,
                "bot_username": info.get("username", ""),
                "bot_id": info.get("id"),
            }
        except Exception as e:
            return {"platform": "discord", "success": False, "error": str(e)}

    def _test_slack(self, token: str) -> dict:
        """Test Slack bot token via auth.test."""
        try:
            from pinky_outreach.slack import SlackAdapter
            adapter = SlackAdapter(token)
            info = adapter.get_bot_info()
            return {
                "platform": "slack",
                "success": True,
                "bot_user": info.get("user", ""),
                "team": info.get("team", ""),
            }
        except Exception as e:
            return {"platform": "slack", "success": False, "error": str(e)}

    def close(self) -> None:
        self._db.close()
