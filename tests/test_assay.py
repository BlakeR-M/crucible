"""Checks for the Assay experiment's problem classes and coding tasks.

Plain script, no test framework, so it runs anywhere Python does:
    python tests/test_assay.py

Assay was killed on its own evidence (see assay/README.md). The generators,
solvers and verifiers stay in the tree as the record of that experiment, and
these checks keep them sound: the solver's own answer must verify, a corrupted
answer must not, an instance marked impossible must really have no solution,
and every coding task's reference solution must pass its own tests.
"""

from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from assay import tasks  # noqa: E402
from assay.problems import CLASSES, rng_for, self_check  # noqa: E402

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok  {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} {detail}")


def section(name: str) -> None:
    print(f"\n{name}")


def problem_checks() -> None:
    section("assay: problem classes")
    check("three classes are registered", set(CLASSES) == {"rostering", "seating", "jobs"})
    for name, klass in CLASSES.items():
        failures = self_check(klass, count=40)
        check(f"{name}: solver answers verify, corrupted ones fail, "
              f"impossible instances have no solution",
              not failures, "; ".join(failures[:3]))
        for difficulty in ("easy", "medium", "hard"):
            problems = [klass.generate(rng_for(seed), difficulty) for seed in range(30)]
            check(f"{name}/{difficulty}: every instance carries prose and an answer format",
                  all(p.prose and p.answer_format for p in problems))
        again = [klass.generate(rng_for(seed), "medium").prose for seed in range(10)]
        first = [klass.generate(rng_for(seed), "medium").prose for seed in range(10)]
        check(f"{name}: generation is deterministic in the seed", again == first)
        impossible = sum(1 for seed in range(120)
                         if klass.generate(rng_for(seed), "medium").impossible)
        check(f"{name}: some, and not most, medium instances are impossible",
              0 < impossible < 60, f"{impossible}/120")


def task_checks() -> None:
    section("assay: coding tasks")
    check("every reference solution passes its own tests", tasks.validate() == 0)


def main() -> None:
    problem_checks()
    task_checks()
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
