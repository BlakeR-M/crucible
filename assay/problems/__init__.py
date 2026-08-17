"""The problem classes, and the check that they are sound.

    python -m assay.problems

Runs every class through self_check: the solver's own answer must verify, a
deliberately corrupted answer must not, and an instance marked impossible must
really have no solution. Nothing downstream is worth measuring until this
passes, because a verifier that accepts a wrong answer turns every score into a
number about the verifier.
"""

from __future__ import annotations

from . import jobs, rostering, seating
from .base import IMPOSSIBLE, Klass, Problem, Verdict, rng_for, self_check

CLASSES = {
    "rostering": rostering.KLASS,
    "seating": seating.KLASS,
    "jobs": jobs.KLASS,
}

__all__ = ["CLASSES", "Klass", "Problem", "Verdict", "IMPOSSIBLE", "rng_for",
           "self_check"]


def main() -> int:
    total_failures = 0
    for name, klass in CLASSES.items():
        failures = self_check(klass, count=60)
        total_failures += len(failures)
        status = "ok" if not failures else f"{len(failures)} PROBLEMS"
        print(f"  {name:<12} {klass.blurb:<62} {status}")
        for line in failures[:6]:
            print(f"      {line}")

    # How many instances land impossible. A class with none never tests whether
    # a model can recognise infeasibility, and a class with too many is mostly
    # measuring one answer.
    print()
    for name, klass in CLASSES.items():
        problems = [klass.generate(rng_for(seed), "medium") for seed in range(120)]
        impossible = sum(1 for p in problems if p.impossible)
        print(f"  {name:<12} {impossible}/120 instances impossible "
              f"({impossible / 1.2:.0f}%)")
    return 1 if total_failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
