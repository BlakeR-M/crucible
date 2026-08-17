"""Seating a room: who sits at which table, with people who must and must not mix.

The wedding-table problem, and it earns its place for a reason beyond
familiarity. It is a pure assignment problem with no arithmetic in it at all, so
a model cannot stumble into the right answer by being good at sums. Either it
tracked who can sit with whom, or it did not.

It also produces the most legible possible failure. "Marcus and Nina are at
table 2" is either allowed or it is not, and a person reading the demo needs no
explanation to see which.

Three constraint families:

    capacity   a table holds only so many
    apart      two people must not share a table
    together   two people must share a table

The together and apart rules interact, which is where models come unstuck: put
two people together and you have implicitly decided about everyone already at
that table.
"""

from __future__ import annotations

import random

NL = chr(10)

from .base import IMPOSSIBLE, Klass, Problem, humanise, pick_names

SIZES = {
    "easy": (6, 2, 3),        # guests, tables, seats per table
    "medium": (9, 3, 3),
    "hard": (12, 3, 4),
}


def generate(rng: random.Random, difficulty: str = "medium") -> Problem:
    guests_count, tables, seats = SIZES.get(difficulty, SIZES["medium"])
    guests = pick_names(rng, guests_count)

    apart, together = [], []

    # Infeasibility has to be planted, not hoped for. Scattering more random
    # pairs does almost nothing: keeping people apart is graph colouring, and a
    # sparse random graph on nine guests is nearly always colourable with three
    # tables. Measured across four densities from three pairs up to twelve, the
    # rate never rose above 1%, so a class relying on chance would almost never
    # ask the model whether an arrangement exists at all.
    #
    # Two structures make it certainly impossible, and both read as something a
    # person would actually say:
    #
    #   a group who must all sit apart, one larger than the number of tables
    #   a group who must all sit together, one larger than a table
    #
    # Neither is stated as a group. Both are written out as ordinary pairs, so
    # nothing in the prose announces that this instance is the impossible kind.
    plant = rng.random() < 0.28
    if plant and rng.random() < 0.5:
        clique = rng.sample(guests, len(spec_tables := [f"Table {i + 1}"
                                                        for i in range(tables)]) + 1)
        for i, a in enumerate(clique):
            for b in clique[i + 1:]:
                apart.append(tuple(sorted((a, b))))
    elif plant:
        group = rng.sample(guests, seats + 1)
        for a, b in zip(group, group[1:]):
            together.append(tuple(sorted((a, b))))

    for _ in range(rng.choice([2, 3, 4])):
        pair = tuple(sorted(rng.sample(guests, 2)))
        if pair not in apart and pair not in together:
            apart.append(pair)
    if not plant:
        for _ in range(rng.choice([0, 1, 1, 2])):
            pair = tuple(sorted(rng.sample(guests, 2)))
            if pair not in apart and pair not in together:
                together.append(pair)

    spec = {
        "guests": guests,
        "tables": [f"Table {i + 1}" for i in range(tables)],
        "seats": seats,
        "apart": [list(p) for p in apart],
        "together": [list(p) for p in together],
    }

    problem = Problem(
        id=f"seating-{rng.getrandbits(24):06x}",
        klass="seating",
        prose=render(spec),
        spec=spec,
        answer_format=(
            "one line per guest, written exactly as 'Name: Table 1'. "
            f"If no valid seating exists, reply with the single word {IMPOSSIBLE}."
        ),
        difficulty=difficulty,
    )
    solution = solve(problem)
    return Problem(**{**problem.__dict__, "impossible": solution is None})


def render(spec: dict) -> str:
    lines = [
        f"I'm seating {humanise(spec['guests'])} for dinner.",
        f"There are {len(spec['tables'])} tables and each one seats "
        f"{spec['seats']} people.",
        "Everyone needs a seat.",
    ]
    for a, b in spec["apart"]:
        lines.append(f"{a} and {b} must not be at the same table.")
    for a, b in spec["together"]:
        lines.append(f"{a} and {b} have to be at the same table.")
    return " ".join(lines)


# ------------------------------------------------------------------- solving

def solve(problem: Problem):
    spec = problem.spec
    guests, tables, seats = spec["guests"], spec["tables"], spec["seats"]
    apart = {tuple(p) for p in spec["apart"]}
    together = {tuple(p) for p in spec["together"]}

    assignment: dict = {}
    used = {t: 0 for t in tables}

    def conflicts(name: str, table: str) -> bool:
        for other, seated in assignment.items():
            if seated != table:
                continue
            pair = tuple(sorted((name, other)))
            if pair in apart:
                return True
        return False

    def separated(name: str, table: str) -> bool:
        """A partner already seated elsewhere makes this table wrong."""
        for a, b in together:
            if name == a and b in assignment and assignment[b] != table:
                return True
            if name == b and a in assignment and assignment[a] != table:
                return True
        return False

    def step(index: int) -> bool:
        if index == len(guests):
            return True
        name = guests[index]
        for table in tables:
            if used[table] >= seats:
                continue
            if conflicts(name, table) or separated(name, table):
                continue
            assignment[name] = table
            used[table] += 1
            if step(index + 1):
                return True
            del assignment[name]
            used[table] -= 1
        return False

    return dict(assignment) if step(0) else None


# ----------------------------------------------------------------- verifying

def verify(problem: Problem, candidate) -> object:
    from .base import Verdict

    spec = problem.spec
    if problem.impossible:
        if candidate is None:
            return Verdict(True)
        return Verdict(False, "a seating was given for a problem that has none",
                       "impossible")
    if candidate is None:
        return Verdict(False, "said impossible, but a valid seating exists",
                       "impossible")
    if not isinstance(candidate, dict):
        return Verdict(False, f"expected a mapping, got {type(candidate).__name__}",
                       "shape")

    missing = [g for g in spec["guests"] if g not in candidate]
    if missing:
        return Verdict(False, f"{missing[0]} has no seat", "coverage")
    strangers = [g for g in candidate if g not in spec["guests"]]
    if strangers:
        return Verdict(False, f"seated someone who is not a guest: {strangers[0]}",
                       "coverage")

    counts: dict = {}
    for guest in spec["guests"]:
        table = candidate[guest]
        if table not in spec["tables"]:
            return Verdict(False, f"{guest} is at {table!r}, which is not a table",
                           "shape")
        counts[table] = counts.get(table, 0) + 1
    for table, count in counts.items():
        if count > spec["seats"]:
            return Verdict(False, f"{table} has {count} people but seats "
                                  f"{spec['seats']}", "capacity")

    for a, b in spec["apart"]:
        if candidate[a] == candidate[b]:
            return Verdict(False, f"{a} and {b} are both at {candidate[a]}",
                           "apart")
    for a, b in spec["together"]:
        if candidate[a] != candidate[b]:
            return Verdict(False, f"{a} is at {candidate[a]} and {b} is at "
                                  f"{candidate[b]}, but they must sit together",
                           "together")
    return Verdict(True)


# ------------------------------------------------------------------- parsing

def parse(text: str):
    import re

    if IMPOSSIBLE.lower() in text.lower():
        return None
    found = {}
    pattern = re.compile(
        r"\*{0,2}([A-Z][a-z]+)\*{0,2}\s*[:\-–]\s*\*{0,2}(?:Table\s*)?(\d+)",
        re.IGNORECASE)
    for name, number in pattern.findall(text):
        found.setdefault(name.capitalize(), f"Table {int(number)}")
    return found or {}


def corrupt(problem: Problem, solution, rng) -> list:
    spec = problem.spec
    out = []

    dropped = dict(solution)
    dropped.pop(spec["guests"][0])
    out.append(("coverage", dropped))

    # Everyone at one table. Guests always outnumber the seats at a table.
    crammed = {g: spec["tables"][0] for g in spec["guests"]}
    out.append(("capacity", crammed))

    if spec["apart"]:
        a, b = spec["apart"][0]
        bad = dict(solution)
        bad[b] = bad[a]
        out.append(("apart", bad))

    if spec["together"]:
        a, b = spec["together"][0]
        other = next((t for t in spec["tables"] if t != solution[a]), None)
        if other:
            bad = dict(solution)
            bad[b] = other
            out.append(("together", bad))
    return out


def formats(problem, solution) -> list:
    guests = problem.spec["guests"]
    plain = NL.join(f"{g}: {solution[g]}" for g in guests)
    bold = NL.join(f"**{g}**: {solution[g]}" for g in guests)
    lower = NL.join(f"{g} - {solution[g].lower()}" for g in guests)
    chatty = "Seating plan:" + NL + NL + plain + NL + NL + "That satisfies everything."
    return [plain, bold, lower, chatty]


KLASS = Klass(
    name="seating",
    generate=generate, solve=solve, verify=verify, parse=parse,
    corrupt=corrupt, formats=formats,
    blurb="guests to tables under capacity, must-sit-apart and must-sit-together",
)
