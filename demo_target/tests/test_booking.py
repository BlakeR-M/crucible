"""End-to-end and unit coverage for the bookings package.

Run from the project root with ``python -m unittest discover``.
"""

import unittest
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from bookings import (
    Actor,
    Booking,
    BookingStatus,
    BusinessHours,
    Customer,
    InMemoryRepository,
    Resource,
)
from bookings.api import paginate
from bookings.auth import has_permission, require_permission
from bookings.availability import day_slots, overlaps
from bookings.errors import (
    BillingError,
    InvalidBookingWindow,
    NotFound,
    OutsideBusinessHours,
    PermissionDenied,
    SlotUnavailable,
)
from bookings.invoicing import build_invoice
from bookings.money import format_money, money, percentage_of, sum_money
from bookings.service import BookingService

DAY = date(2026, 9, 14)
NEXT_DAY = DAY + timedelta(days=1)

OWNER = Actor(id="act_owner", name="Blake", role="owner")
AGENT = Actor(id="act_agent", name="Sam", role="agent")
VIEWER = Actor(id="act_viewer", name="Robin", role="viewer")

STUDIO_A = Resource(id="res_a", name="Studio A", hourly_rate=money("120.00"))
STUDIO_B = Resource(id="res_b", name="Studio B", hourly_rate=money("90.00"))

ACME = Customer(id="cus_acme", name="Acme Pty Ltd", billing_email="ap@acme.test")
BOREAL = Customer(id="cus_boreal", name="Boreal Studio", billing_email="ap@boreal.test")
NO_BILLING = Customer(id="cus_nb", name="Prospect Co", billing_email=None)


def at(hour: int, minute: int = 0, day: date = DAY) -> datetime:
    """Build a wall-clock datetime on ``day``."""
    return datetime.combine(day, time(hour, minute))


class TestAuth(unittest.TestCase):
    """Role levels gate every permission in the table."""

    def test_owner_carries_every_permission(self):
        self.assertTrue(has_permission(OWNER, "invoice:write"))
        self.assertTrue(has_permission(OWNER, "report:read"))

    def test_agent_may_book_but_not_invoice(self):
        self.assertTrue(has_permission(AGENT, "booking:write"))
        self.assertTrue(has_permission(AGENT, "booking:cancel"))
        self.assertFalse(has_permission(AGENT, "invoice:write"))

    def test_viewer_is_read_only(self):
        self.assertTrue(has_permission(VIEWER, "booking:read"))
        self.assertFalse(has_permission(VIEWER, "booking:write"))
        with self.assertRaises(PermissionDenied):
            require_permission(VIEWER, "booking:write")

    def test_unknown_role_falls_back_to_read_only(self):
        integration = Actor(id="act_bot", name="Partner bot", role="partner-bot")
        self.assertTrue(has_permission(integration, "booking:read"))
        self.assertFalse(has_permission(integration, "booking:write"))

    def test_unknown_permission_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            has_permission(OWNER, "booking:teleport")


class TestAvailability(unittest.TestCase):
    """Slot generation and interval arithmetic."""

    def test_hourly_slots_fill_the_trading_day(self):
        slots = day_slots(DAY)
        self.assertEqual(len(slots), 8)
        self.assertEqual(slots[0].start, at(9))
        self.assertEqual(slots[-1].end, at(17))

    def test_trailing_part_slot_is_dropped(self):
        slots = day_slots(DAY, slot_minutes=90)
        self.assertEqual(len(slots), 5)
        self.assertEqual(slots[-1].end, at(16, 30))

    def test_partial_overlap_conflicts(self):
        self.assertTrue(overlaps(at(10), at(12), at(11), at(13)))

    def test_contained_interval_conflicts(self):
        self.assertTrue(overlaps(at(10), at(14), at(11), at(12)))

    def test_separated_intervals_do_not_conflict(self):
        self.assertFalse(overlaps(at(9), at(10), at(11), at(12)))

    def test_business_hours_reject_early_starts(self):
        hours = BusinessHours()
        self.assertTrue(hours.contains(at(9), at(10)))
        self.assertTrue(hours.contains(at(16), at(17)))
        self.assertFalse(hours.contains(at(8), at(9)))
        self.assertFalse(hours.contains(at(16), at(9, 0, NEXT_DAY)))


class TestBookingService(unittest.TestCase):
    """The flows the HTTP handlers actually call."""

    def setUp(self):
        self.repo = InMemoryRepository()
        self.service = BookingService(
            self.repo,
            customers=[ACME, BOREAL, NO_BILLING],
            resources=[STUDIO_A, STUDIO_B],
        )

    def test_create_booking_reserves_the_slot_and_invoices_it(self):
        booking = self.service.create_booking(
            AGENT, ACME.id, STUDIO_A.id, at(10), at(12)
        )
        self.assertEqual(booking.status, BookingStatus.CONFIRMED)
        self.assertIsNotNone(booking.reservation_id)
        self.assertEqual(booking.duration_minutes, 120)

        invoices = self.repo.list_invoices(customer_id=ACME.id)
        self.assertEqual(len(invoices), 1)
        self.assertEqual(invoices[0].total, money("264.00"))
        self.assertEqual(invoices[0].number, "INV-00001")

    def test_overlapping_booking_is_rejected(self):
        self.service.create_booking(AGENT, ACME.id, STUDIO_A.id, at(10), at(12))
        with self.assertRaises(SlotUnavailable):
            self.service.create_booking(AGENT, BOREAL.id, STUDIO_A.id, at(11), at(13))

    def test_other_resource_is_still_bookable(self):
        self.service.create_booking(AGENT, ACME.id, STUDIO_A.id, at(10), at(12))
        other = self.service.create_booking(
            AGENT, BOREAL.id, STUDIO_B.id, at(10), at(12)
        )
        self.assertEqual(other.resource_id, STUDIO_B.id)

    def test_retry_with_the_same_idempotency_key_replays_the_booking(self):
        first = self.service.create_booking(
            AGENT, ACME.id, STUDIO_A.id, at(10), at(11), idempotency_key="req-1"
        )
        second = self.service.create_booking(
            AGENT, ACME.id, STUDIO_A.id, at(10), at(11), idempotency_key="req-1"
        )
        self.assertEqual(first.id, second.id)
        self.assertEqual(len(self.repo.active_reservations(STUDIO_A.id)), 1)

    def test_available_slots_drop_the_booked_window(self):
        self.service.create_booking(AGENT, ACME.id, STUDIO_A.id, at(10), at(11))
        starts = [slot.start for slot in self.service.available_slots(AGENT, STUDIO_A.id, DAY)]
        self.assertNotIn(at(10), starts)
        self.assertIn(at(14), starts)
        self.assertIn(at(15), starts)

    def test_cancelling_frees_the_slot_and_clears_the_cache(self):
        booking = self.service.create_booking(
            AGENT, ACME.id, STUDIO_A.id, at(10), at(11)
        )
        warm = [s.start for s in self.service.available_slots(AGENT, STUDIO_A.id, DAY)]
        self.assertNotIn(at(10), warm)

        invoice = self.service.cancel_booking(
            AGENT, booking.id, now=at(10, 0, DAY - timedelta(days=7))
        )
        self.assertEqual(booking.status, BookingStatus.CANCELLED)
        self.assertFalse(invoice.is_payable)

        after = [s.start for s in self.service.available_slots(AGENT, STUDIO_A.id, DAY)]
        self.assertIn(at(10), after)

    def test_rescheduling_moves_the_booking(self):
        booking = self.service.create_booking(
            AGENT, ACME.id, STUDIO_A.id, at(10), at(11)
        )
        moved = self.service.reschedule_booking(
            AGENT, booking.id, at(13, 0, NEXT_DAY), at(14, 0, NEXT_DAY)
        )
        self.assertEqual(moved.start, at(13, 0, NEXT_DAY))

        next_day = [
            s.start for s in self.service.available_slots(AGENT, STUDIO_A.id, NEXT_DAY)
        ]
        self.assertNotIn(at(13, 0, NEXT_DAY), next_day)

        same_day = [
            s.start for s in self.service.available_slots(AGENT, STUDIO_A.id, DAY)
        ]
        self.assertIn(at(10), same_day)

    def test_bookings_outside_trading_hours_are_rejected(self):
        with self.assertRaises(OutsideBusinessHours):
            self.service.create_booking(AGENT, ACME.id, STUDIO_A.id, at(8), at(9))

    def test_inverted_window_is_rejected(self):
        with self.assertRaises(InvalidBookingWindow):
            self.service.create_booking(AGENT, ACME.id, STUDIO_A.id, at(12), at(11))

    def test_viewer_cannot_create_a_booking(self):
        with self.assertRaises(PermissionDenied):
            self.service.create_booking(VIEWER, ACME.id, STUDIO_A.id, at(10), at(11))

    def test_unknown_resource_is_not_found(self):
        with self.assertRaises(NotFound):
            self.service.create_booking(AGENT, ACME.id, "res_missing", at(10), at(11))

    def test_customer_without_billing_email_cannot_be_invoiced(self):
        with self.assertRaises(BillingError):
            self.service.create_booking(
                AGENT, NO_BILLING.id, STUDIO_A.id, at(10), at(11)
            )

    def test_listing_bookings_is_paged_and_ordered(self):
        self.service.create_booking(AGENT, ACME.id, STUDIO_A.id, at(10), at(11))
        self.service.create_booking(AGENT, BOREAL.id, STUDIO_A.id, at(13), at(14))
        self.service.create_booking(AGENT, ACME.id, STUDIO_B.id, at(15), at(16))
        self.service.create_booking(AGENT, BOREAL.id, STUDIO_B.id, at(9), at(10))

        first = self.service.list_bookings(OWNER, page=1, page_size=2)
        self.assertEqual(first.total_items, 4)
        self.assertEqual(first.total_pages, 2)
        self.assertTrue(first.has_next)
        self.assertEqual([b.start for b in first.items], [at(9), at(10)])

        second = self.service.list_bookings(OWNER, page=2, page_size=2)
        self.assertFalse(second.has_next)
        self.assertEqual([b.start for b in second.items], [at(13), at(15)])

    def test_listing_filters_by_customer(self):
        self.service.create_booking(AGENT, ACME.id, STUDIO_A.id, at(10), at(11))
        self.service.create_booking(AGENT, BOREAL.id, STUDIO_A.id, at(13), at(14))

        page = self.service.list_bookings(OWNER, customer_id=ACME.id, page_size=5)
        self.assertEqual(page.total_items, 1)
        self.assertEqual(page.items[0].customer_id, ACME.id)


class TestInvoicing(unittest.TestCase):
    """Pricing a booking into a tax invoice."""

    def _booking(self, **overrides) -> Booking:
        fields = dict(
            id="bkg_test",
            customer_id=ACME.id,
            resource_id=STUDIO_A.id,
            start=at(10),
            end=at(12),
            status=BookingStatus.CONFIRMED,
        )
        fields.update(overrides)
        return Booking(**fields)

    def test_hourly_line_item_and_gst(self):
        invoice = build_invoice(self._booking(), ACME, STUDIO_A, "INV-00001")
        self.assertEqual(len(invoice.line_items), 1)
        self.assertEqual(invoice.line_items[0].amount, money("240.00"))
        self.assertEqual(invoice.subtotal, money("240.00"))
        self.assertEqual(invoice.tax, money("24.00"))
        self.assertEqual(invoice.total, money("264.00"))

    def test_setup_fee_is_a_second_line(self):
        invoice = build_invoice(
            self._booking(requires_setup=True), ACME, STUDIO_A, "INV-00002"
        )
        self.assertEqual(len(invoice.line_items), 2)
        self.assertEqual(invoice.subtotal, money("275.00"))
        self.assertEqual(invoice.total, money("302.50"))

    def test_discount_reduces_the_taxable_amount(self):
        invoice = build_invoice(
            self._booking(discount_percent=10.0), ACME, STUDIO_A, "INV-00003"
        )
        self.assertEqual(invoice.discount, money("24.00"))
        self.assertEqual(invoice.tax, money("21.60"))
        self.assertEqual(invoice.total, money("237.60"))

    def test_cancelled_booking_has_no_billable_time(self):
        invoice = build_invoice(
            self._booking(status=BookingStatus.CANCELLED), ACME, STUDIO_A, "INV-00004"
        )
        self.assertEqual(invoice.line_items, [])
        self.assertEqual(invoice.total, money("0.00"))
        self.assertFalse(invoice.is_payable)

    def test_missing_billing_email_blocks_the_invoice(self):
        with self.assertRaises(BillingError):
            build_invoice(self._booking(), NO_BILLING, STUDIO_A, "INV-00005")


class TestMoneyHelpers(unittest.TestCase):
    """Decimal helpers used by every price in the service."""

    def test_money_rounds_half_up(self):
        self.assertEqual(money("2.675"), Decimal("2.68"))
        self.assertEqual(money("2.674"), Decimal("2.67"))

    def test_sum_money_quantises_once(self):
        self.assertEqual(sum_money(["0.005", "0.005", "1.00"]), Decimal("1.01"))

    def test_percentage_of_rounds_half_up(self):
        self.assertEqual(percentage_of(Decimal("186.85"), 10), Decimal("18.69"))
        self.assertEqual(percentage_of(Decimal("200.00"), 10), Decimal("20.00"))

    def test_format_money(self):
        self.assertEqual(format_money(Decimal("1234.5")), "AUD 1,234.50")


class TestPagination(unittest.TestCase):
    """Page slicing for listing endpoints."""

    def setUp(self):
        self.rows = ["a", "b", "c", "d"]

    def test_first_page(self):
        page = paginate(self.rows, page=1, page_size=2)
        self.assertEqual(page.items, ["a", "b"])
        self.assertEqual(page.total_items, 4)
        self.assertEqual(page.total_pages, 2)
        self.assertTrue(page.has_next)

    def test_last_page(self):
        page = paginate(self.rows, page=2, page_size=2)
        self.assertEqual(page.items, ["c", "d"])
        self.assertFalse(page.has_next)

    def test_page_past_the_end_is_empty(self):
        page = paginate(self.rows, page=9, page_size=2)
        self.assertEqual(page.items, [])
        self.assertFalse(page.has_next)

    def test_single_page_holds_everything(self):
        page = paginate(self.rows, page=1, page_size=4)
        self.assertEqual(page.items, self.rows)
        self.assertEqual(page.total_pages, 1)
        self.assertFalse(page.has_next)

    def test_invalid_arguments_are_rejected(self):
        with self.assertRaises(ValueError):
            paginate(self.rows, page=0)
        with self.assertRaises(ValueError):
            paginate(self.rows, page=1, page_size=0)
        with self.assertRaises(ValueError):
            paginate(self.rows, page=1, page_size=1000)


if __name__ == "__main__":
    unittest.main()
