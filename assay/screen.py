"""How well does a model do on each problem class, and is there room to beat it?

This is the kill test for the whole idea, and it runs before anything is
trained. If a frontier model already answers a class correctly nearly every
time, that class is dead: there is nothing for a small specialist to win. If it
sits somewhere in the middle, that is the target.

    python -m assay.screen --provider local --n 40
    python -m assay.screen --provider openai --model gpt-5 --n 60 --classes jobs

Two floors are printed beside every score, and they matter as much as the score.

**Always-impossible.** Some instances have no valid answer, so a model that
replies IMPOSSIBLE to everything scores whatever fraction that is. A result
below that floor is worse than refusing to think.

**Nothing parsed.** A reply the parser could not read counts as wrong, because
an answer nobody can act on is not an answer. It is reported separately so a
low score caused by formatting is never mistaken for a low score caused by
reasoning.

The prose is the only thing the model sees. The formal spec exists for the
solver and the verifier and never enters a prompt, because the premise being
tested is that formalising an informally stated problem is the hard part.
"""

from __future__ import annotations

import argparse
import json
import os
import time
from collections import Counter
from pathlib import Path

from crucible.providers import Budget, OpenAIProvider, Tier

from assay.problems import CLASSES, rng_for

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "assay" / "results"

LOCAL_URL = os.environ.get("CRUCIBLE_LOCAL_URL", "http://127.0.0.1:8080/v1")
LOCAL_MODEL = os.environ.get("CRUCIBLE_LOCAL_MODEL", "local")

SYSTEM = """\
You solve small scheduling and arrangement problems stated in plain English.

Work carefully. Check every stated rule against your answer before giving it.
Some of these problems have no valid answer at all; saying so is correct when
it is true, and wrong when it is not.

End your reply with the answer in exactly the format asked for, and nothing
after it.
"""


def ask(provider, problem, budget, max_output: int) -> str:
    user = (f"{problem.prose}\n\n"
            f"Give your answer as {problem.answer_format}")
    reply = provider.complete(SYSTEM, user, Tier.PLANNER, budget,
                              max_output=max_output)
    return reply.text


def screen(klass_name: str, provider, count: int, difficulty: str,
           budget, max_output: int) -> dict:
    klass = CLASSES[klass_name]
    records = []
    started = time.time()

    for seed in range(count):
        problem = klass.generate(rng_for(seed), difficulty)
        try:
            text = ask(provider, problem, budget, max_output)
        except Exception as exc:  # noqa: BLE001
            records.append({"id": problem.id, "impossible": problem.impossible,
                            "correct": False, "broke": "call_failed",
                            "reason": f"{type(exc).__name__}: {exc}"[:200],
                            "reply": "", "seed": seed})
            continue
        candidate = klass.parse(text)
        parsed = candidate is not None or "IMPOSSIBLE" in text.upper()
        verdict = klass.verify(problem, candidate)
        records.append({
            "id": problem.id, "impossible": problem.impossible,
            "correct": bool(verdict),
            "broke": verdict.broke or ("" if verdict else "unknown"),
            "reason": verdict.reason[:200],
            "said_impossible": candidate is None,
            "parsed": parsed,
            "reply": text[-1200:], "seed": seed,
        })
        mark = "ok " if verdict else "XX "
        print(f"  {mark} {problem.id}  {verdict.reason[:72]}")

    return summarise(klass_name, records, difficulty,
                     round(time.time() - started, 1))


def summarise(klass_name: str, records: list, difficulty: str,
              seconds: float) -> dict:
    total = len(records)
    correct = sum(1 for r in records if r["correct"])
    impossible = sum(1 for r in records if r["impossible"])
    unparsed = sum(1 for r in records if not r.get("parsed", True))
    # How the model did on the two halves separately. A score that looks fine
    # overall often hides a model that never once recognised infeasibility.
    solvable = [r for r in records if not r["impossible"]]
    unsolvable = [r for r in records if r["impossible"]]
    return {
        "class": klass_name,
        "difficulty": difficulty,
        "n": total,
        "correct": correct,
        "accuracy_pct": round(correct / total * 100, 1) if total else 0.0,
        "floor_always_impossible_pct": (round(impossible / total * 100, 1)
                                        if total else 0.0),
        "on_solvable_pct": (round(sum(1 for r in solvable if r["correct"])
                                  / len(solvable) * 100, 1) if solvable else None),
        "on_impossible_pct": (round(sum(1 for r in unsolvable if r["correct"])
                                    / len(unsolvable) * 100, 1)
                              if unsolvable else None),
        "unparsed": unparsed,
        "broke": dict(Counter(r["broke"] for r in records if not r["correct"])),
        "seconds": seconds,
        "records": records,
    }


def report(summary: dict) -> None:
    s = summary
    print(f"\n  {s['class']} ({s['difficulty']}), n={s['n']}, {s['seconds']}s")
    print(f"    accuracy            {s['accuracy_pct']}%")
    print(f"    on solvable ones    {s['on_solvable_pct']}%")
    print(f"    on impossible ones  {s['on_impossible_pct']}%")
    print(f"    floor: always say IMPOSSIBLE   {s['floor_always_impossible_pct']}%")
    if s["unparsed"]:
        print(f"    replies nothing could be read from: {s['unparsed']}")
    if s["broke"]:
        print("    failures by rule broken:")
        for rule, count in sorted(s["broke"].items(), key=lambda kv: -kv[1]):
            print(f"      {rule or 'correct-but-unverified':<16} {count}")

    headroom = 100 - s["accuracy_pct"]
    print(f"    headroom            {headroom:.1f} points")
    if s["accuracy_pct"] >= 90:
        print("    -> too easy. no room for a specialist to win here.")
    elif s["accuracy_pct"] >= 45:
        print("    -> TARGET. enough room to beat, enough signal to train on.")
    else:
        print("    -> very hard. good headroom, but check the failures are "
              "reasoning rather than the model misreading the task.")


GEMINI_URL = "https://generativelanguage.googleapis.com/v1beta/openai"


def _key(name: str) -> str:
    """The named key from the environment, or from an env file.

    The file is CRUCIBLE_ENV_FILE when set, otherwise .env at the repo root.
    Never printed, never written into a result file. The screener records the
    model name and the spend and nothing else about how it reached the model.
    """
    value = os.environ.get(name, "")
    if value:
        return value
    path = Path(os.environ.get("CRUCIBLE_ENV_FILE") or ROOT / ".env")
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.startswith(f"{name}="):
                return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


def build_provider(kind: str, model: str):
    if kind == "local":
        return OpenAIProvider("", {t: LOCAL_MODEL for t in Tier},
                              base_url=LOCAL_URL, metered=False)
    if kind == "gemini":
        key = _key("GEMINI_API_KEY")
        if not key:
            raise SystemExit("no GEMINI_API_KEY in the environment or .env")
        return OpenAIProvider(key, {t: model for t in Tier},
                              base_url=GEMINI_URL)
    key = _key("OPENAI_API_KEY")
    if not key:
        raise SystemExit("no OPENAI_API_KEY in the environment or .env")
    return OpenAIProvider(key, {t: model for t in Tier})


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--provider", choices=["local", "openai", "gemini"],
                        default="local")
    parser.add_argument("--model", default="gpt-5")
    parser.add_argument("--n", type=int, default=30)
    parser.add_argument("--difficulty", default="medium",
                        choices=["easy", "medium", "hard"])
    parser.add_argument("--classes", nargs="*", default=list(CLASSES))
    parser.add_argument("--ceiling", type=float, default=3.00,
                        help="hard spend cap in USD for a paid run")
    parser.add_argument("--max-output", type=int, default=1400)
    args = parser.parse_args()

    RESULTS.mkdir(parents=True, exist_ok=True)
    provider = build_provider(args.provider, args.model)
    budget = Budget(ceiling_usd=args.ceiling,
                    unmetered=(args.provider == "local"))

    name = "local" if args.provider == "local" else args.model
    print(f"screening {name} on {', '.join(args.classes)}, "
          f"n={args.n} each, ceiling ${args.ceiling:.2f}")

    summaries = {}
    for klass_name in args.classes:
        print(f"\n=== {klass_name} ===")
        summaries[klass_name] = screen(klass_name, provider, args.n,
                                       args.difficulty, budget, args.max_output)

    print(f"\n{'=' * 62}")
    for summary in summaries.values():
        report(summary)
    print(f"\n  spent ${budget.spent_usd:.4f} over {budget.calls} calls")

    slug = name.replace("/", "-").replace("\\", "-").replace(":", "")
    out = RESULTS / f"screen-{slug}-{args.difficulty}.json"
    out.write_text(json.dumps(
        {"model": name, "difficulty": args.difficulty, "n": args.n,
         "spend_usd": round(budget.spent_usd, 4), "summaries": summaries},
        indent=2), encoding="utf-8")
    print(f"  written to {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
