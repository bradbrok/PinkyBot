"""CalDAV calendar adapter — supports iCloud, Nextcloud, Fastmail, Proton, etc.

Requires: caldav, icalendar

iCloud setup:
  - url: https://caldav.icloud.com
  - username: Apple ID email
  - password: App-specific password (from appleid.apple.com → Security → App Passwords)

Nextcloud setup:
  - url: https://your-nextcloud.example.com/remote.php/dav
  - username / password: Nextcloud credentials or app token
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from typing import Optional

from .base import AbstractCalendarAdapter, CalendarEvent, FreeSlot


class CalDAVAdapter(AbstractCalendarAdapter):
    """CalDAV adapter using the caldav Python library."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        calendar_name: Optional[str] = None,
    ):
        self._url = url
        self._username = username
        self._password = password
        self._calendar_name = calendar_name
        self._calendar = None

    def _get_calendar(self):
        if self._calendar is not None:
            return self._calendar
        import caldav
        client = caldav.DAVClient(url=self._url, username=self._username, password=self._password)
        principal = client.principal()
        calendars = principal.calendars()
        if not calendars:
            raise RuntimeError("No calendars found on CalDAV server")
        if self._calendar_name:
            for cal in calendars:
                if cal.name == self._calendar_name:
                    self._calendar = cal
                    return cal
            raise RuntimeError(f"Calendar {self._calendar_name!r} not found")
        self._calendar = calendars[0]
        return self._calendar

    def get_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        cal = self._get_calendar()
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        if end.tzinfo is None:
            end = end.replace(tzinfo=timezone.utc)
        results = cal.date_search(start=start, end=end, expand=True)
        events = []
        for vevent in results:
            try:
                comp = vevent.instance.vevent
                evt_start = comp.dtstart.value
                evt_end = comp.dtend.value if hasattr(comp, "dtend") else evt_start + timedelta(hours=1)
                if not isinstance(evt_start, datetime):
                    evt_start = datetime.combine(evt_start, datetime.min.time()).replace(tzinfo=timezone.utc)
                if not isinstance(evt_end, datetime):
                    evt_end = datetime.combine(evt_end, datetime.min.time()).replace(tzinfo=timezone.utc)
                events.append(CalendarEvent(
                    event_id=str(comp.uid.value),
                    title=str(comp.summary.value) if hasattr(comp, "summary") else "(no title)",
                    start=evt_start,
                    end=evt_end,
                    description=str(comp.description.value) if hasattr(comp, "description") else "",
                    location=str(comp.location.value) if hasattr(comp, "location") else "",
                ))
            except Exception:
                continue
        return events

    def create_event(self, title: str, start: datetime, end: datetime, description: str = "", location: str = "") -> CalendarEvent:
        from icalendar import Calendar, Event as IEvent
        import uuid as _uuid
        cal_obj = Calendar()
        cal_obj.add("prodid", "-//Pinky//Calendar//EN")
        cal_obj.add("version", "2.0")
        event = IEvent()
        uid = str(_uuid.uuid4())
        event.add("uid", uid)
        event.add("summary", title)
        event.add("dtstart", start)
        event.add("dtend", end)
        if description:
            event.add("description", description)
        if location:
            event.add("location", location)
        cal_obj.add_component(event)
        self._get_calendar().save_event(cal_obj.to_ical().decode())
        return CalendarEvent(event_id=uid, title=title, start=start, end=end, description=description, location=location)

    def delete_event(self, event_id: str) -> bool:
        try:
            for vevent in self._get_calendar().search(uid=event_id):
                vevent.delete()
            return True
        except Exception:
            return False

    def find_free_slots(self, duration_minutes: int, within_days: int = 7, working_hours: tuple[int, int] = (9, 18)) -> list[FreeSlot]:
        now = datetime.now(timezone.utc)
        end_range = now + timedelta(days=within_days)
        duration = timedelta(minutes=duration_minutes)
        work_start_h, work_end_h = working_hours
        busy = [(e.start, e.end) for e in self.get_events(now, end_range)]
        busy.sort()
        slots = []
        cursor = now.replace(minute=0, second=0, microsecond=0) + timedelta(hours=1)
        while cursor < end_range:
            day_start = cursor.replace(hour=work_start_h, minute=0, second=0)
            day_end = cursor.replace(hour=work_end_h, minute=0, second=0)
            slot_start = max(cursor, day_start)
            while slot_start + duration <= day_end:
                slot_end = slot_start + duration
                if not any(bs < slot_end and be > slot_start for bs, be in busy):
                    slots.append(FreeSlot(start=slot_start, end=slot_end))
                slot_start += timedelta(minutes=30)
            cursor = day_start + timedelta(days=1)
        return slots[:10]
