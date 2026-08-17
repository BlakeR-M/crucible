"""Does constrained decoding make a 9B model reliable at agentic tool calling?

One question, two arms, and a deliberate effort not to flatter the answer.

  A  the model, asked in the system prompt to reply with one JSON object
  B  the same model, same prompt, with the reply constrained during sampling so
     that nothing outside the documented shape can be generated

Everything else is held still: same weights, same quantisation, same
temperature, same seed sequence, same workspace, same task, serial execution.
Thinking is disabled in both, because the constraint would otherwise forbid the
<think> block in B and the arms would differ by two things instead of one.

Each arm runs several times. The first version of this sampled each arm once at
temperature zero, which was wrong twice over: the run was one observation of a
noisy process, and greedy decoding made three independent verifiers return
character-identical verdicts, so a panel of three was one opinion with three
votes. Both are fixed here. The arms sample, and they repeat.

What gets measured, and why each one is here:

  strict / salvaged / dead   how the raw replies graded. Dead steps are the
                             direct cost. Salvaged is the interesting number,
                             because it is work the arena's tolerant parser is
                             doing on the model's behalf, invisible in any
                             metric taken after parsing.

  defects recovered          against the nine planted in demo_target, scored
                             once per defect rather than once per mention.

  survivors                  what came through three adversarial verifiers.

The bar was set before any number existed and is written in the plan: an 80%
fall in malformed calls, six of nine defects recovered, and reasoning no more
than about 15% below the unconstrained arm.

    python -m bench.run_bench --arm A --runs 5
    python -m bench.run_bench --arm B --runs 5
    python -m bench.run_bench --compare
"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from dataclasses import asdict
from pathlib import Path

from crucible.archive import score_against_key
from crucible.ledger import Ledger
from crucible.local import LocalProvider
from crucible.orchestrator import Orchestrator
from crucible.policy import review_policy
from crucible.providers import Budget

from bench import schemas
from bench.classify import Tally, classify
from bench.score import load_key, score

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "bench" / "results"
WORKSPACE = ROOT / "demo_target"

BASE_URL = "http://127.0.0.1:8080"
MODEL = "Qwen3.5-9B-Q5_K_M"

TASK = ("Review this codebase for correctness defects. Report only real bugs "
        "that produce wrong behaviour.")

# Serial. Two agents running concurrently against one llama.cpp server share
# its batching and the arms stop being comparable, which matters more here than
# the wall clock does.
WORKERS = 1

# Far apart so two runs of the same arm never share a seed, and fixed so the
# whole benchmark replays.
SEED_STRIDE = 100_000


# Two factors, so four arms. Constraint was the question; thinking turned out
# to matter as much and had to be separated from it rather than fixed at one
# level. Qwen3.5 is a reasoning model, so running it with thinking off tests a
# configuration nobody would deploy, and a weak result there would say more
# about the harness than about the model. Verified first that llama.cpp routes
# <think> to reasoning_content and applies the schema only to the answer, so
# the two factors really are independent here.
ARMS = {
    "A": {"constrained": False, "think": False},
    "B": {"constrained": True,  "think": False},
    "C": {"constrained": False, "think": True},
    "D": {"constrained": True,  "think": True},
}


def single_run(arm: str, index: int, *, constrained: bool, think: bool,
               ceiling: float) -> dict:
    tally = Tally()
    replies: list[dict] = []
    unknown_roles = 0
    tools = set(review_policy(WORKSPACE).rules)

    def on_reply(record: dict) -> None:
        nonlocal unknown_roles
        role = schemas.role_for(record["system"])
        if role == "unknown":
            unknown_roles += 1
        verdict = classify(record["text"], tools, role)
        tally.add(verdict, role)
        replies.append({
            "role": role, "grade": verdict.grade, "kind": verdict.kind,
            "tool": verdict.tool, "reason": verdict.reason,
            "constrained": record["constrained"],
            "output_tokens": record["output_tokens"],
            "seconds": record["seconds"], "finish": record["finish"],
            # Kept whole. Every number above is derived from these, and a
            # result nobody can go back and check is not a result.
            "text": record["text"],
        })

    raised: list[dict] = []

    def collect(event: dict) -> None:
        if event.get("kind") == "finding_raised":
            raised.append(dict(event))

    provider = LocalProvider(
        BASE_URL, MODEL,
        schema_for=schemas.schema_for if constrained else None,
        on_reply=on_reply,
        think=think,
        seed=1234 + index * SEED_STRIDE,
    )
    ledger_path = RESULTS / f"arm-{arm}-run{index}.jsonl"
    ledger_path.unlink(missing_ok=True)

    budget = Budget(ceiling_usd=ceiling, unmetered=True)
    orchestrator = Orchestrator(
        provider, WORKSPACE, review_policy(WORKSPACE), Ledger(ledger_path),
        budget, emit=collect, max_workers=WORKERS,
    )

    started = time.time()
    report = orchestrator.run(TASK)
    elapsed = time.time() - started

    key = load_key(WORKSPACE / ".answer_key.json")
    shipped = score_against_key(report, WORKSPACE)

    return {
        "run": index,
        "seconds": round(elapsed, 1),
        "wire": tally.as_dict(),
        "unknown_roles": unknown_roles,
        "raised": report.raised,
        "survived": report.survived,
        "lanes": report.lanes,
        "tool_calls": report.tool_calls,
        "refusals": report.refusals,
        "model_calls": budget.calls,
        "halted": report.halted,
        "probe_held": report.probe.get("held"),
        "recovery_survivors": score(report.findings, key),
        "recovery_raised": score(raised, key),
        "shipped_scorer_found": len(shipped["found"]) if shipped else None,
        "report": asdict(report),
        "raised_findings": raised,
        "replies": replies,
    }


def _spread(values: list) -> dict:
    if not values:
        return {"mean": 0, "min": 0, "max": 0}
    return {
        "mean": round(statistics.mean(values), 2),
        "min": min(values), "max": max(values),
        "stdev": round(statistics.stdev(values), 2) if len(values) > 1 else 0.0,
    }


def aggregate(runs: list[dict]) -> dict:
    def col(get):
        return _spread([get(r) for r in runs])

    # Defects found across every run of the arm, rather than in any single one.
    # A model that finds a different two each time is a different thing from
    # one that finds the same two, and the union says which.
    union: set[str] = set()
    for r in runs:
        union.update(r["recovery_raised"]["recovered"])

    return {
        "runs": len(runs),
        "strict_pct": col(lambda r: r["wire"]["strict_pct"]),
        "salvaged_pct": col(lambda r: r["wire"]["salvaged_pct"]),
        "dead_pct": col(lambda r: r["wire"]["dead_pct"]),
        "dead": col(lambda r: r["wire"]["dead"]),
        "salvaged": col(lambda r: r["wire"]["salvaged"]),
        "replies": col(lambda r: r["wire"]["total"]),
        "raised": col(lambda r: r["raised"]),
        "survived": col(lambda r: r["survived"]),
        "found_hunt": col(lambda r: r["recovery_raised"]["recovered_count"]),
        "found_final": col(lambda r: r["recovery_survivors"]["recovered_count"]),
        "tool_calls": col(lambda r: r["tool_calls"]),
        "seconds": col(lambda r: r["seconds"]),
        "union_found_hunt": sorted(union),
        "union_count": len(union),
    }


def run_arm(arm: str, *, repeats: int, ceiling: float = 5.00) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    constrained = ARMS[arm]["constrained"]
    think = ARMS[arm]["think"]
    print(f"\n=== arm {arm}: "
          f"{'constrained' if constrained else 'prompted only'}, "
          f"thinking {'on' if think else 'off'}, {repeats} runs ===")
    runs = []
    for i in range(repeats):
        result = single_run(arm, i, constrained=constrained, think=think,
                            ceiling=ceiling)
        runs.append(result)
        w = result["wire"]
        print(f"  run {i}  {w['total']:>3} replies  "
              f"strict {w['strict_pct']:>5}%  dead {w['dead']:>2}  "
              f"raised {result['raised']:>2}  survived {result['survived']:>2}  "
              f"defects {result['recovery_raised']['recovered_count']}/9 hunt "
              f"{result['recovery_survivors']['recovered_count']}/9 final  "
              f"{result['seconds']:>5}s")

    payload = {
        "arm": arm, "constrained": constrained, "think": think,
        "model": MODEL, "task": TASK, "workers": WORKERS,
        "aggregate": aggregate(runs), "runs": runs,
    }
    (RESULTS / f"arm-{arm}.json").write_text(
        json.dumps(payload, indent=2), encoding="utf-8")
    agg = payload["aggregate"]
    print(f"\n  mean strict {agg['strict_pct']['mean']}%  "
          f"dead {agg['dead']['mean']}  "
          f"defects/hunt {agg['found_hunt']['mean']}  "
          f"union across runs {agg['union_count']}/9 ({', '.join(agg['union_found_hunt']) or 'none'})")
    return payload


def compare() -> None:
    arms = {}
    for name in ARMS:
        path = RESULTS / f"arm-{name}.json"
        if path.exists():
            arms[name] = json.loads(path.read_text(encoding="utf-8"))
    if len(arms) < 2:
        print("need at least two arms; run --arm A and --arm B first")
        return

    names = list(arms)
    print()
    for n in names:
        a = arms[n]
        print(f"  arm {n}: {'constrained' if a['constrained'] else 'prompted only'}, "
              f"thinking {'on' if a.get('think') else 'off'}")
    rows = [
        ("replies per run", "replies"),
        ("strict %", "strict_pct"),
        ("salvaged %", "salvaged_pct"),
        ("dead %", "dead_pct"),
        ("dead steps", "dead"),
        ("findings raised", "raised"),
        ("findings survived", "survived"),
        ("defects found (hunt)", "found_hunt"),
        ("defects found (final)", "found_final"),
        ("tool calls", "tool_calls"),
        ("seconds", "seconds"),
    ]
    width = 24
    print()
    print(" " * width + "".join(f"{('arm ' + n):>20}" for n in names))
    print("-" * (width + 20 * len(names)))
    for label, key in rows:
        cells = ""
        for n in names:
            s = arms[n]["aggregate"][key]
            cells += f"{s['mean']:>10} ({s['min']}-{s['max']})".rjust(20)
        print(f"{label:<{width}}{cells}")
    print(f"\n{'union of defects found':<{width}}"
          + "".join(f"{str(arms[n]['aggregate']['union_count']) + '/9':>20}"
                    for n in names))
    for n in names:
        print(f"  arm {n}: {', '.join(arms[n]['aggregate']['union_found_hunt']) or 'none'}")

    # The gate is run on each thinking level separately. Averaging over both
    # would hide the only interesting possibility, which is that the constraint
    # helps at one operating point and not at the other.
    for unconstrained, constrained, label in (("A", "B", "thinking off"),
                                              ("C", "D", "thinking on")):
        if unconstrained in arms and constrained in arms:
            print(f"\n  gate, {label} ({unconstrained} vs {constrained}):")
            _verdict(arms[unconstrained]["aggregate"],
                     arms[constrained]["aggregate"])


def _verdict(a: dict, b: dict) -> None:
    """The bars, as written down before any of these numbers existed."""
    a_bad = a["salvaged"]["mean"] + a["dead"]["mean"]
    b_bad = b["salvaged"]["mean"] + b["dead"]["mean"]
    drop = (a_bad - b_bad) / a_bad * 100 if a_bad else 0.0
    found_b = b["found_final"]["mean"]
    found_a = a["found_final"]["mean"]

    checks = [
        ("malformed replies fall 80%+",
         a_bad > 0 and drop >= 80,
         f"{a_bad} -> {b_bad} per run ({drop:.0f}% fall)"
         if a_bad else "unconstrained arm had none to fall"),
        ("constrained recovers 6 of 9+",
         found_b >= 6, f"{found_b}/9 mean"),
        ("reasoning within ~15%",
         found_a == 0 or found_b >= found_a * 0.85,
         f"{found_a}/9 vs {found_b}/9"),
    ]
    for label, passed, detail in checks:
        print(f"    {'PASS' if passed else 'FAIL'}  {label:<30} {detail}")
    if all(p for _, p, _ in checks):
        print("    -> all three bars cleared at this operating point")
    else:
        print("    -> at least one bar missed; on the plan that stops the "
              "notation work rather than starting it")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm", choices=sorted(ARMS))
    parser.add_argument("--runs", type=int, default=5)
    parser.add_argument("--compare", action="store_true")
    parser.add_argument("--ceiling", type=float, default=5.00)
    args = parser.parse_args()

    if args.compare:
        compare()
        return 0
    if not args.arm:
        parser.error(f"pass --arm {'/'.join(sorted(ARMS))}, or --compare")
    run_arm(args.arm, repeats=args.runs, ceiling=args.ceiling)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
