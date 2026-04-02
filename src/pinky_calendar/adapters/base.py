"""Abstract base class for calendar adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime


@dataclass
class CalendarEvent:
    """Normalised event across all providers."""
    event_id: str
    title: str
    start: datetime
    end: datetime
    description: str = ""
    location: str = ""
    url: str = ""


@dataclass
class FreeSlot:
    """A free time slot."""
    start: datetime
    end: datetime


class AbstractCalendarAdapter(ABC):
    """Common interface all calendar adapters must implement."""

    @abstractmethod
    def get_events(self, start: datetime, end: datetime) -> list[CalendarEvent]:
        """Return events in the given date range."""
        ...

    @abstractmethod
    def create_event(
        self,
        title: str,
        start: datetime,
        end: datetime,
        description: str = "",
        location: str = "",
    ) -> CalendarEvent:
        """Create a new event. Returns the created event."""
        ...

    @abstractmethod
    def delete_event(self, event_id: str) -> bool:
        """Delete an event by ID. Returns True on success."""
        ...

    @abstractmethod
    def find_free_slots(
        self,
        duration_minutes: int,
        within_days: int = 7,
        working_hours: tuple[int, int] = (9, 18),
    ) -> list[FreeSlot]:
        """Find available slots of the given duration within the next N days."""
        ...
