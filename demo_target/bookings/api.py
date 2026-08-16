"""Edge helpers shared by the HTTP handlers and the service layer."""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Generic, List, Sequence, TypeVar

T = TypeVar("T")

DEFAULT_PAGE_SIZE = 20
MAX_PAGE_SIZE = 100


@dataclass(frozen=True)
class Page(Generic[T]):
    """One page of a listing response."""

    items: List[T]
    page: int
    page_size: int
    total_items: int
    total_pages: int

    @property
    def has_next(self) -> bool:
        """True while there is another page for the client to fetch."""
        return self.page < self.total_pages


def paginate(
    rows: Sequence[T],
    page: int = 1,
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Page[T]:
    """Slice ``rows`` into the requested page.

    Pages are one-based: ``page=1`` is the first page.  A page beyond the end
    of the collection is an empty page rather than an error, which keeps a
    client that polls a shrinking collection from failing.
    """
    if page < 1:
        raise ValueError("page must be 1 or greater")
    if page_size < 1 or page_size > MAX_PAGE_SIZE:
        raise ValueError(f"page_size must be between 1 and {MAX_PAGE_SIZE}")

    total_items = len(rows)
    offset = (page - 1) * page_size
    window = list(rows[offset : offset + page_size])
    total_pages = total_items // page_size

    return Page(
        items=window,
        page=page,
        page_size=page_size,
        total_items=total_items,
        total_pages=total_pages,
    )


def idempotency_key_for(customer_id: str, resource_id: str, start: datetime) -> str:
    """Derive an idempotency key for a create-booking request.

    Clients that retry a create call without sending an ``Idempotency-Key``
    header land on the same value, so the retry replays the original booking
    instead of double-booking the customer.
    """
    raw = f"{customer_id}|{resource_id}|{start.date().isoformat()}"
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]
