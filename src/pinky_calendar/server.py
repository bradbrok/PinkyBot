"""pinky-calendar MCP Server — calendar tools for Claude Code agents.

Exposes get_events, create_event, find_free_slots, delete_event as MCP tools.
Currently supports CalDAV (Apple Calendar, Google via CalDAV, Nextcloud, etc.).
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timedelta, timezone

from mcp.server.fastmcp import FastMCP


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _err(msg: str) -> str:
    return json.dumps({"error": msg})


def _parse_dt(s: str) -> datetime:
    """Parse ISO datetime string. Returns aware UTC datetime."""
    s = s.strip()
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        # Try date-only
        from datetime import date
        d = date.fromisoformat(s)
        dt = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _fmt_dt(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M %Z")


def create_server(
    caldav_url: str = "",
    caldav_username: str = "",
    caldav_password: str = "",
    *,
    host: str = "127.0.0.1",
    port: int = 8105,
) -> FastMCP:
    mcp = FastMCP("pinky-calendar", host=host, port=port)

    # Build adapter lazily — only if credentials provided
    adapter = None

    def _get_adapter():
        nonlocal adapter
        if adapter is not None:
            return adapter
        if not caldav_url or not caldav_username:
            return None
        from pinky_calendar.adapters.caldav import CalDAVAdapter
        adapter = CalDAVAdapter(
            url=caldav_url,
            username=caldav_username,
            password=caldav_password,
        )
        return adapter

    # ── Tools ─────────────────────────────────────────────────────────────────

    @mcp.tool()
    def get_events(
        start_date: str = "",
        end_date: str = "",
        days: int = 7,
    ) -> str:
        """Get calendar events in a date range.

        Args:
            start_date: ISO date/datetime to start from (default: now)
            end_date: ISO date/datetime to end at (default: start + days)
            days: Number of days to look ahead if end_date not given (default 7)

        Returns JSON list of events with id, title, start, end, description, location.
        """
        a = _get_adapter()
        if not a:
            return _err("Calendar not configured. Set CALDAV_URL, CALDAV_USERNAME, CALDAV_PASSWORD.")

        now = datetime.now(tz=timezone.utc)
        start = _parse_dt(start_date) if start_date else now
        end = _parse_dt(end_date) if end_date else start + timedelta(days=days)

        try:
            events = a.get_events(start, end)
            return json.dumps([
                {
                    "id": e.event_id,
                    "title": e.title,
                    "start": _fmt_dt(e.start),
                    "end": _fmt_dt(e.end),
                    "description": e.description,
                    "location": e.location,
                    "url": e.url,
                }
                for e in events
            ])
        except Exception as e:
            return _err(f"Failed to fetch events: {e}")

    @mcp.tool()
    def create_event(
        title: str,
        start: str,
        end: str,
        description: str = "",
        location: str = "",
    ) -> str:
        """Create a new calendar event.

        Args:
            title: Event title/summary
            start: ISO datetime for event start (e.g. "2025-06-10T14:00:00")
            end: ISO datetime for event end (e.g. "2025-06-10T15:00:00")
            description: Optional event description
            location: Optional location

        Returns JSON with created event id and details.
        """
        a = _get_adapter()
        if not a:
            return _err("Calendar not configured.")

        try:
            ev = a.create_event(
                title=title,
                start=_parse_dt(start),
                end=_parse_dt(end),
                description=description,
                location=location,
            )
            return json.dumps({
                "created": True,
                "id": ev.event_id,
                "title": ev.title,
                "start": _fmt_dt(ev.start),
                "end": _fmt_dt(ev.end),
            })
        except Exception as e:
            return _err(f"Failed to create event: {e}")

    @mcp.tool()
    def delete_event(event_id: str) -> str:
        """Delete a calendar event by its UID.

        Args:
            event_id: The event UID (from get_events)

        Returns JSON {deleted: true} or an error.
        """
        a = _get_adapter()
        if not a:
            return _err("Calendar not configured.")

        try:
            ok = a.delete_event(event_id)
            return json.dumps({"deleted": ok})
        except Exception as e:
            return _err(f"Failed to delete event: {e}")

    @mcp.tool()
    def find_free_slots(
        duration_minutes: int = 60,
        within_days: int = 7,
        work_start_hour: int = 9,
        work_end_hour: int = 18,
    ) -> str:
        """Find free time slots of at least duration_minutes.

        Args:
            duration_minutes: Minimum slot length in minutes (default 60)
            within_days: How many days ahead to look (default 7)
            work_start_hour: Working day start hour 0-23 (default 9)
            work_end_hour: Working day end hour 0-23 (default 18)

        Returns JSON list of {start, end} free slots.
        """
        a = _get_adapter()
        if not a:
            return _err("Calendar not configured.")

        try:
            slots = a.find_free_slots(
                duration_minutes=duration_minutes,
                within_days=within_days,
                working_hours=(work_start_hour, work_end_hour),
            )
            return json.dumps([
                {"start": _fmt_dt(s.start), "end": _fmt_dt(s.end)}
                for s in slots
            ])
        except Exception as e:
            return _err(f"Failed to find free slots: {e}")

    _log(f"pinky-calendar: server ready (caldav_url={caldav_url or 'not set'})")
    return mcp
