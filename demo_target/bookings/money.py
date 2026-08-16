"""Money helpers.

Every monetary amount that crosses a module boundary in this service is a
:class:`decimal.Decimal` quantised to cents with ``ROUND_HALF_UP``.  Australian
tax invoices are rounded half up, so ``ROUND_HALF_EVEN`` (Python's default for
``round``) is the wrong tool and binary floats are never a safe intermediate.
"""

from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
from typing import Iterable, Union

Numeric = Union[int, str, float, Decimal]

CENTS = Decimal("0.01")
ZERO = Decimal("0.00")


def to_decimal(value: Numeric) -> Decimal:
    """Convert ``value`` to a :class:`Decimal` without going through binary float.

    ``Decimal(0.1)`` keeps the binary expansion of the float; ``Decimal("0.1")``
    keeps the decimal value the user typed, so every conversion goes via ``str``.
    """
    if isinstance(value, Decimal):
        return value
    return Decimal(str(value))


def money(value: Numeric) -> Decimal:
    """Return ``value`` as an amount in cents, rounded half up."""
    return to_decimal(value).quantize(CENTS, rounding=ROUND_HALF_UP)


def sum_money(values: Iterable[Numeric]) -> Decimal:
    """Sum ``values`` and quantise the result once, at the end."""
    total = ZERO
    for value in values:
        total += to_decimal(value)
    return money(total)


def percentage_of(amount: Numeric, percent: Numeric) -> Decimal:
    """Return ``percent`` percent of ``amount``, rounded half up to cents."""
    return money(to_decimal(amount) * to_decimal(percent) / Decimal(100))


def format_money(amount: Numeric, currency: str = "AUD") -> str:
    """Render ``amount`` for a customer-facing document."""
    return f"{currency} {money(amount):,.2f}"
