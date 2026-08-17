"""Recompute every number from the saved replies, identically for both arms.

The harness grades replies as it goes, which is enough to watch a run and not
enough to trust a comparison. Any metric added after the first arm has already
finished would be computed by newer code for the second arm than for the first,
and two arms measured by two instruments are not two arms.

So the raw text of every reply is kept, and this reads it back. Whatever is
added here is applied to both arms by the same code on the same day, and the
saved runs stay checkable by anyone who wants to disagree with the arithmetic.

The cut this exists for is grade by kind. A tool call is one short line and a
small model gets it right nearly always; the done object is where a hunter has
to emit several findings of seven fields each, most of them prose containing
quotes, and a literal newline inside a JSON string is invalid JSON. If
constrained decoding earns its keep anywhere it is there, and an aggregate over
both kinds hides it, because tool calls outnumber done objects several to one.

    python -m bench.analyse
"""

from __future__ import annotations

import json
from pathlib import Path

from crucible.policy import review_policy

from bench.classify import classify

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "bench" / "results"
WORKSPACE = ROOT / "demo_target"


def _blank() -> dict:
    return {"strict": 0, "salvaged": 0, "dead": 0}


def regrade(arm: dict, tools: set[str]) -> dict:
    """Every saved reply of every run, graded again by today's classifier."""
    overall = _blank()
    by_kind: dict[str, dict] = {}
    by_role: dict[str, dict] = {}
    reasons: dict[str, int] = {}
    # A reply that parses but whose done object is missing required fields is
    # a distinct failure from one that does not parse at all, and only the
    # constrained arm can be immune to it by construction.
    incomplete_done = 0
    tokens = 0
    seconds = 0.0

    replies = [r for run in arm["runs"] for r in run["replies"]]
    for reply in replies:
        verdict = classify(reply["text"], tools, reply["role"])
        overall[verdict.grade] += 1
        by_kind.setdefault(verdict.kind, _blank())[verdict.grade] += 1
        by_role.setdefault(reply["role"], _blank())[verdict.grade] += 1
        if verdict.grade != "strict" and verdict.reason:
            key = verdict.reason[:70]
            reasons[key] = reasons.get(key, 0) + 1
        tokens += reply.get("output_tokens") or 0
        seconds += reply.get("seconds") or 0.0
        if verdict.kind == "done":
            incomplete_done += _done_missing_fields(reply["text"], reply["role"])

    return {
        "runs": len(arm["runs"]),
        "overall": overall,
        "by_kind": by_kind,
        "by_role": by_role,
        "incomplete_done": incomplete_done,
        "output_tokens": tokens,
        "generation_seconds": round(seconds, 1),
        "top_reasons": dict(sorted(reasons.items(), key=lambda kv: -kv[1])[:8]),
    }


REQUIRED = {
    "hunter": ("done", "findings"),
    "verifier": ("done", "refuted", "confidence", "concrete_failure", "reasoning"),
    "prober": ("done", "got_through", "notes"),
}
FINDING_FIELDS = ("title", "file", "line", "severity", "summary",
                  "failure_scenario", "evidence")


def _done_missing_fields(text: str, role: str) -> int:
    """1 if this done object is short of what its seat is supposed to return."""
    from crucible.orchestrator import _extract_json

    obj = _extract_json(text)
    if not isinstance(obj, dict):
        return 0
    for field in REQUIRED.get(role, ()):
        if field not in obj:
            return 1
    if role == "hunter":
        for finding in obj.get("findings") or []:
            if not isinstance(finding, dict):
                return 1
            if any(f not in finding for f in FINDING_FIELDS):
                return 1
    return 0


def _pct(slot: dict, grade: str) -> float:
    total = sum(slot.values())
    return round(slot[grade] / total * 100, 1) if total else 0.0


def main() -> int:
    tools = set(review_policy(WORKSPACE).rules)
    arms = {}
    for name in ("A", "B", "C"):
        path = RESULTS / f"arm-{name}.json"
        if path.exists():
            arms[name] = json.loads(path.read_text(encoding="utf-8"))
    if not arms:
        print("no arms to analyse")
        return 1

    out = {}
    for name, arm in arms.items():
        out[name] = regrade(arm, tools)

    print(f"\n{'':<26}" + "".join(f"{('arm ' + n):>14}" for n in arms))
    print("-" * (26 + 14 * len(arms)))

    def row(label, get):
        print(f"{label:<26}" + "".join(f"{get(out[n], arms[n]):>14}" for n in arms))

    row("runs", lambda o, a: o["runs"])
    row("replies (all runs)", lambda o, a: sum(o["overall"].values()))
    row("strict", lambda o, a: o["overall"]["strict"])
    row("salvaged", lambda o, a: o["overall"]["salvaged"])
    row("dead", lambda o, a: o["overall"]["dead"])
    print()
    for kind in ("call", "done", "answer", "none"):
        if any(kind in out[n]["by_kind"] for n in arms):
            row(f"{kind}: strict %",
                lambda o, a, k=kind: _pct(o["by_kind"].get(k, _blank()), "strict"))
            row(f"{kind}: salvaged %",
                lambda o, a, k=kind: _pct(o["by_kind"].get(k, _blank()), "salvaged"))
            row(f"{kind}: dead %",
                lambda o, a, k=kind: _pct(o["by_kind"].get(k, _blank()), "dead"))
            print()
    row("incomplete done objects", lambda o, a: o["incomplete_done"])
    row("output tokens", lambda o, a: o["output_tokens"])
    row("generation seconds", lambda o, a: o["generation_seconds"])
    row("defects/run (hunt)",
        lambda o, a: a["aggregate"]["found_hunt"]["mean"])
    row("defects/run (final)",
        lambda o, a: a["aggregate"]["found_final"]["mean"])
    row("defects union across runs",
        lambda o, a: f"{a['aggregate']['union_count']}/9")
    row("findings raised/run", lambda o, a: a["aggregate"]["raised"]["mean"])
    row("findings survived/run", lambda o, a: a["aggregate"]["survived"]["mean"])

    for name in arms:
        top = out[name]["top_reasons"]
        if top:
            print(f"\narm {name}, why replies were not strict:")
            for reason, count in top.items():
                print(f"  {count:>4}  {reason}")

    (RESULTS / "analysis.json").write_text(
        json.dumps(out, indent=2), encoding="utf-8")
    print(f"\nwritten to {RESULTS / 'analysis.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
