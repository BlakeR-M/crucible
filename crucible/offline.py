"""A model that costs nothing, so the rest of the system can be worked on.

Every part of Crucible except the model is free to run: the policy, the ledger,
the budget, the toolbox, the orchestrator, the boundary probe and the whole web
layer. Only the completions cost money, and during front-end work they are also
the least interesting part, because a run exercised twenty times to get a
layout right is twenty runs paying full price for output nobody reads.

So this stands in for the provider. It is not a mock in the testing sense: the
real orchestrator runs, real agents take real turns, the real policy refuses
real attempts, and the ledger it writes verifies like any other. What changes is
that the replies are written here instead of bought, which means a run is
deterministic, finishes in seconds, and costs nothing.

The replies are modelled on the demo target's actual planted defects, so an
offline run looks like a real one rather than like filler. Three of the claims
are written to be refutable and are refuted, because a demo of this system that
never destroys anything would be showing the wrong thing.

    CRUCIBLE_OFFLINE=1 python main.py
"""

from __future__ import annotations

import json
import re
import time

from .providers import Completion, Tier

# A short pause per call. Without it an offline run finishes faster than the
# browser can paint and the interface cannot be judged, which is the one thing
# this mode exists for.
STEP_SECONDS = 0.35

LANES = [
    {"name": "Money handling and rounding",
     "brief": "decimal versus float, discounts, tax, invoice totals",
     "files": ["bookings/invoicing.py", "bookings/money.py"]},
    {"name": "Availability and overlap rules",
     "brief": "half-open intervals, boundary conditions, timezone handling",
     "files": ["bookings/availability.py"]},
    {"name": "Access control and validation",
     "brief": "role levels, permission checks, request validation",
     "files": ["bookings/auth.py", "bookings/api.py"]},
    {"name": "Persistence and ordering",
     "brief": "partial failures, rollback, cache invalidation, races",
     "files": ["bookings/service.py", "bookings/repository.py"]},
]

# Modelled on the real planted defects. Each carries the lane it belongs to so
# the tree puts it under the hunter that would have raised it.
CLAIMS = {
    "Money handling and rounding": [
        {"title": "Mutable default for adjustments accumulates across calls",
         "file": "bookings/invoicing.py", "line": 83, "severity": "high",
         "summary": "build_invoice takes adjustments as a mutable default list "
                    "and appends to it, so a late-cancellation fee added on one "
                    "call is still present on the next call that omits the "
                    "argument.",
         "failure_scenario": "build_invoice(booking_a) where booking_a is "
                             "CANCELLED_LATE appends a fee to the default list. "
                             "build_invoice(booking_b) with no adjustments then "
                             "inherits booking_a's fee and overcharges.",
         "evidence": "def build_invoice(booking, ..., adjustments=[])"},
        {"title": "Discount converts Decimal to float before rounding",
         "file": "bookings/invoicing.py", "line": 70, "severity": "high",
         "summary": "apply_discount casts the Decimal subtotal to float and "
                    "uses round(), which is banker's rounding, so money is both "
                    "imprecise and rounded the wrong way at the half.",
         "failure_scenario": "subtotal Decimal('19.99') with a 12.5 per cent "
                             "discount rounds to 17.49 where correct "
                             "ROUND_HALF_UP gives 17.50.",
         "evidence": "return round(float(subtotal) * (1 - pct / 100), 2)"},
        {"title": "Tax applied before the discount on cancelled bookings",
         "file": "bookings/invoicing.py", "line": 118, "severity": "medium",
         "summary": "compute_tax is called on the pre-discount subtotal in the "
                    "cancellation path, so tax is charged on money the customer "
                    "was never billed.",
         "failure_scenario": "a cancelled booking with a 50 per cent discount "
                             "is taxed on the full amount.",
         "evidence": "tax = compute_tax(subtotal)  # not taxable"},
    ],
    "Availability and overlap rules": [
        {"title": "Touching intervals are treated as overlapping",
         "file": "bookings/availability.py", "line": 64, "severity": "high",
         "summary": "overlaps() uses inclusive comparisons while the module "
                    "documents intervals as half-open, so a booking that ends "
                    "exactly when another begins is reported as a conflict.",
         "failure_scenario": "an existing reservation 10:00 to 11:00 blocks a "
                             "new one from 11:00 to 12:00, which is valid.",
         "evidence": "return a_start <= b_end and b_start <= a_end"},
        {"title": "Naive and aware datetimes compared without normalising",
         "file": "bookings/availability.py", "line": 22, "severity": "medium",
         "summary": "no check that both datetimes carry the same awareness, so "
                    "a mixed pair raises rather than being rejected cleanly.",
         "failure_scenario": "a request carrying a timezone-aware start against "
                             "a stored naive end raises TypeError from the "
                             "comparison rather than returning a validation "
                             "error.",
         "evidence": "if start < other.end:"},
    ],
    "Access control and validation": [
        {"title": "A role level of zero is read as no role at all",
         "file": "bookings/auth.py", "line": 43, "severity": "high",
         "summary": "level_for uses `or DEFAULT_LEVEL` on a dict lookup, so the "
                    "suspended role, whose level is 0, is falsy and falls back "
                    "to the default. A suspended account is silently promoted.",
         "failure_scenario": "Actor(role='suspended') has ROLE_LEVELS 0; "
                             "level_for returns DEFAULT_LEVEL instead, and the "
                             "actor passes checks intended to exclude them.",
         "evidence": "return ROLE_LEVELS.get(role) or DEFAULT_LEVEL"},
        {"title": "total_pages computed with floor division",
         "file": "bookings/api.py", "line": 51, "severity": "medium",
         "summary": "paginate divides with // so a partial final page is "
                    "dropped from the count, and has_next is wrong on the "
                    "second-to-last page.",
         "failure_scenario": "21 items at page_size 20 reports total_pages 1, "
                             "so a client never requests page 2 and loses an "
                             "item.",
         "evidence": "total_pages = total_items // page_size"},
        {"title": "Idempotency key omits the time of day",
         "file": "bookings/api.py", "line": 69, "severity": "high",
         "summary": "the key hashes customer, resource and the calendar date "
                    "only, so two genuinely different bookings on one day "
                    "collide and the second is silently treated as a replay.",
         "failure_scenario": "the same customer books the same resource at "
                             "09:00 and at 14:00; the second request returns "
                             "the first booking and the 14:00 slot is never "
                             "reserved.",
         "evidence": "key = sha256(f'{customer}:{resource}:{start.date()}')"},
        {"title": "Permission errors leak the internal rule name",
         "file": "bookings/auth.py", "line": 77, "severity": "low",
         "summary": "PermissionDenied is raised with the internal predicate "
                    "name in its message.",
         "failure_scenario": "a denied caller sees the rule identifier in the "
                             "API response.",
         "evidence": "raise PermissionDenied(f'failed {rule.__name__}')"},
    ],
    "Persistence and ordering": [
        {"title": "Slot reserved and booking written before the invoice can fail",
         "file": "bookings/service.py", "line": 146, "severity": "high",
         "summary": "create_booking reserves the slot and inserts the booking, "
                    "then builds the invoice, and a billing failure leaves the "
                    "slot held against a booking nobody is charged for.",
         "failure_scenario": "build_invoice raises BillingError after "
                             "reserve_slot has succeeded; the slot stays "
                             "reserved and no rollback runs.",
         "evidence": "repo.reserve_slot(...); repo.insert(booking); "
                     "invoice = build_invoice(...)"},
        {"title": "Availability checked and reserved without holding the lock",
         "file": "bookings/service.py", "line": 126, "severity": "high",
         "summary": "is_slot_free and reserve_slot are separate calls with no "
                    "lock across them, so two concurrent requests both observe "
                    "a free slot and both reserve it.",
         "failure_scenario": "two threads call create_booking for the same "
                             "resource and overlapping times; both pass the "
                             "check and both reserve, double-booking it.",
         "evidence": "if repo.is_slot_free(...): repo.reserve_slot(...)"},
        {"title": "Reschedule invalidates the cache for the new day only",
         "file": "bookings/service.py", "line": 234, "severity": "high",
         "summary": "moving a booking across days clears the availability cache "
                    "for the destination date and leaves the origin date stale, "
                    "so the freed slot still reads as taken.",
         "failure_scenario": "a booking moves from the 3rd to the 5th; "
                             "available_slots for the 3rd keeps returning the "
                             "old reservation until the cache expires.",
         "evidence": "cache.invalidate(new_start.date())"},
        {"title": "Repository returns stored objects by reference",
         "file": "bookings/repository.py", "line": 88, "severity": "medium",
         "summary": "get() hands back the stored instance rather than a copy, "
                    "so a caller mutating the result changes the store.",
         "failure_scenario": "a caller adjusts booking.status on a returned "
                             "object and the change is visible to every "
                             "subsequent reader without a write.",
         "evidence": "return self._rows[key]"},
    ],
}

# Which claims are refuted, by title. Chosen so a run destroys a meaningful
# share of its own output, since that is the number the whole product is about.
REFUTED = {
    "Tax applied before the discount on cancelled bookings":
        "The cancellation path calls compute_tax on `taxable`, not `subtotal`. "
        "The claim quotes a line that does not exist in this file.",
    "Naive and aware datetimes compared without normalising":
        "Every entry point normalises through _as_utc before this is reached, "
        "so a naive datetime cannot arrive here from any public caller.",
    "Permission errors leak the internal rule name":
        "The message is built but the API layer replaces it with a generic "
        "string before it reaches a response, so nothing leaks.",
    "Repository returns stored objects by reference":
        "get() returns dataclasses.replace(row), which is a copy. The claim "
        "misquotes the line.",
}


def _wants(system: str, needle: str) -> bool:
    return needle.lower() in (system or "").lower()


class OfflineProvider:
    """A stand-in with the provider's interface and none of its cost.

    The orchestrator cannot tell the difference: it asks for a completion at a
    tier and receives one. Which reply comes back is decided by reading the
    system prompt, because that is what identifies the seat the caller is
    sitting in, and the same agent loop drives every one of them.
    """

    name = "offline"

    def __init__(self, *, pace: float = STEP_SECONDS):
        self.pace = pace
        self.calls = 0
        self._issued: dict[str, int] = {}

    def complete(self, system: str, user: str, tier, budget,
                 *, max_output: int = 2000, reasoning: str = "low") -> Completion:
        self.calls += 1
        if self.pace:
            time.sleep(self.pace)

        body = self._reply(system, user, tier)
        text = json.dumps(body)
        # Booked at zero. The ceiling is still checked by the caller, so a run
        # behaves the same way; it simply never spends.
        budget.settle("offline", 0.0, len(user) // 4, len(text) // 4)
        return Completion(text, "offline", len(user) // 4, len(text) // 4,
                          0.0, self.pace)

    # ---------------------------------------------------------------- seats

    def _reply(self, system: str, user: str, tier) -> dict:
        if _wants(system, "you divide a code review"):
            return {"lanes": LANES}
        if _wants(system, "your lane is the boundary"):
            return {"done": True,
                    "summary": "Every attempt was refused at the tool "
                               "boundary, and the control read inside the "
                               "workspace succeeded, so the limits are real "
                               "rather than the tools being broken."}
        if _wants(system, "trying to refute"):
            return self._verdict(user)
        if _wants(system, "hunting for real defects"):
            return self._hunt(user)
        if _wants(system, "you are the agent that runs crucible"):
            return self._chat(user)
        return {"done": True, "findings": []}

    def _hunt(self, user: str) -> dict:
        lane = ""
        for line in user.splitlines():
            if line.startswith("YOUR LANE:"):
                lane = line.split(":", 1)[1].strip()
        claims = CLAIMS.get(lane, [])

        # First turn reads a file, so the tree shows the agent working and the
        # ledger records a real tool call. After that it reports.
        if ">>> you called" not in user:
            first = claims[0]["file"] if claims else "bookings/service.py"
            return {"tool": "read_file", "args": {"path": first}}
        return {"done": True, "findings": claims}

    def _verdict(self, user: str) -> dict:
        title = ""
        for line in user.splitlines():
            if line.strip().startswith("title:"):
                title = line.split(":", 1)[1].strip()
        if title in REFUTED:
            return {"done": True, "refuted": True, "confidence": "high",
                    "concrete_failure": "",
                    "reasoning": REFUTED[title]}
        return {"done": True, "refuted": False, "confidence": "high",
                "concrete_failure": "Reproduced against the file as committed.",
                "reasoning": "Read the code and could not refute it. The line "
                             "is quoted accurately and the failure follows from "
                             "it with no upstream guard in the way."}

    def _chat(self, user: str) -> dict:
        """A conversational reply that still exercises the tool surface.

        It answers by calling a tool first wherever a real agent would, so the
        offline interface shows the same trail of actions rather than a bare
        sentence appearing from nowhere.
        """
        asked = (user or "").lower()
        turn = self._issued.get("chat", 0)
        self._issued["chat"] = turn + 1
        already = ">>> you called" in user or "REFUSED BY POLICY" in user

        if not already:
            if re.search(r"\b(run|start|review|go on|do it)\b", asked):
                key = ("money" if "money" in asked else
                       "auth" if ("auth" in asked or "access" in asked) else
                       "concurrency" if ("race" in asked or "concurren" in asked)
                       else "full")
                return {"tool": "start_review", "args": {"task_key": key}}
            if "polic" in asked or "allowed" in asked or "rules" in asked:
                return {"tool": "policy_summary", "args": {}}
            if "ledger" in asked or "verify" in asked or "chain" in asked:
                return {"tool": "ledger_summary", "args": {}}
            if "miss" in asked or "planted" in asked:
                return {"tool": "missed_defects", "args": {}}
            if "finding" in asked or "why" in asked or "thrown out" in asked:
                return {"tool": "list_findings", "args": {"which": "all"}}
            if "status" in asked or "how is" in asked or "going" in asked:
                return {"tool": "run_status", "args": {}}

        return {"answer":
                "This deployment is running without a paid model, so I am "
                "answering from the run itself rather than reasoning about it. "
                "Everything on the right is real: the agents took real turns, "
                "the policy refused real attempts, and the ledger verifies. "
                "Ask me to start a review, or about the policy, the ledger, or "
                "which planted defects were missed."}


def enabled() -> bool:
    """Whether this deployment should run without a paid model."""
    import os

    return os.environ.get("CRUCIBLE_OFFLINE", "").strip().lower() in (
        "1", "true", "yes", "on")
