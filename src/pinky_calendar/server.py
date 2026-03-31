"""Pinky Calendar — MCP server exposing calendar tools to agents.

Usage:
    python -m pinky_calendar --agent barsik --api-url http://localhost:8888
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

from mcp.server.fastmcp import FastMCP


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def create_server(
    *,
    agent_name: str = "",
    api_url: str = "http://localhost:8888",
    host: str = "127.0.0.1",
    port: int = 8104,
) -> FastMCP:
    """Create the pinky-calendar MCP server."""

    mcp = FastMCP("pinky-calendar", host=host, port=port)

    def _api(method: str, path: str, body: dict | None = None) -> dict:
        url = f"{api_url}{path}"
        data = json.dumps(body).encode() if body else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json"} if data else {},
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read())
        except urllib.error.HTTPError as e:
            return {"error": e.read().decode(), "status": e.code}
        except Exception as e:
            return {"error": str(e)}

    def _get(path: str) -> dict:
        return _api("GET", path)

    @mcp.tool()
    def calendar_connect(provider: str, chat_id: str) -> str:
        """Start the OAuth flow to connect a user's calendar.

        Returns an authorization URL the user must visit to grant access.
        Currently supports: google.

        Args:
            provider: Calendar provider ("google").
            chat_id: The user's Telegram/platform chat ID (used as user_id).
        """
        if provider != "google":
            return json.dumps({"error": f"Provider {provider!r} not yet supported. Use 'google'."})

        result = _get(f"/oauth/calendar/google/start?user_id={chat_id}")
        if "error" in result:
            _log(f"calendar[{agent_name}]: connect failed for {chat_id}: {result['error']}")
        else:
            _log(f"calendar[{agent_name}]: generated auth URL for {chat_id}")
        return json.dumps(result)

    @mcp.tool()
    def calendar_status(chat_id: str) -> str:
        """Check which calendar providers a user has connected.

        Args:
            chat_id: The user's Telegram/platform chat ID.
        """
        result = _get(f"/calendar/status/{chat_id}")
        return json.dumps(result)

    @mcp.tool()
    def get_events(chat_id: str, days_ahead: int = 7) -> str:
        """Get a user's upcoming calendar events.

        Args:
            chat_id: The user's Telegram/platform chat ID.
            days_ahead: How many days ahead to look (default 7).
        """
        result = _api("POST", "/calendar/events", {
            "user_id": chat_id,
            "days_ahead": days_ahead,
        })
        return json.dumps(result)

    @mcp.tool()
    def create_event(
        chat_id: str,
        title: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
    ) -> str:
        """Create a calendar event for a user.

        Args:
            chat_id: The user's Telegram/platform chat ID.
            title: Event title.
            start: Start datetime in ISO 8601 format (e.g. "2026-04-01T10:00:00").
            end: End datetime in ISO 8601 format.
            description: Optional event description.
            location: Optional event location.
        """
        result = _api("POST", "/calendar/create-event", {
            "user_id": chat_id,
            "title": title,
            "start": start,
            "end": end,
            "description": description,
            "location": location,
        })
        if "error" in result:
            _log(f"calendar[{agent_name}]: create_event failed for {chat_id}: {result['error']}")
        else:
            _log(f"calendar[{agent_name}]: created event '{title}' for {chat_id}")
        return json.dumps(result)

    @mcp.tool()
    def find_free_slot(
        chat_id: str,
        duration_minutes: int = 60,
        within_days: int = 7,
    ) -> str:
        """Find the next available free time slots in a user's calendar.

        Args:
            chat_id: The user's Telegram/platform chat ID.
            duration_minutes: Length of slot needed in minutes (default 60).
            within_days: Search window in days (default 7).
        """
        result = _api("POST", "/calendar/free-slots", {
            "user_id": chat_id,
            "duration_minutes": duration_minutes,
            "within_days": within_days,
        })
        return json.dumps(result)

    @mcp.tool()
    def delete_event(chat_id: str, event_id: str) -> str:
        """Delete a calendar event.

        Args:
            chat_id: The user's Telegram/platform chat ID.
            event_id: The event ID to delete (from get_events).
        """
        result = _api("POST", "/calendar/delete-event", {
            "user_id": chat_id,
            "event_id": event_id,
        })
        return json.dumps(result)

    return mcp
