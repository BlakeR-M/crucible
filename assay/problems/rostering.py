"""Staff rostering: who works which shift, under rules stated in plain English.

Chosen first because everyone has suffered it, and because it is the shape of
problem a language model fails at in the most useful way. Asked to build a
roster it will produce a complete, confident, well-formatted table in which one
person works a shift they said they could not, and nothing about the output
signals the error. That is exactly the failure a side-by-side demo makes
devastating, and exactly the failure a verifier catches without argument.

Five constraint families, each stated as a sentence a person would actually
write:

    availability   someone cannot work certain days
    max shifts     nobody works more than N shifts across the week
    coverage       every shift needs exactly one person
    seniority      certain shifts need someone from a named group
    separation     two people must not both be rostered on the same day

The solver is plain backtracking. The instances are deliberately small enough
that exhaustive search is instant, because the search was never the interesting
part: the interesting part is whether a model can turn the paragraph into the
right answer at all.
"""

from __future__ import annotations

import random

NL = chr(10)

from .base import IMPOSSIBLE, Klass, Problem, humanise, pick_names

DAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]
SHIFTS = ["morning", "evening"]

SIZES = {
    "easy": (4, 3),      # staff, days
    "medium": (5, 4),
    "hard": (6, 5),
}


def _slots(days: list) -> list:
    return [(d, s) for d in days for s in SHIFTS]


def generate(rng: random.Random, difficulty: str = "medium") -> Problem:
    staff_count, day_count = SIZES.get(difficulty, SIZES["medium"])
    names = pick_names(rng, staff_count)
    days = DAYS[:day_count]
    slots = _slots(days)

    # Availability: each person loses a day or two.
    unavailable = {}
    for name in names:
        blocked = rng.sample(days, rng.choice([0, 1, 1, 2]))
        if blocked:
            unavailable[name] = sorted(blocked, key=days.index)

    # A cap that binds: with this many slots, the cap has to be tight enough to
    # matter but loose enough that a solution usually exists.
    max_shifts = max(2, -(-len(slots) // staff_count) + rng.choice([0, 1]))

    seniors = sorted(rng.sample(names, max(1, staff_count // 2)))
    senior_shifts = rng.choice([[], ["evening"]])

    # A pair who cannot share a day, used about half the time.
    apart = []
    if staff_count >= 4 and rng.random() < 0.5:
        apart = sorted(rng.sample(names, 2))

    spec = {
        "staff": names, "days": days, "shifts": SHIFTS, "slots": slots,
        "unavailable": unavailable, "max_shifts": max_shifts,
        "seniors": seniors, "senior_shifts": senior_shifts, "apart": apart,
    }

    problem = Problem(
        id=f"roster-{rng.getrandbits(24):06x}",
        klass="rostering",
        prose=render(spec),
        spec=spec,
        answer_format=(
            "one line per shift, written exactly as "
            "'Monday morning: Name', in the order the shifts are listed. "
            f"If no valid roster exists, reply with the single word {IMPOSSIBLE}."
        ),
        difficulty=difficulty,
    )
    solution = solve(problem)
    return Problem(**{**problem.__dict__, "impossible": solution is None})


def render(spec: dict) -> str:
    """The problem as a person would write it, with nothing formal in sight."""
    lines = [
        f"I need a roster for {humanise(spec['staff'])}.",
        f"We open {humanise(spec['days'])}, and each of those days has a "
        f"morning shift and an evening shift.",
        "Exactly one person works each shift.",
    ]
    for name, blocked in spec["unavailable"].items():
        lines.append(f"{name} can't work {humanise(blocked)}.")
    lines.append(f"Nobody should work more than {spec['max_shifts']} shifts "
                 f"in total.")
    if spec["senior_shifts"]:
        lines.append(
            f"{humanise(spec['seniors'])} are the senior staff, and every "
            f"{humanise(spec['senior_shifts'])} shift has to be covered by one "
            f"of them.")
    if spec["apart"]:
        a, b = spec["apart"]
        lines.append(f"{a} and {b} can't both be working on the same day.")
    return " ".join(lines)


# ------------------------------------------------------------------- solving

def solve(problem: Problem):
    """First valid roster found, or None. Plain backtracking over the slots."""
    spec = problem.spec
    slots = [tuple(s) for s in spec["slots"]]
    staff = spec["staff"]
    unavailable = {k: set(v) for k, v in spec["unavailable"].items()}
    senior_shifts = set(spec["senior_shifts"])
    seniors = set(spec["seniors"])
    apart = set(spec["apart"])

    assignment: dict = {}
    counts = {n: 0 for n in staff}
    per_day: dict = {d: set() for d in spec["days"]}

    def ok(name: str, day: str, shift: str) -> bool:
        if day in unavailable.get(name, ()):
            return False
        if counts[name] >= spec["max_shifts"]:
            return False
        if shift in senior_shifts and name not in seniors:
            return False
        if apart and name in apart:
            other = (apart - {name}).pop()
            if other in per_day[day]:
                return False
        return True

    def step(index: int) -> bool:
        if index == len(slots):
            return True
        day, shift = slots[index]
        for name in staff:
            if not ok(name, day, shift):
                continue
            assignment[f"{day} {shift}"] = name
            counts[name] += 1
            per_day[day].add(name)
            if step(index + 1):
                return True
            del assignment[f"{day} {shift}"]
            counts[name] -= 1
            # Only drop the person from the day when this was their last shift
            # on it. Removing unconditionally lets a later branch place someone
            # beside a person the rules keep apart.
            if not any(assignment.get(f"{day} {s}") == name for s in SHIFTS):
                per_day[day].discard(name)
        return False

    return dict(assignment) if step(0) else None


# ----------------------------------------------------------------- verifying

def verify(problem: Problem, candidate) -> "object":
    """Check a roster against the rules, never against the reference answer."""
    from .base import Verdict

    spec = problem.spec
    if problem.impossible:
        if candidate is None:
            return Verdict(True)
        return Verdict(False, "a valid roster was given for a problem that has "
                              "none", "impossible")
    if candidate is None:
        return Verdict(False, "said impossible, but a valid roster exists",
                       "impossible")
    if not isinstance(candidate, dict):
        return Verdict(False, f"expected a mapping, got {type(candidate).__name__}",
                       "shape")

    slots = [f"{d} {s}" for d, s in (tuple(x) for x in spec["slots"])]
    missing = [s for s in slots if s not in candidate]
    if missing:
        return Verdict(False, f"no one rostered for {missing[0]}", "coverage")
    extra = [s for s in candidate if s not in slots]
    if extra:
        return Verdict(False, f"rostered a shift that does not exist: {extra[0]}",
                       "coverage")

    counts: dict = {}
    per_day: dict = {}
    for slot in slots:
        name = candidate[slot]
        if name not in spec["staff"]:
            return Verdict(False, f"{name!r} is not one of the staff", "staff")
        day, shift = slot.rsplit(" ", 1)
        if day in spec["unavailable"].get(name, ()):
            return Verdict(False, f"{name} is rostered on {day} but cannot work "
                                  f"that day", "availability")
        if spec["senior_shifts"] and shift in spec["senior_shifts"]:
            if name not in spec["seniors"]:
                return Verdict(False, f"{name} is not senior but covers the "
                                      f"{day} {shift} shift", "seniority")
        counts[name] = counts.get(name, 0) + 1
        per_day.setdefault(day, set()).add(name)

    for name, count in counts.items():
        if count > spec["max_shifts"]:
            return Verdict(False, f"{name} works {count} shifts, more than the "
                                  f"limit of {spec['max_shifts']}", "max_shifts")

    if spec["apart"]:
        a, b = spec["apart"]
        for day, names in per_day.items():
            if a in names and b in names:
                return Verdict(False, f"{a} and {b} both work on {day}",
                               "separation")
    return Verdict(True)


# ------------------------------------------------------------------- parsing

def parse(text: str):
    """Read a roster out of a model's reply, or None for an impossible claim.

    Deliberately generous about surrounding prose and formatting, because the
    measurement is meant to be about reasoning rather than about whether the
    model obeyed a layout instruction. It is strict about the content: a line
    only counts when it names a real day and shift.
    """
    import re

    if IMPOSSIBLE.lower() in text.lower():
        return None
    found = {}
    pattern = re.compile(
        r"\b(" + "|".join(DAYS) + r")\b[^\S\n]*(" + "|".join(SHIFTS) + r")\b"
        # Markdown emphasis may sit on either side of the separator, not
        # only in front of the name. A model writing "**Monday morning**:
        # Ines" puts the asterisks between the shift and the colon, and a
        # pattern tolerating them only before the name read none of it.
        r"[*_\s]*[:\-–][*_\s]*([A-Z][a-z]+)",
        re.IGNORECASE)
    for day, shift, name in pattern.findall(text):
        key = f"{day.capitalize()} {shift.lower()}"
        found.setdefault(key, name.capitalize())
    return found or {}


def corrupt(problem: Problem, solution, rng) -> list:
    """Answers that certainly break a named rule.

    Every one is chosen so the violation is guaranteed rather than likely. A
    swap of two shifts is neither: in a roster it usually yields a second valid
    answer, which is why the first version of this test reported the verifier
    as broken when the verifier was right.
    """
    spec = problem.spec
    out = []
    slots = [f"{d} {s}" for d, s in (tuple(x) for x in spec["slots"])]

    dropped = dict(solution)
    dropped.pop(slots[0])
    out.append(("coverage", dropped))

    stranger = dict(solution)
    stranger[slots[0]] = "Nobody"
    out.append(("staff", stranger))

    # One person on every shift. There are always more shifts than the cap,
    # so this breaks max_shifts whatever else it breaks.
    hog = {slot: spec["staff"][0] for slot in slots}
    out.append(("max_shifts", hog))

    for name, blocked in spec["unavailable"].items():
        target = f"{blocked[0]} morning"
        if target in slots:
            bad = dict(solution)
            bad[target] = name
            out.append(("availability", bad))
            break

    if spec["senior_shifts"]:
        junior = next((n for n in spec["staff"] if n not in spec["seniors"]), None)
        if junior:
            for slot in slots:
                if slot.rsplit(" ", 1)[1] in spec["senior_shifts"]:
                    bad = dict(solution)
                    bad[slot] = junior
                    out.append(("seniority", bad))
                    break

    if spec["apart"]:
        a, b = spec["apart"]
        day = spec["days"][0]
        bad = dict(solution)
        bad[f"{day} morning"] = a
        bad[f"{day} evening"] = b
        out.append(("separation", bad))
    return out


def formats(problem, solution) -> list:
    """Correct answers written the way models actually write them."""
    slots = [f"{d} {s}" for d, s in (tuple(x) for x in problem.spec["slots"])]
    plain = NL.join(f"{s}: {solution[s]}" for s in slots)
    bold = NL.join(f"**{s}**: {solution[s]}" for s in slots)
    dashed = NL.join(f"- {s} - {solution[s]}" for s in slots)
    chatty = "Here is a roster that works:" + NL + NL + plain + NL + NL + "Every rule is met."
    return [plain, bold, dashed, chatty]


KLASS = Klass(
    name="rostering",
    generate=generate, solve=solve, verify=verify, parse=parse,
    corrupt=corrupt, formats=formats,
    blurb="staff shifts under availability, cap, seniority and separation rules",
)
