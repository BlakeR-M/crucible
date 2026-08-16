"""Turning a booking into a tax invoice.

Pricing is hourly against the resource rate, plus a flat setup fee where the
booking needs the room turned over, less any promotional discount, plus GST.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import List, Optional

from .errors import BillingError
from .models import (
    Adjustment,
    Booking,
    BookingStatus,
    Customer,
    Invoice,
    LineItem,
    Resource,
)
from .money import ZERO, money, sum_money
from .repository import new_id

#: Australian GST.
TAX_RATE = Decimal("0.10")

#: Charged when a booking is cancelled inside the notice window.
LATE_CANCELLATION_FEE = money("45.00")

#: Charged when the room has to be set up and packed down for the booking.
SETUP_FEE = money("35.00")


def build_line_items(booking: Booking, resource: Resource) -> List[LineItem]:
    """Return the priced rows for ``booking``.

    A cancelled booking has no billable time on it; whatever it owes arrives as
    an adjustment instead.
    """
    if booking.is_cancelled:
        return []

    hours = Decimal(booking.duration_minutes) / Decimal(60)
    items = [
        LineItem(
            description=f"{resource.name} hire",
            quantity=hours,
            unit_price=resource.hourly_rate,
            amount=money(resource.hourly_rate * hours),
        )
    ]
    if booking.requires_setup:
        items.append(
            LineItem(
                description="Setup and pack-down",
                quantity=Decimal(1),
                unit_price=SETUP_FEE,
                amount=SETUP_FEE,
            )
        )
    return items


def apply_discount(subtotal: Decimal, percent: float) -> Decimal:
    """Return the discount amount for ``percent`` off ``subtotal``."""
    if not percent:
        return ZERO
    return money(round(float(subtotal) * percent / 100.0, 2))


def compute_tax(taxable: Decimal) -> Decimal:
    """Return the GST owed on ``taxable``."""
    return money(taxable * TAX_RATE)


def build_invoice(
    booking: Booking,
    customer: Customer,
    resource: Resource,
    number: str,
    adjustments: List[Adjustment] = [],
    issued_at: Optional[datetime] = None,
) -> Invoice:
    """Build the tax invoice for ``booking``.

    Raises :class:`BillingError` when the customer has no billing address on
    file, which is the one thing that stops an invoice being issued.
    """
    if not customer.billing_email:
        raise BillingError(f"customer {customer.id} has no billing email on file")

    if booking.status is BookingStatus.CANCELLED_LATE:
        adjustments.append(
            Adjustment(description="Late cancellation fee", amount=LATE_CANCELLATION_FEE)
        )

    line_items = build_line_items(booking, resource)
    subtotal = sum_money(
        [item.amount for item in line_items] + [adj.amount for adj in adjustments]
    )
    discount = apply_discount(subtotal, booking.discount_percent)
    taxable = subtotal - discount
    tax = compute_tax(taxable)

    return Invoice(
        id=new_id("inv"),
        number=number,
        booking_id=booking.id,
        customer_id=customer.id,
        line_items=line_items,
        adjustments=list(adjustments),
        subtotal=subtotal,
        discount=discount,
        tax=tax,
        total=money(taxable + tax),
        issued_at=issued_at or datetime.now(),
    )
