"""CalDAV calendar adapter.

Works with Apple Calendar, Google Calendar (via CalDAV), Nextcloud, Fastmail,
iCloud, and any standards-compliant CalDAV server.
"""

from __future__ import annotations

import sys
from datetime import datetime, timedelta, timezone
from typing import Any

from .base import AbstractCalendarAdapter, CalendarEvent, FreeSlot


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _to_dt(val: Any) -> datetime:
    """Normalise vobject/icalendar datetime to aware datetime."""
    if isinstance(val, datetime):
        if val.tzinfo is None:
            return val.replace(tzinfo=timezone.utc)
        return val
    return datetime(val.year, val.month, val.day, tzinfo=timezone.utc)


class CalDAVAdapter(AbstractCalendarAdapter):
    """Calendar adapter backed by a CalDAV server."""

    def __init__(self, url: str, username: str, password: str) -> None:
        self._url = url
        self._username = username
        self._password = password
        self._client: Any = None
        self._principal: Any = None

    def _connect(self) -> None:
        if self._client is not None:
            return
        import caldav  # type: ignore[import]
        self._client = caldav.DAVClient(
            url=self._url,
            username=self._username,
            password=self._password,
        )
        self._principal = self._client.principal()
        _log(f"caldav: connected to {self._url}")

    def _calendars(self):
        self._connect()
        return self._principal.calendars()

    def get_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Return all events across all calendars in [start, end]."""
        events: list[CalendarEvent] = []
        for cal in self._calendars():
            try:
                raw = cal.date_search(start=start, end=end, expand=True)
                for item in raw:
                    try:
                        vevent = item.vobject_instance.vevent
                        event_id = str(getattr(vevent, "uid", None) or item.url)
                        title = str(getattr(vevent, "summary", None) or "Untitled")
                        ev_start = _to_dt(vevent.dtstart.value)
                        ev_end = _to_dt(
                            vevent.dtend.value
                            if hasattr(vevent, "dtend")
                            else ev_start + timedelta(hours=1)
                        )
                        desc = str(getattr(vevent, "description", None) or "")
                        loc = str(getattr(vevent, "location", None) or "")
                        url = str(getattr(vevent, "url", None) or "")
                        events.append(CalendarEvent(
                            event_id=event_id,
                            title=title,
                            start=ev_start,
                            end=ev_end,
                            description=desc,
                            location=loc,
                            url=url,
                        ))
                    except Exception as e:
                        _log(f"caldav: skipping malformed event: {e}")
            except Exception as e:
                _log(f"caldav: error reading calendar {cal}: {e}")
        events.sort(key=lambda e: e.start)
        return events

    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        description: str = "",
        location: str = "",
    ) -> CalendarEvent:
        """Create a new event in the first writeable calendar."""
        import uuid
        from icalendar import Calendar, Event  # type: ignore[import]

        event_id = str(uuid.uuid4())
        cal_obj = Calendar()
        cal_obj.add("prodid", "-//PinkyBot//EN")
        cal_obj.add("version", "2.0")

        ev = Event()
        ev.add("uid", event_id)
        ev.add("summary", title)
        ev.add("dtstart", start)
        ev.add("dtend", end)
        if description:
            ev.add("description", description)
        if location:
            ev.add("location", location)
        cal_obj.add_component(ev)

        target_cal = self._calendars()[0]
        target_cal.save_event(cal_obj.to_ical().decode())
        _log(f"caldav: created event '{title}' ({event_id})")

        return CalendarEvent(
            event_id=event_id,
            title=title,
            start=start,
            end=end,
            description=description,
            location=location,
        )

    def delete_event(self, event_id: str) -> bool:
        """Delete event by UID across all calendars."""
        for cal in self._calendars():
            try:
                results = cal.search(uid=event_id)
                for item in results:
                    item.delete()
                    _log(f"caldav: deleted event {event_id}")
                    return True
            except Exception:
                continue
        _log(f"caldav: event {event_id} not found")
        return False

    def find_free_slots(
        self,
        duration_minutes: int,
        within_days: int = 7,
        working_hours: tuple[int, int] = (9, 18),
    ) -> list[FreeSlot]:
        """Find free slots of at least duration_minutes within the next N days."""
        now = datetime.now(tz=timezone.utc)
        end_window = now + timedelta(days=within_days)
        existing = self.get_events(now, end_window)

        slots: list[FreeSlot] = []
        work_start_h, work_end_h = working_hours
        duration = timedelta(minutes=duration_minutes)

        day = now.date()
        for _ in range(within_days):
            day_start = datetime(day.year, day.month, day.day, work_start_h, 0, tzinfo=timezone.utc)
            day_end = datetime(day.year, day.month, day.day, work_end_h, 0, tzinfo=timezone.utc)

            busy = sorted(
                (max(e.start, day_start), min(e.end, day_end))
                for e in existing
                if e.start < day_end and e.end > day_start
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

    def test_connection(self) -> dict:
        """Test connectivity and return status."""
        try:
            cals = self._calendars()
            names = [str(getattr(c, "name", c.url)) for c in cals[:5]]
            return {"ok": True, "calendars": names, "count": len(cals)}
        except Exception as e:
            return {"ok": False, "error": str(e)}
