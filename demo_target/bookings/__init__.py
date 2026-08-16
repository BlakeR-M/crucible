"""Studio booking and invoicing service.

Public surface:

    from bookings import BookingService, InMemoryRepository

Everything else in the package is an implementation detail of those two.
"""

from .api import Page, paginate
from .availability import BusinessHours, Slot, day_slots, overlaps
from .errors import (
    BillingError,
    BookingClosed,
    BookingError,
    InvalidBookingWindow,
    NotFound,
    OutsideBusinessHours,
    PermissionDenied,
    SlotUnavailable,
)
from .models import (
    Actor,
    Adjustment,
    Booking,
    BookingStatus,
    Customer,
    Invoice,
    LineItem,
    Resource,
)
from .repository import InMemoryRepository, Reservation
from .service import BookingService

__all__ = [
    "Actor",
    "Adjustment",
    "BillingError",
    "Booking",
    "BookingClosed",
    "BookingError",
    "BookingService",
    "BookingStatus",
    "BusinessHours",
    "Customer",
    "InMemoryRepository",
    "InvalidBookingWindow",
    "Invoice",
    "LineItem",
    "NotFound",
    "OutsideBusinessHours",
    "Page",
    "PermissionDenied",
    "Reservation",
    "Resource",
    "Slot",
    "SlotUnavailable",
    "day_slots",
    "overlaps",
    "paginate",
]

__version__ = "1.4.2"
