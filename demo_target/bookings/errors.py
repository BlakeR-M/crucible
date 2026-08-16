"""Domain errors raised by the booking service.

Every error the service raises deliberately derives from :class:`BookingError`
so the HTTP layer can map one exception family onto status codes.
"""


class BookingError(Exception):
    """Base class for every error the booking domain raises."""


class NotFound(BookingError):
    """A referenced customer, resource, or booking does not exist."""


class PermissionDenied(BookingError):
    """The actor's role does not carry the permission being exercised."""


class SlotUnavailable(BookingError):
    """The requested window is already reserved on that resource."""


class OutsideBusinessHours(BookingError):
    """The requested window falls outside the resource's trading hours."""


class InvalidBookingWindow(BookingError):
    """The requested window is empty, inverted, or spans more than one day."""


class BookingClosed(BookingError):
    """The booking has already been cancelled or completed."""


class BillingError(BookingError):
    """The customer cannot be invoiced (missing or invalid billing details)."""
