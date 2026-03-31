"""Google Calendar adapter.

Requires: google-api-python-client, google-auth-oauthlib, google-auth-httplib2
"""

from __future__ import annotations

import time
from datetime import datetime, timezone, timedelta
from typing import TYPE_CHECKING

from .base import AbstractCalendarAdapter, CalendarEvent, FreeSlot

if TYPE_CHECKING:
    from ..store import CalendarToken


class GoogleCalendarAdapter(AbstractCalendarAdapter):
    """Wraps the Google Calendar v3 API."""

    SCOPES = ["https://www.googleapis.com/auth/calendar"]

    def __init__(self, token: "CalendarToken", client_id: str, client_secret: str):
        self._token = token
        self._client_id = client_id
        self._client_secret = client_secret
        self._service = self._build_service()

    # ── Internal helpers ──────────────────────────────────────

    def _build_service(self):
        from google.oauth2.credentials import Credentials
        from googleapiclient.discovery import build

        creds = Credentials(
            token=self._token.access_token,
            refresh_token=self._token.refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._client_id,
            client_secret=self._client_secret,
            scopes=self.SCOPES,
        )
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    @staticmethod
    def _to_dt(google_dt: dict) -> datetime:
        """Parse a Google API datetime/date dict into a UTC datetime."""
        if "dateTime" in google_dt:
            return datetime.fromisoformat(google_dt["dateTime"].replace("Z", "+00:00"))
        # All-day event: date only
        d = google_dt["date"]
        return datetime.fromisoformat(d).replace(tzinfo=timezone.utc)

    @staticmethod
    def _fmt(dt: datetime) -> str:
        """Format a datetime for the Google API (RFC3339)."""
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.isoformat()

    # ── AbstractCalendarAdapter ───────────────────────────────

    def get_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        result = self._service.events().list(
            calendarId="primary",
            timeMin=self._fmt(start),
            timeMax=self._fmt(end),
            singleEvents=True,
            orderBy="startTime",
            maxResults=100,
        ).execute()

        events = []
        for item in result.get("items", []):
            events.append(CalendarEvent(
                event_id=item["id"],
                title=item.get("summary", "(no title)"),
                start=self._to_dt(item["start"]),
                end=self._to_dt(item["end"]),
                description=item.get("description", ""),
                location=item.get("location", ""),
                url=item.get("htmlLink", ""),
            ))
        return events

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        description: str = "",
        location: str = "",
    ) -> CalendarEvent:
        body = {
            "summary": title,
            "description": description,
            "location": location,
            "start": {"dateTime": self._fmt(start)},
            "end": {"dateTime": self._fmt(end)},
        }
        item = self._service.events().insert(calendarId="primary", body=body).execute()
        return CalendarEvent(
            event_id=item["id"],
            title=item.get("summary", title),
            start=self._to_dt(item["start"]),
            end=self._to_dt(item["end"]),
            description=description,
            location=location,
            url=item.get("htmlLink", ""),
        )

    def delete_event(self, event_id: str) -> bool:
        try:
            self._service.events().delete(calendarId="primary", eventId=event_id).execute()
            return True
        except Exception:
            return False

    def find_free_slots(
        self,
        duration_minutes: int,
        within_days: int = 7,
        working_hours: tuple[int, int] = (9, 18),
    ) -> list[FreeSlot]:
        now = datetime.now(timezone.utc)
        end_range = now + timedelta(days=within_days)

        # Use freebusy query for efficiency
        body = {
            "timeMin": self._fmt(now),
            "timeMax": self._fmt(end_range),
            "items": [{"id": "primary"}],
        }
        fb = self._service.freebusy().query(body=body).execute()
        busy_periods = fb["calendars"]["primary"]["busy"]

        busy = []
        for period in busy_periods:
            busy.append((
                datetime.fromisoformat(period["start"].replace("Z", "+00:00")),
                datetime.fromisoformat(period["end"].replace("Z", "+00:00")),
            ))
        busy.sort()

        slots = []
        duration = timedelta(minutes=duration_minutes)
        work_start_h, work_end_h = working_hours

        # Walk day by day through working hours
        cursor = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        while cursor < end_range:
            day_start = cursor.replace(hour=work_start_h, minute=0, second=0)
            day_end = cursor.replace(hour=work_end_h, minute=0, second=0)

            slot_start = max(cursor, day_start)
            while slot_start + duration <= day_end:
                slot_end = slot_start + duration
                # Check if this window overlaps any busy period
                conflict = any(
                    bs < slot_end and be > slot_start
                    for bs, be in busy
                )
                if not conflict:
                    slots.append(FreeSlot(start=slot_start, end=slot_end))
                slot_start += timedelta(minutes=30)

            cursor = (day_start + timedelta(days=1))

        return slots[:10]  # Return up to 10 suggestions
