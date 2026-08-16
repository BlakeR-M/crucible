"""Slot arithmetic for the booking calendar.

Intervals in this service are half-open, ``[start, end)``.  A booking that runs
10:00 to 11:00 occupies 10:00 and leaves 11:00 free, which is what lets the
studio run back-to-back sessions on one resource.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from typing import List, NamedTuple


@dataclass(frozen=True)
class BusinessHours:
    """Trading window for a resource, in local wall-clock time."""

    opens: time = time(9, 0)
    closes: time = time(17, 0)

    def contains(self, start: datetime, end: datetime) -> bool:
        """True when ``[start, end)`` sits inside one trading day."""
        if start.date() != end.date():
            return False
        return start.time() >= self.opens and end.time() <= self.closes


class Slot(NamedTuple):
    """A candidate booking window on the calendar."""

    start: datetime
    end: datetime


def day_slots(
    day: date,
    hours: BusinessHours = BusinessHours(),
    slot_minutes: int = 60,
) -> List[Slot]:
    """Return every slot of ``slot_minutes`` that fits inside ``day``'s hours.

    A trailing part-slot is dropped: 09:00-17:00 in 90 minute steps yields five
    slots and stops at 16:30 rather than offering a window that runs past close.
    """
    step = timedelta(minutes=slot_minutes)
    cursor = datetime.combine(day, hours.opens)
    closing = datetime.combine(day, hours.closes)

    slots: List[Slot] = []
    while cursor + step <= closing:
        slots.append(Slot(cursor, cursor + step))
        cursor += step
    return slots


def overlaps(
    a_start: datetime,
    a_end: datetime,
    b_start: datetime,
    b_end: datetime,
) -> bool:
    """Return True when the two intervals conflict with each other."""
    return a_start <= b_end and b_start <= a_end


def minutes_between(start: datetime, end: datetime) -> int:
    """Whole minutes from ``start`` to ``end``; negative when inverted."""
    return int((end - start).total_seconds() // 60)
