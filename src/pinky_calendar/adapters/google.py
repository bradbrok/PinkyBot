"""Google Calendar adapter via the Google Calendar REST API.

Uses google-api-python-client with oauth2 credentials.  Supports automatic
token refresh via an optional on_token_refresh callback so the API layer can
persist refreshed tokens.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Callable

from .base import AbstractCalendarAdapter, CalendarEvent, FreeSlot


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _parse_dt(value: str | dict) -> datetime:
    """Parse a Google Calendar dateTime or date string to an aware datetime."""
    if isinstance(value, dict):
        # Google returns {"dateTime": "...", "timeZone": "..."}  or  {"date": "..."}
        raw = value.get("dateTime") or value.get("date") or ""
    else:
        raw = value or ""

    raw = raw.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        # Fallback: date-only string
        parts = raw[:10].split("-")
        dt = datetime(int(parts[0]), int(parts[1]), int(parts[2]), tzinfo=timezone.utc)

    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_dt(dt: datetime) -> str:
    """Format datetime for Google Calendar API (RFC3339)."""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


class GoogleCalendarAdapter(AbstractCalendarAdapter):
    """Calendar adapter backed by the Google Calendar API."""

    def __init__(
        self,
        access_token: str,
        refresh_token: str,
        client_id: str,
        client_secret: str,
        token_expiry: str | None = None,
        on_token_refresh: Callable[[str, datetime | None], None] | None = None,
    ) -> None:
        self._access_token = access_token
        self._refresh_token = refresh_token
        self._client_id = client_id
        self._client_secret = client_secret
        self._token_expiry: datetime | None = None
        self._on_token_refresh = on_token_refresh
        self._service = None

        if token_expiry:
            try:
                self._token_expiry = datetime.fromisoformat(token_expiry.replace("Z", "+00:00"))
                if self._token_expiry.tzinfo is None:
                    self._token_expiry = self._token_expiry.replace(tzinfo=timezone.utc)
            except Exception:
                pass

    # ── Internal helpers ────────────────────────────────────────────────────

    def _is_expired(self) -> bool:
        """True when token is missing or within 60 s of expiry."""
        if not self._access_token:
            return True
        if self._token_expiry is None:
            return False
        return datetime.now(tz=timezone.utc) >= self._token_expiry - timedelta(seconds=60)

    def _maybe_refresh(self) -> None:
        """Refresh the access token if expired."""
        if not self._is_expired():
            return
        from pinky_calendar.oauth import refresh_access_token
        _log("google_calendar: access token expired, refreshing…")
        result = refresh_access_token(self._client_id, self._client_secret, self._refresh_token)
        self._access_token = result["access_token"]
        self._token_expiry = result.get("expiry")
        self._service = None  # rebuild with new token
        if self._on_token_refresh:
            try:
                self._on_token_refresh(self._access_token, self._token_expiry)
            except Exception as e:
                _log(f"google_calendar: on_token_refresh callback failed: {e}")

    def _get_service(self):
        """Return (and cache) the googleapiclient service object."""
        if self._service is not None:
            return self._service
        self._maybe_refresh()

        from google.oauth2.credentials import Credentials  # type: ignore[import]
        from googleapiclient.discovery import build  # type: ignore[import]

        creds = Credentials(
            token=self._access_token,
            refresh_token=self._refresh_token,
            token_uri="https://oauth2.googleapis.com/token",
            client_id=self._client_id,
            client_secret=self._client_secret,
        )
        self._service = build("calendar", "v3", credentials=creds, cache_discovery=False)
        return self._service

    # ── AbstractCalendarAdapter ─────────────────────────────────────────────

    def get_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Return events in the primary calendar between start and end."""
        svc = self._get_service()
        result = (
            svc.events()
            .list(
                calendarId="primary",
                timeMin=_format_dt(start),
                timeMax=_format_dt(end),
                singleEvents=True,
                orderBy="startTime",
                maxResults=250,
            )
            .execute()
        )

        events: list[CalendarEvent] = []
        for item in result.get("items", []):
            try:
                ev_start = _parse_dt(item["start"])
                ev_end = _parse_dt(item.get("end", item["start"]))
                events.append(
                    CalendarEvent(
                        event_id=item["id"],
                        title=item.get("summary", "Untitled"),
                        start=ev_start,
                        end=ev_end,
                        description=item.get("description", ""),
                        location=item.get("location", ""),
                        url=item.get("htmlLink", ""),
                    )
                )
            except Exception as e:
                _log(f"google_calendar: skipping malformed event {item.get('id')}: {e}")
        return events

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        description: str = "",
        location: str = "",
    ) -> CalendarEvent:
        """Create a new event in the primary calendar."""
        svc = self._get_service()
        body: dict = {
            "summary": title,
            "start": {"dateTime": _format_dt(start)},
            "end": {"dateTime": _format_dt(end)},
        }
        if description:
            body["description"] = description
        if location:
            body["location"] = location

        created = svc.events().insert(calendarId="primary", body=body).execute()
        _log(f"google_calendar: created event '{title}' ({created['id']})")
        return CalendarEvent(
            event_id=created["id"],
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
            url=created.get("htmlLink", ""),
        )

    def delete_event(self, event_id: str) -> bool:
        """Delete an event from the primary calendar by ID."""
        try:
            self._get_service().events().delete(calendarId="primary", eventId=event_id).execute()
            _log(f"google_calendar: deleted event {event_id}")
            return True
        except Exception as e:
            _log(f"google_calendar: delete failed for {event_id}: {e}")
            return False

    def find_free_slots(
        self,
        duration_minutes: int,
        within_days: int = 7,
        working_hours: tuple[int, int] = (9, 18),
    ) -> list[FreeSlot]:
        """Find free slots using the freebusy API."""
        svc = self._get_service()
        now = datetime.now(tz=timezone.utc)
        end_window = now + timedelta(days=within_days)

        fb_result = (
            svc.freebusy()
            .query(
                body={
                    "timeMin": _format_dt(now),
                    "timeMax": _format_dt(end_window),
                    "items": [{"id": "primary"}],
                }
            )
            .execute()
        )

        busy_raw = fb_result.get("calendars", {}).get("primary", {}).get("busy", [])
        busy_intervals = [(_parse_dt(b["start"]), _parse_dt(b["end"])) for b in busy_raw]

        slots: list[FreeSlot] = []
        work_start_h, work_end_h = working_hours
        duration = timedelta(minutes=duration_minutes)

        day = now.date()
        for _ in range(within_days):
            day_start = datetime(day.year, day.month, day.day, work_start_h, 0, tzinfo=timezone.utc)
            day_end = datetime(day.year, day.month, day.day, work_end_h, 0, tzinfo=timezone.utc)

            busy = sorted(
                (max(b_s, day_start), min(b_e, day_end))
                for b_s, b_e in busy_intervals
                if b_s < day_end and b_e > day_start
            )

            cursor = max(day_start, now)
            for b_start, b_end in busy:
                if cursor + duration <= b_start:
                    slots.append(FreeSlot(start=cursor, end=b_start))
                cursor = max(cursor, b_end)
            if cursor + duration <= day_end:
                slots.append(FreeSlot(start=cursor, end=day_end))

            day = day + timedelta(days=1)

        return slots

    # ── Extra ───────────────────────────────────────────────────────────────

    def test_connection(self) -> dict:
        """Verify connectivity by fetching primary calendar metadata."""
        try:
            cal = self._get_service().calendars().get(calendarId="primary").execute()
            return {
                "ok": True,
                "email": cal.get("id", ""),
                "summary": cal.get("summary", ""),
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}
