"""Shared shape for every problem class, and the rules they all obey.

Three things have to be true of every class here or the measurement built on
top of it is worthless.

**The verifier checks constraints, never equality with the reference answer.**
These are constraint problems and most of them have many valid solutions. A
verifier that compares against one stored answer would mark a perfectly good
alternative wrong, and the resulting score would measure agreement with our
solver rather than correctness. Every verify() below reads the candidate
against the rules and says which rule it broke.

**Some instances are impossible on purpose.** Recognising that no arrangement
satisfies the constraints is a distinct reasoning skill, and language models are
notably bad at it: they produce a confident arrangement that quietly violates
something rather than saying it cannot be done. A class with no unsatisfiable
instances would miss the failure mode most worth catching.

**The prose is the input; the formal spec never reaches the model.** The whole
premise is that the hard part is faithfully formalising an informally stated
problem, not searching once it is formal. A solver reads the spec and produces
ground truth. The model reads only the words. If the spec ever leaked into the
prompt we would be benchmarking a solver against itself.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field


@dataclass(frozen=True)
class Problem:
    """One instance: what the model sees, and what the solver sees."""

    id: str
    klass: str
    # The natural-language statement. This is the only thing the model gets.
    prose: str
    # The machine-readable statement, for the solver and the verifier only.
    spec: dict
    # How the answer should be written, told to the model in plain terms.
    answer_format: str
    difficulty: str = "medium"
    # True when no arrangement satisfies the constraints, so the only correct
    # answer is to say so.
    impossible: bool = False


@dataclass(frozen=True)
class Verdict:
    """Whether a candidate answer is right, and if not, which rule it broke."""

    correct: bool
    reason: str = ""
    # Which named constraint failed, for grouping failures afterwards.
    broke: str = ""

    def __bool__(self) -> bool:
        return self.correct


@dataclass
class Klass:
    """One problem class, registered so the screener can iterate them."""

    name: str
    generate: object      # (rng, difficulty) -> Problem
    solve: object         # (Problem) -> solution | None
    verify: object        # (Problem, candidate) -> Verdict
    parse: object         # (str) -> candidate | None
    # Answers that certainly break a stated rule, for testing the verifier.
    # Each class supplies its own because only the class knows which rules it
    # has: a generic "swap two entries" is not a corruption at all in a
    # constraint problem, it is usually just a different valid solution, and a
    # verifier is right to accept it.
    corrupt: object = None   # (Problem, solution, rng) -> list
    # Plausible ways a model might write a correct answer, for testing the
    # parser. Worth as much as the corruption test and for the same reason: a
    # parser that fails to read a correct answer scores it wrong, and the run
    # then measures formatting instead of reasoning. The first version of the
    # jobs parser scored a whole class at zero because the model wrote
    # "warehouse" where the job was named "the warehouse".
    formats: object = None   # (Problem, solution) -> list[str]
    blurb: str = ""


# The word a model must produce when it believes there is no valid arrangement.
# One fixed token so the parser can recognise the answer without interpreting
# prose, and stated in every prompt.
IMPOSSIBLE = "IMPOSSIBLE"


def rng_for(seed: int) -> random.Random:
    """A generator seeded per instance, so the whole set replays exactly."""
    return random.Random(seed)


def pick_names(rng: random.Random, count: int) -> list:
    """Ordinary first names, so the prose reads like something a person wrote."""
    pool = [
        "Priya", "Tom", "Aisha", "Noah", "Mei", "Diego", "Hannah", "Yusuf",
        "Grace", "Liam", "Fatima", "Oscar", "Ines", "Kai", "Sofia", "Elias",
        "Ruby", "Omar", "Nina", "Jonah", "Leila", "Marcus", "Zara", "Finn",
    ]
    return rng.sample(pool, count)


def humanise(items: list) -> str:
    """['a','b','c'] as 'a, b and c', because a prompt should read like prose."""
    items = [str(i) for i in items]
    if not items:
        return ""
    if len(items) == 1:
        return items[0]
    return ", ".join(items[:-1]) + " and " + items[-1]


def self_check(klass: Klass, count: int = 40) -> list:
    """Prove the class is sound before anything is measured with it.

    Four properties, and the third is the one that matters most. A verifier
    that accepts everything is worse than no verifier at all, because it turns
    every wrong answer into a passing score, so each solved instance is
    deliberately corrupted and the verifier has to reject it.

        1. every solvable instance gets a solution from solve()
        2. that solution passes verify()
        3. a corrupted version of it fails verify()
        4. an instance marked impossible really has no solution
    """
    failures = []
    for seed in range(count):
        rng = rng_for(seed)
        problem = klass.generate(rng, "medium")
        solution = klass.solve(problem)

        if problem.impossible:
            if solution is not None:
                failures.append(
                    f"{problem.id}: marked impossible but solve() found "
                    f"{solution!r}")
            continue

        if solution is None:
            failures.append(f"{problem.id}: solvable but solve() found nothing")
            continue

        verdict = klass.verify(problem, solution)
        if not verdict:
            failures.append(f"{problem.id}: solver's own answer failed "
                            f"verify(): {verdict.reason}")
            continue

        if klass.formats:
            for rendering in klass.formats(problem, solution):
                recovered = klass.parse(rendering)
                if not klass.verify(problem, recovered):
                    failures.append(
                        f"{problem.id}: parse() could not recover a correct "
                        f"answer written as {rendering.strip()[:60]!r}")
                    break

        broken = klass.corrupt(problem, solution, rng) if klass.corrupt else []
        if not broken:
            failures.append(f"{problem.id}: no corruption could be built, so "
                            f"the verifier was never tested on this instance")
            continue
        for label, corrupted in broken:
            if klass.verify(problem, corrupted):
                failures.append(
                    f"{problem.id}: verify() accepted an answer that breaks "
                    f"{label}")
                break
    return failures
