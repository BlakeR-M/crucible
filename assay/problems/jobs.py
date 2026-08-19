"""A tradie's day: what order to do the jobs in so every window is met.

Ordering rather than assignment, which is what makes this class earn a place
next to the other two. Rostering asks who, seating asks where, this asks when,
and a model can be competent at one and hopeless at another.

It is also the class with arithmetic in it, and the arithmetic is the trap. A
model will produce a plausible order and then quietly get the clock wrong:
finish a ninety-minute job at the wrong time, forget the travel between two
addresses, or start something five minutes before its window opens. The order
looks sensible and the schedule is impossible, which is exactly the failure a
verifier settles in one line and a reader recognises immediately.

    duration     each job takes a stated number of minutes
    window       each job has an earliest start and a latest finish
    travel       a fixed number of minutes between any two jobs
    one at once  a single person, so nothing overlaps

Times are minutes from 8am throughout, which keeps the arithmetic honest and the
prose readable.
"""

from __future__ import annotations

import random

NL = chr(10)
from itertools import permutations

from .base import IMPOSSIBLE, Klass, Problem, humanise

SIZES = {"easy": 4, "medium": 5, "hard": 6}

# Display name paired with the one word that identifies it. Models drop the
# article, lowercase things, and re-word the rest, so matching on the full
# display name finds nothing: the first version of this parser scored a run at
# zero because the model wrote "warehouse" where the name was "the warehouse".
# Each key is unique and survives being rewritten.
PLACES = [
    ("the Hartley place", "hartley"),
    ("the cafe on Bunda Street", "bunda"),
    ("the Nguyen house", "nguyen"),
    ("the clinic in Braddon", "braddon"),
    ("the warehouse", "warehouse"),
    ("the school hall", "school hall"),
    ("the Okafor unit", "okafor"),
    ("the bakery", "bakery"),
    ("the gym", "gym"),
    ("the dentist", "dentist"),
]
NAMES = [name for name, _ in PLACES]
KEY_OF = {name: key for name, key in PLACES}

DAY_START = 0            # 8:00am
DAY_END = 9 * 60         # 5:00pm


def clock(minutes: int) -> str:
    """Minutes from 8am as a plain time, for prose a person would write."""
    total = 8 * 60 + minutes
    hour, minute = divmod(total, 60)
    suffix = "am" if hour < 12 else "pm"
    display = hour if 1 <= hour <= 12 else abs(hour - 12) or 12
    return f"{display}:{minute:02d}{suffix}"


def generate(rng: random.Random, difficulty: str = "medium") -> Problem:
    count = SIZES.get(difficulty, SIZES["medium"])
    places = rng.sample(NAMES, count)
    travel = rng.choice([10, 15, 20])

    # Build a feasible day first, then describe it. Windows drawn at random
    # would nearly always be impossible, and a class where almost every
    # instance is impossible measures nothing.
    order = list(places)
    rng.shuffle(order)
    durations = {p: rng.choice([30, 45, 60, 90]) for p in places}
    windows = {}
    clock_now = rng.choice([0, 15, 30])
    for index, place in enumerate(order):
        if index:
            clock_now += travel
        start = clock_now
        finish = start + durations[place]
        slack_before = rng.choice([0, 15, 30, 45])
        slack_after = rng.choice([0, 15, 30, 60])
        windows[place] = [max(DAY_START, start - slack_before),
                          min(DAY_END, finish + slack_after)]
        clock_now = finish

    # Then tighten one window at random, which is what turns some instances
    # impossible without making the whole set impossible.
    if rng.random() < 0.25:
        victim = rng.choice(places)
        low, high = windows[victim]
        windows[victim] = [low, max(low + durations[victim] - 15, low)]

    spec = {
        "jobs": places, "durations": durations, "windows": windows,
        "travel": travel, "day_end": DAY_END,
    }
    problem = Problem(
        id=f"jobs-{rng.getrandbits(24):06x}",
        klass="jobs",
        prose=render(spec),
        spec=spec,
        answer_format=(
            "the job names in the order you would do them, one per line, "
            "numbered 1., 2., 3. and so on. "
            f"If no order works, reply with the single word {IMPOSSIBLE}."
        ),
        difficulty=difficulty,
    )
    solution = solve(problem)
    return Problem(**{**problem.__dict__, "impossible": solution is None})


def render(spec: dict) -> str:
    lines = [
        f"I've got {len(spec['jobs'])} jobs today and I start at 8:00am.",
        f"It takes {spec['travel']} minutes to get between any two of them, "
        f"and I can only be at one place at a time.",
    ]
    for job in spec["jobs"]:
        low, high = spec["windows"][job]
        lines.append(
            f"{job.capitalize()} takes {spec['durations'][job]} minutes, and it "
            f"can't start before {clock(low)} or finish after {clock(high)}.")
    lines.append("What order should I do them in?")
    return " ".join(lines)


# ------------------------------------------------------------------- solving

def _schedule(spec: dict, order: list):
    """Run an order as early as possible. Returns start times, or None."""
    starts = {}
    now = DAY_START
    for index, job in enumerate(order):
        if index:
            now += spec["travel"]
        low, high = spec["windows"][job]
        start = max(now, low)
        finish = start + spec["durations"][job]
        if finish > high or finish > spec["day_end"]:
            return None
        starts[job] = start
        now = finish
    return starts


def solve(problem: Problem):
    """Exhaustive over orders. Six jobs is 720 permutations, which is nothing."""
    spec = problem.spec
    for order in permutations(spec["jobs"]):
        if _schedule(spec, list(order)) is not None:
            return list(order)
    return None


# ----------------------------------------------------------------- verifying

def verify(problem: Problem, candidate) -> object:
    """Check the order can actually be worked, scheduling it as early as possible.

    Earliest-possible is not a convenience, it is the right test: if a job
    cannot fit when started as early as its window and the previous job allow,
    it cannot fit at all, because every later start is worse. So an order that
    fails this check has no feasible timing anywhere.
    """
    from .base import Verdict

    spec = problem.spec
    if problem.impossible:
        if candidate is None:
            return Verdict(True)
        return Verdict(False, "an order was given for a day that cannot be "
                              "worked", "impossible")
    if candidate is None:
        return Verdict(False, "said impossible, but a workable order exists",
                       "impossible")
    if not isinstance(candidate, (list, tuple)):
        return Verdict(False, f"expected a list, got {type(candidate).__name__}",
                       "shape")

    order = list(candidate)
    if len(order) != len(spec["jobs"]):
        return Verdict(False, f"listed {len(order)} jobs, there are "
                              f"{len(spec['jobs'])}", "coverage")
    if set(order) != set(spec["jobs"]):
        unknown = [j for j in order if j not in spec["jobs"]]
        if unknown:
            return Verdict(False, f"{unknown[0]!r} is not one of the jobs",
                           "coverage")
        return Verdict(False, "a job is listed twice", "coverage")

    now = DAY_START
    for index, job in enumerate(order):
        if index:
            now += spec["travel"]
        low, high = spec["windows"][job]
        start = max(now, low)
        finish = start + spec["durations"][job]
        if finish > high:
            return Verdict(
                False,
                f"{job} cannot finish by {clock(high)}: starting as early as "
                f"{clock(start)} it runs to {clock(finish)}", "window")
        if finish > spec["day_end"]:
            return Verdict(False, f"{job} runs past the end of the day", "day")
        now = finish
    return Verdict(True)


# ------------------------------------------------------------------- parsing

def parse(text: str):
    """Pull an ordered list of job names out of a reply.

    Two problems to survive. Models drop the article and re-word the rest, so
    each place is matched on a single distinctive key rather than its full
    name. And a model that reasons before answering mentions every job several
    times, so reading the whole reply recovers the order it thought about
    rather than the order it chose: the numbered list at the end is the answer,
    and that is what gets read.
    """
    import re

    if IMPOSSIBLE.lower() in text.lower():
        return None

    lines = text.strip().splitlines()
    numbered, run = [], []
    for line in lines:
        if re.match(r"\s*\d+\s*[.)]\s*\S", line):
            run.append(line)
        elif run:
            numbered, run = run if len(run) >= len(numbered) else numbered, []
    if run and len(run) >= len(numbered):
        numbered = run
    region = chr(10).join(numbered) if numbered else text

    lowered = region.lower()
    hits = []
    for name, key in PLACES:
        position = lowered.find(key)
        if position != -1:
            hits.append((position, name))
    return [name for _, name in sorted(hits)]


def corrupt(problem: Problem, solution, rng) -> list:
    """Coverage breaks, plus a real window break found by search.

    The window case is taken from an order the scheduler has already proved
    unworkable, rather than from a guess that reordering will break something.
    A guess is often wrong: many orders are valid, and testing the verifier
    against one of those tests nothing.
    """
    spec = problem.spec
    out = [
        ("coverage", list(solution)[:-1]),
        ("coverage", list(solution) + [solution[0]]),
        ("coverage", ["a job that does not exist"] + list(solution)[1:]),
    ]
    for order in permutations(spec["jobs"]):
        if _schedule(spec, list(order)) is None:
            out.append(("window", list(order)))
            break
    return out


def formats(problem, solution) -> list:
    numbered = NL.join(f"{i}. {job}" for i, job in enumerate(solution, 1))
    # The article dropped and everything lowercased, which is what the model
    # actually did and what the first parser could not read.
    stripped = NL.join(
        f"{i}. {job[4:].lower() if job.startswith('the ') else job.lower()}"
        for i, job in enumerate(solution, 1))
    # Reasoning first, in a different order from the answer. A parser reading
    # the whole reply recovers the order it thought about, not the one it chose.
    misleading = (
        "Let me consider these. " + ", ".join(reversed(solution)) + " all have "
        "windows to respect." + NL + NL + "My answer:" + NL + numbered)
    timed = NL.join(f"{i}. {job} (starts 9:00am)"
                    for i, job in enumerate(solution, 1))
    return [numbered, stripped, misleading, timed]


KLASS = Klass(
    name="jobs",
    generate=generate, solve=solve, verify=verify, parse=parse,
    corrupt=corrupt, formats=formats,
    blurb="ordering a day's jobs under durations, time windows and travel",
)
