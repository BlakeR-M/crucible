"""In-memory persistence for the booking service.

The production deployment swaps this class for the Postgres repository; the two
share this interface and this test suite.  A single re-entrant lock guards the
tables because the API workers share one repository instance across threads.
"""

from __future__ import annotations

import threading
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Dict, Iterator, List, Optional, Sequence

from .availability import overlaps
from .models import Booking, BookingStatus, Invoice


def new_id(prefix: str) -> str:
    """Return a short, sortable-enough identifier with a type prefix."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


@dataclass
class Reservation:
    """A hold on a resource for a window of time.

    Reservations are the source of truth for availability; a booking points at
    one and releasing it is what frees the slot again.
    """

    id: str
    resource_id: str
    start: datetime
    end: datetime
    active: bool = True


class InMemoryRepository:
    """Thread-safe store for bookings, invoices, and slot reservations."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._bookings: Dict[str, Booking] = {}
        self._invoices: Dict[str, Invoice] = {}
        self._reservations: Dict[str, Reservation] = {}
        self._idempotency: Dict[str, str] = {}
        self._invoice_seq = 0

    @contextmanager
    def lock(self) -> Iterator["InMemoryRepository"]:
        """Hold the repository lock across a read-modify-write sequence."""
        with self._lock:
            yield self

    # ------------------------------------------------------------------
    # reservations
    # ------------------------------------------------------------------
    def is_slot_free(
        self,
        resource_id: str,
        start: datetime,
        end: datetime,
        ignore: Optional[str] = None,
    ) -> bool:
        """Report whether ``resource_id`` is unheld for ``[start, end)``.

        ``ignore`` skips one reservation, which is what lets a booking be moved
        without colliding with the hold it already owns.

        A caller that writes based on this answer must hold :meth:`lock` across
        both the check and the write: the reservation table is free to change
        between the two calls otherwise.
        """
        with self._lock:
            for reservation in self._reservations.values():
                if not reservation.active or reservation.resource_id != resource_id:
                    continue
                if ignore is not None and reservation.id == ignore:
                    continue
                if overlaps(start, end, reservation.start, reservation.end):
                    return False
            return True

    def reserve_slot(self, resource_id: str, start: datetime, end: datetime) -> str:
        """Record a hold on ``resource_id`` and return the reservation id."""
        with self._lock:
            reservation = Reservation(
                id=new_id("res"), resource_id=resource_id, start=start, end=end
            )
            self._reservations[reservation.id] = reservation
            return reservation.id

    def release_slot(self, reservation_id: str) -> None:
        """Release a hold.  Releasing an unknown or released hold is a no-op."""
        with self._lock:
            reservation = self._reservations.get(reservation_id)
            if reservation is not None:
                reservation.active = False

    def active_reservations(self, resource_id: str) -> List[Reservation]:
        """Every live hold on one resource, oldest window first."""
        with self._lock:
            held = [
                r
                for r in self._reservations.values()
                if r.active and r.resource_id == resource_id
            ]
        return sorted(held, key=lambda r: r.start)

    # ------------------------------------------------------------------
    # bookings
    # ------------------------------------------------------------------
    def add_booking(self, booking: Booking) -> None:
        """Insert a booking."""
        with self._lock:
            self._bookings[booking.id] = booking

    def get_booking(self, booking_id: str) -> Optional[Booking]:
        """Fetch one booking, or None when the id is unknown."""
        with self._lock:
            return self._bookings.get(booking_id)

    def update_booking(self, booking: Booking) -> None:
        """Persist changes to an existing booking."""
        with self._lock:
            if booking.id not in self._bookings:
                raise KeyError(f"unknown booking {booking.id}")
            self._bookings[booking.id] = booking

    def list_bookings(
        self,
        *,
        customer_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        statuses: Optional[Sequence[BookingStatus]] = None,
    ) -> List[Booking]:
        """Bookings matching every supplied filter, earliest start first."""
        with self._lock:
            rows = list(self._bookings.values())

        if customer_id is not None:
            rows = [b for b in rows if b.customer_id == customer_id]
        if resource_id is not None:
            rows = [b for b in rows if b.resource_id == resource_id]
        if statuses is not None:
            wanted = set(statuses)
            rows = [b for b in rows if b.status in wanted]
        return sorted(rows, key=lambda b: b.start)

    # ------------------------------------------------------------------
    # invoices
    # ------------------------------------------------------------------
    def add_invoice(self, invoice: Invoice) -> None:
        """Insert an invoice."""
        with self._lock:
            self._invoices[invoice.id] = invoice

    def get_invoice(self, invoice_id: str) -> Optional[Invoice]:
        """Fetch one invoice, or None when the id is unknown."""
        with self._lock:
            return self._invoices.get(invoice_id)

    def list_invoices(self, *, customer_id: Optional[str] = None) -> List[Invoice]:
        """Invoices matching the filter, oldest first."""
        with self._lock:
            rows = list(self._invoices.values())
        if customer_id is not None:
            rows = [i for i in rows if i.customer_id == customer_id]
        return sorted(rows, key=lambda i: i.number)

    def next_invoice_number(self) -> str:
        """Allocate the next gapless invoice number.

        The read and the increment share one lock so two workers issuing
        invoices at the same moment cannot land on the same number.
        """
        with self._lock:
            self._invoice_seq += 1
            return f"INV-{self._invoice_seq:05d}"

    # ------------------------------------------------------------------
    # idempotency
    # ------------------------------------------------------------------
    def booking_for_key(self, key: str) -> Optional[str]:
        """Return the booking id already recorded against ``key``."""
        with self._lock:
            return self._idempotency.get(key)

    def remember_idempotency(self, key: str, booking_id: str) -> None:
        """Bind ``key`` to ``booking_id`` so a retry replays the same booking."""
        with self._lock:
            self._idempotency[key] = booking_id
