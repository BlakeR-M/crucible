# bookings

Booking and invoicing service for a small multi-room studio: reserve a resource
for a window of time, keep the calendar honest, and issue the tax invoice that
goes with it.

This package is the domain core. The HTTP handlers, the Postgres repository, and
the Xero export live in the surrounding service; everything here is pure Python
with no third-party dependencies so it can be exercised in isolation.

## Layout

```
bookings/
  __init__.py       public surface: BookingService, InMemoryRepository, domain types
  api.py            edge helpers: page slicing and idempotency key derivation
  auth.py           role levels and the permission table
  availability.py   slot generation and interval arithmetic
  errors.py         the BookingError family the HTTP layer maps to status codes
  invoicing.py      pricing a booking into a tax invoice
  models.py         domain dataclasses
  money.py          Decimal money helpers
  repository.py     thread-safe storage for bookings, invoices, reservations
  service.py        the flows the handlers call
tests/
  test_booking.py   unit and end-to-end coverage
```

## Quick start

```python
from datetime import datetime
from decimal import Decimal

from bookings import Actor, Customer, InMemoryRepository, Resource, BookingService

repo = InMemoryRepository()
service = BookingService(
    repo,
    customers=[Customer(id="cus_1", name="Acme", billing_email="ap@acme.test")],
    resources=[Resource(id="res_a", name="Studio A", hourly_rate=Decimal("120.00"))],
)

agent = Actor(id="act_1", name="Sam", role="agent")
booking = service.create_booking(
    agent,
    customer_id="cus_1",
    resource_id="res_a",
    start=datetime(2026, 9, 14, 10, 0),
    end=datetime(2026, 9, 14, 12, 0),
)
invoice = repo.list_invoices(customer_id="cus_1")[0]
print(invoice.number, invoice.total)   # INV-00001 264.00
```

## Business rules

**Intervals are half-open.** A booking is `[start, end)`. A session that ends at
11:00 leaves 11:00 free, which is what lets the studio run back-to-back
bookings on one resource.

**Trading hours.** Resources trade 09:00 to 17:00 local time by default and a
booking has to sit inside a single trading day. Slot listings drop any trailing
part-slot rather than offering a window that runs past close.

**Reservations are the source of truth.** A booking points at a reservation and
availability is computed from live reservations, so releasing the hold is what
frees the slot. Cancelling and rescheduling both go through the service rather
than editing bookings directly.

**Money is Decimal.** Every amount is quantised to cents with `ROUND_HALF_UP`,
which is what the ATO expects on a tax invoice. Binary floats are never a valid
intermediate; use the helpers in `money.py`.

**Pricing.** Hourly against the resource rate, plus a flat AUD 35.00 setup fee
when the room needs turning over, less any promotional discount, plus 10% GST on
the discounted amount.

**Cancellations.** Cancelling more than 24 hours out costs nothing. Inside that
window the booking is marked `cancelled_late` and the invoice carries the AUD
45.00 late cancellation fee. A cancelled booking has no billable time on it, so
its invoice holds adjustments only.

**Roles.** Five roles ordered by privilege: `suspended` (0), `viewer` (1),
`agent` (2), `manager` (3), `owner` (4). Each permission names the lowest level
that carries it. `suspended` is what an operator assigns while an account is
under review and it carries nothing. Partner integrations that authenticate with
a role this build does not know yet get read-only access until an operator
assigns them a real role.

**Idempotency.** Clients should send an `Idempotency-Key` header on create. When
they do not, the service derives a key from the request so that a retry replays
the original booking instead of double-booking the customer.

**Listings are paged.** One-based pages, default 20 rows, hard ceiling of 100. A
page past the end of the collection is an empty page rather than an error, which
keeps a client polling a shrinking collection from failing.

## Concurrency

The API workers share one repository instance across threads. The repository
guards its tables with a single re-entrant lock, and any caller that reads the
reservation table and then writes based on the answer has to hold
`repo.lock()` across both steps.

## Tests

```
python -m unittest discover
```

Run it from this directory. The suite uses the standard library only.
