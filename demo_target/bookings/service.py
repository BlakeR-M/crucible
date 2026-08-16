"""Booking orchestration: availability, reservations, invoices, and the cache.

This is the only module the HTTP handlers talk to.  It owns the ordering of the
writes that make up a booking and the short-lived cache of free slots that the
calendar screen hammers.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Dict, Iterable, List, Optional, Tuple

from .api import Page, idempotency_key_for, paginate
from .auth import require_permission
from .availability import BusinessHours, Slot, day_slots
from .errors import (
    BookingClosed,
    InvalidBookingWindow,
    NotFound,
    OutsideBusinessHours,
    SlotUnavailable,
)
from .invoicing import build_invoice
from .models import Actor, Booking, BookingStatus, Customer, Invoice, Resource
from .repository import InMemoryRepository, new_id

#: A cancellation this close to the start time carries the late fee.
NOTICE_WINDOW = timedelta(hours=24)


class BookingService:
    """Application service for the booking and invoicing flows."""

    def __init__(
        self,
        repo: InMemoryRepository,
        customers: Iterable[Customer],
        resources: Iterable[Resource],
        hours: BusinessHours = BusinessHours(),
        slot_minutes: int = 60,
    ) -> None:
        self.repo = repo
        self.hours = hours
        self.slot_minutes = slot_minutes
        self._customers: Dict[str, Customer] = {c.id: c for c in customers}
        self._resources: Dict[str, Resource] = {r.id: r for r in resources}
        self._slot_cache: Dict[Tuple[str, date], List[Slot]] = {}

    # ------------------------------------------------------------------
    # reads
    # ------------------------------------------------------------------
    def available_slots(self, actor: Actor, resource_id: str, day: date) -> List[Slot]:
        """Return the free slots on ``resource_id`` for ``day``.

        The calendar screen calls this once per resource per visible day, so
        the answer is cached and dropped whenever the resource's day changes.
        """
        require_permission(actor, "booking:read")
        self._resource(resource_id)

        cache_key = (resource_id, day)
        cached = self._slot_cache.get(cache_key)
        if cached is not None:
            return list(cached)

        free = [
            slot
            for slot in day_slots(day, self.hours, self.slot_minutes)
            if self.repo.is_slot_free(resource_id, slot.start, slot.end)
        ]
        self._slot_cache[cache_key] = free
        return list(free)

    def list_bookings(
        self,
        actor: Actor,
        *,
        customer_id: Optional[str] = None,
        resource_id: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Page[Booking]:
        """Return one page of bookings, earliest start first."""
        require_permission(actor, "booking:read")
        rows = self.repo.list_bookings(
            customer_id=customer_id, resource_id=resource_id
        )
        return paginate(rows, page=page, page_size=page_size)

    # ------------------------------------------------------------------
    # writes
    # ------------------------------------------------------------------
    def create_booking(
        self,
        actor: Actor,
        customer_id: str,
        resource_id: str,
        start: datetime,
        end: datetime,
        *,
        idempotency_key: Optional[str] = None,
        discount_percent: float = 0.0,
        requires_setup: bool = False,
        notes: str = "",
    ) -> Booking:
        """Reserve a slot, record the booking, and issue its deposit invoice."""
        require_permission(actor, "booking:write")
        customer = self._customer(customer_id)
        self._resource(resource_id)

        if end <= start:
            raise InvalidBookingWindow("booking must end after it starts")
        if not self.hours.contains(start, end):
            raise OutsideBusinessHours(
                f"{start:%H:%M}-{end:%H:%M} falls outside "
                f"{self.hours.opens:%H:%M}-{self.hours.closes:%H:%M}"
            )

        key = idempotency_key or idempotency_key_for(customer_id, resource_id, start)
        replayed = self.repo.booking_for_key(key)
        if replayed is not None:
            existing = self.repo.get_booking(replayed)
            if existing is not None:
                return existing

        if not self.repo.is_slot_free(resource_id, start, end):
            raise SlotUnavailable(
                f"{resource_id} is already booked for {start:%Y-%m-%d %H:%M}"
            )
        reservation_id = self.repo.reserve_slot(resource_id, start, end)

        booking = Booking(
            id=new_id("bkg"),
            customer_id=customer_id,
            resource_id=resource_id,
            start=start,
            end=end,
            status=BookingStatus.CONFIRMED,
            reservation_id=reservation_id,
            discount_percent=discount_percent,
            requires_setup=requires_setup,
            notes=notes,
        )
        self.repo.add_booking(booking)

        invoice = build_invoice(
            booking,
            customer,
            self._resource(resource_id),
            self.repo.next_invoice_number(),
        )
        self.repo.add_invoice(invoice)
        self.repo.remember_idempotency(key, booking.id)
        self._invalidate(resource_id, start.date())
        return booking

    def cancel_booking(
        self,
        actor: Actor,
        booking_id: str,
        *,
        now: Optional[datetime] = None,
    ) -> Invoice:
        """Cancel a booking, free its slot, and issue the cancellation invoice."""
        require_permission(actor, "booking:cancel")
        booking = self._booking(booking_id)
        if not booking.is_active:
            raise BookingClosed(f"booking {booking_id} is already {booking.status.value}")

        now = now or datetime.now()
        late = (booking.start - now) < NOTICE_WINDOW
        booking.status = (
            BookingStatus.CANCELLED_LATE if late else BookingStatus.CANCELLED
        )
        self.repo.update_booking(booking)

        if booking.reservation_id is not None:
            self.repo.release_slot(booking.reservation_id)
        self._invalidate(booking.resource_id, booking.start.date())

        invoice = build_invoice(
            booking,
            self._customer(booking.customer_id),
            self._resource(booking.resource_id),
            self.repo.next_invoice_number(),
        )
        self.repo.add_invoice(invoice)
        return invoice

    def reschedule_booking(
        self,
        actor: Actor,
        booking_id: str,
        new_start: datetime,
        new_end: datetime,
    ) -> Booking:
        """Move a booking to a new window, keeping its invoice history intact."""
        require_permission(actor, "booking:write")
        booking = self._booking(booking_id)
        if not booking.is_active:
            raise BookingClosed(f"booking {booking_id} is already {booking.status.value}")
        if new_end <= new_start:
            raise InvalidBookingWindow("booking must end after it starts")
        if not self.hours.contains(new_start, new_end):
            raise OutsideBusinessHours(
                f"{new_start:%H:%M}-{new_end:%H:%M} falls outside "
                f"{self.hours.opens:%H:%M}-{self.hours.closes:%H:%M}"
            )

        old_reservation = booking.reservation_id
        with self.repo.lock():
            if not self.repo.is_slot_free(
                booking.resource_id, new_start, new_end, ignore=old_reservation
            ):
                raise SlotUnavailable(
                    f"{booking.resource_id} is already booked for "
                    f"{new_start:%Y-%m-%d %H:%M}"
                )
            new_reservation = self.repo.reserve_slot(
                booking.resource_id, new_start, new_end
            )

        try:
            booking.start = new_start
            booking.end = new_end
            booking.reservation_id = new_reservation
            self.repo.update_booking(booking)
        except Exception:
            self.repo.release_slot(new_reservation)
            raise

        if old_reservation is not None:
            self.repo.release_slot(old_reservation)
        self._invalidate(booking.resource_id, new_start.date())
        return booking

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------
    def _invalidate(self, resource_id: str, day: date) -> None:
        """Drop the cached slot list for one resource-day."""
        self._slot_cache.pop((resource_id, day), None)

    def _customer(self, customer_id: str) -> Customer:
        try:
            return self._customers[customer_id]
        except KeyError:
            raise NotFound(f"unknown customer {customer_id!r}") from None

    def _resource(self, resource_id: str) -> Resource:
        try:
            return self._resources[resource_id]
        except KeyError:
            raise NotFound(f"unknown resource {resource_id!r}") from None

    def _booking(self, booking_id: str) -> Booking:
        booking = self.repo.get_booking(booking_id)
        if booking is None:
            raise NotFound(f"unknown booking {booking_id!r}")
        return booking
