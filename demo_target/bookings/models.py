"""Domain types for the booking and invoicing service."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from enum import Enum
from typing import List, Optional

from .money import ZERO


class BookingStatus(str, Enum):
    """Lifecycle of a booking.

    ``CANCELLED_LATE`` is a cancellation inside the notice window; it carries a
    fee where a plain ``CANCELLED`` does not.
    """

    PENDING = "pending"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    CANCELLED_LATE = "cancelled_late"
    COMPLETED = "completed"


@dataclass(frozen=True)
class Actor:
    """Whoever is making the call: a staff member, an owner, or an integration."""

    id: str
    name: str
    role: str
    org_id: str = "flow"


@dataclass(frozen=True)
class Customer:
    """A paying customer of the studio."""

    id: str
    name: str
    billing_email: Optional[str] = None
    org_id: str = "flow"


@dataclass(frozen=True)
class Resource:
    """A bookable thing: a room, a chair, a piece of equipment."""

    id: str
    name: str
    hourly_rate: Decimal
    capacity: int = 1


@dataclass
class Booking:
    """A single reservation of one resource for one customer."""

    id: str
    customer_id: str
    resource_id: str
    start: datetime
    end: datetime
    status: BookingStatus = BookingStatus.CONFIRMED
    reservation_id: Optional[str] = None
    discount_percent: float = 0.0
    requires_setup: bool = False
    notes: str = ""
    created_at: datetime = field(default_factory=datetime.now)

    @property
    def duration_minutes(self) -> int:
        """Length of the booking in whole minutes."""
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def is_active(self) -> bool:
        """True while the booking still holds its slot on the calendar."""
        return self.status in (BookingStatus.PENDING, BookingStatus.CONFIRMED)

    @property
    def is_cancelled(self) -> bool:
        """True once the booking has been cancelled, on time or late."""
        return self.status in (BookingStatus.CANCELLED, BookingStatus.CANCELLED_LATE)


@dataclass(frozen=True)
class LineItem:
    """One priced row on an invoice."""

    description: str
    quantity: Decimal
    unit_price: Decimal
    amount: Decimal


@dataclass(frozen=True)
class Adjustment:
    """A fee or credit applied on top of the line items."""

    description: str
    amount: Decimal


@dataclass(frozen=True)
class Invoice:
    """A tax invoice issued against a booking."""

    id: str
    number: str
    booking_id: str
    customer_id: str
    line_items: List[LineItem]
    adjustments: List[Adjustment]
    subtotal: Decimal
    discount: Decimal
    tax: Decimal
    total: Decimal
    issued_at: datetime

    @property
    def is_payable(self) -> bool:
        """True when the invoice asks the customer for money."""
        return self.total > ZERO
