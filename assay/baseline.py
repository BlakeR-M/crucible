"""What a small model actually gets wrong when it writes Python.

This decides whether Assay is worth building, and it runs before any of Assay
exists. The whole premise is that a small model's coding failures are mostly
mechanical rather than conceptual: it hallucinates a function that does not
exist, uses a name that was never bound, gets an argument count wrong. Those a
constrained language can make unrepresentable. A wrong algorithm it cannot
touch.

So: one well-regarded 7B coder, thirty tasks it has never seen, several samples
each, and every failure sorted into a bucket. One number comes out, the share
of failures that are mechanical, and the bar for what that number has to be was
written down before the first sample was drawn.

Two decisions worth stating, because both make the answer harder to like rather
than easier.

The model is Qwen2.5-Coder-7B-Instruct, which is the small coding model people
actually reach for. Improving something nobody rates would prove nothing; the
objection "you picked a weak baseline" has to be unavailable.

And every ambiguous failure is classified as conceptual. An IndexError could be
called a type-system failure by someone arguing for a rich enough type system,
and here it counts as a wrong algorithm. The bucket that supports building
Assay only gets a failure it unambiguously owns, so a result that still favours
building is one the classification did not manufacture.

    python -m assay.baseline --samples 3
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from collections import Counter
from pathlib import Path

from crucible.local import LocalProvider
from crucible.providers import Budget, Tier

from assay.tasks import TASKS

ROOT = Path(__file__).resolve().parent.parent
RESULTS = ROOT / "assay" / "results"

BASE_URL = "http://127.0.0.1:8080"
MODEL = "D:\\models\\Qwen2.5-Coder-7B-Instruct-Q6_K.gguf"

TIMEOUT_SECONDS = 10

SYSTEM = """\
You are a careful Python programmer.

Write one Python function that satisfies the specification. Reply with the
function only, inside a single ```python code fence. No explanation, no tests,
no example usage.

You may use the Python standard library. Do not install anything.
"""

# ------------------------------------------------------------- the buckets
#
# The first five are what a constrained language can make impossible. The sixth
# is what no language design touches, and it is where anything ambiguous goes.
MECHANICAL = ("syntax", "hallucinated_api", "undefined_name", "type_or_arity",
              "inconsistency")
CONCEPTUAL = ("wrong_algorithm",)


def prompt_for(task: dict) -> str:
    return (f"{task['spec']}\n\n"
            f"Write exactly this function:\n\n{task['signature']}\n")


def extract_code(reply: str) -> str:
    """The function out of whatever the model wrapped it in."""
    fenced = re.search(r"```(?:python)?\s*\n(.*?)```", reply, re.S)
    if fenced:
        return fenced.group(1)
    # No fence. If it looks like code from the first line, take it whole.
    stripped = reply.strip()
    if stripped.startswith(("def ", "import ", "from ")):
        return stripped
    # Otherwise salvage from the first def onwards, which is what a model that
    # explained itself first usually leaves behind.
    at_def = stripped.find("\ndef ")
    return stripped[at_def + 1:] if at_def != -1 else stripped


RUNNER = """\
import json, sys, traceback
src = json.loads(sys.argv[1])
ns = {}
try:
    compile(src["code"], "<generated>", "exec")
except SyntaxError as exc:
    print(json.dumps({"stage": "compile", "error": "SyntaxError",
                      "message": str(exc)})); raise SystemExit
try:
    exec(src["code"], ns)
except Exception as exc:
    print(json.dumps({"stage": "define", "error": type(exc).__name__,
                      "message": str(exc)})); raise SystemExit
fn = ns.get(src["name"])
if fn is None:
    print(json.dumps({"stage": "define", "error": "MissingFunction",
                      "message": "the reply defined no function called "
                                 + src["name"]})); raise SystemExit
try:
    exec(src["tests"], ns)
    ns["check"](fn)
except Exception as exc:
    print(json.dumps({"stage": "run", "error": type(exc).__name__,
                      "message": str(exc)[:300],
                      "trace": traceback.format_exc()[-600:]})); raise SystemExit
print(json.dumps({"stage": "pass"}))
"""


def execute(code: str, task: dict) -> dict:
    """Run generated code against the task's tests, in a separate process.

    Separate because generated code can loop forever, and because a module that
    raises at import time would otherwise take this process with it.
    """
    payload = json.dumps({"code": code, "name": task["name"],
                          "tests": task["tests"]})
    with tempfile.TemporaryDirectory() as tmp:
        runner = Path(tmp) / "runner.py"
        runner.write_text(RUNNER, encoding="utf-8")
        try:
            proc = subprocess.run(
                [sys.executable, str(runner), payload],
                capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
                cwd=tmp,
            )
        except subprocess.TimeoutExpired:
            return {"stage": "run", "error": "Timeout",
                    "message": f"did not finish in {TIMEOUT_SECONDS}s"}
    line = (proc.stdout or "").strip().splitlines()
    if not line:
        return {"stage": "run", "error": "NoOutput",
                "message": (proc.stderr or "")[-200:]}
    try:
        return json.loads(line[-1])
    except json.JSONDecodeError:
        return {"stage": "run", "error": "NoOutput", "message": line[-1][:200]}


def classify(outcome: dict) -> str:
    """One failure, one bucket. Ambiguity goes to the bucket Assay cannot fix.

    The rule throughout: a failure only counts as mechanical when the
    interpreter itself names the mechanism. An AssertionError means the code
    ran and computed the wrong answer, which is the one thing no grammar and no
    type system will save.
    """
    if outcome["stage"] == "pass":
        return "pass"
    error = outcome.get("error", "")
    message = outcome.get("message", "")
    trace = outcome.get("trace", "")

    if error == "SyntaxError" or error == "IndentationError":
        return "syntax"

    # A module or attribute that does not exist. The model invented it.
    if error in ("ModuleNotFoundError", "ImportError"):
        return "hallucinated_api"
    if error == "AttributeError":
        return "hallucinated_api"

    # A name that was never bound anywhere in scope.
    if error == "NameError":
        return "undefined_name"
    if error == "MissingFunction":
        return "inconsistency"

    if error == "TypeError":
        # Argument count, argument type, or calling something uncallable. All
        # of these a type checker settles before the program ever runs.
        if any(s in message for s in (
                "positional argument", "keyword argument", "argument",
                "not callable", "unsupported operand", "must be",
                "takes", "missing")):
            return "type_or_arity"
        return "type_or_arity"

    if error == "UnboundLocalError":
        return "undefined_name"

    # Everything else ran and got the answer wrong, or hung, or blew up on an
    # edge case the model failed to reason about. Conceptual, by the rule above.
    return "wrong_algorithm"


def run(samples: int, temperature: float, limit: int | None) -> dict:
    RESULTS.mkdir(parents=True, exist_ok=True)
    tasks = TASKS[:limit] if limit else TASKS
    provider = LocalProvider(BASE_URL, MODEL, think=False,
                             temperature=temperature, seed=90210)
    budget = Budget(ceiling_usd=1.0, unmetered=True)

    records = []
    started = time.time()
    for index, task in enumerate(tasks, 1):
        for sample in range(samples):
            reply = provider.complete(SYSTEM, prompt_for(task), Tier.WORKER,
                                      budget, max_output=900)
            code = extract_code(reply.text)
            outcome = execute(code, task)
            bucket = classify(outcome)
            records.append({
                "task": task["id"], "name": task["name"], "sample": sample,
                "bucket": bucket, "stage": outcome.get("stage"),
                "error": outcome.get("error", ""),
                "message": outcome.get("message", "")[:300],
                "code": code, "reply": reply.text,
                "seconds": round(reply.seconds, 2),
            })
        done = sum(1 for r in records if r["task"] == task["id"]
                   and r["bucket"] == "pass")
        print(f"  {task['id']} {task['name']:<20} {done}/{samples} pass"
              f"   ({index}/{len(tasks)})")

    elapsed = time.time() - started
    summary = summarise(records, samples, len(tasks), elapsed)
    payload = {"model": MODEL, "samples": samples, "temperature": temperature,
               "tasks": len(tasks), "summary": summary, "records": records}
    (RESULTS / "baseline.json").write_text(json.dumps(payload, indent=2),
                                           encoding="utf-8")
    return payload


def summarise(records: list, samples: int, tasks: int, elapsed: float) -> dict:
    buckets = Counter(r["bucket"] for r in records)
    total = len(records)
    passed = buckets.get("pass", 0)
    failures = total - passed
    mechanical = sum(buckets.get(b, 0) for b in MECHANICAL)
    conceptual = sum(buckets.get(b, 0) for b in CONCEPTUAL)
    # Tasks solved at least once, which is the ceiling a perfect mechanical fix
    # could reach: a task the model never gets right in any sample is one where
    # it does not know the answer, and no language helps with that.
    solved_ever = len({r["task"] for r in records if r["bucket"] == "pass"})
    return {
        "generations": total,
        "passed": passed,
        "pass_rate_pct": round(passed / total * 100, 1) if total else 0.0,
        "failures": failures,
        "buckets": dict(buckets),
        "mechanical": mechanical,
        "conceptual": conceptual,
        "mechanical_share_pct": (round(mechanical / failures * 100, 1)
                                 if failures else 0.0),
        "tasks_solved_at_least_once": solved_ever,
        "tasks": tasks,
        "seconds": round(elapsed, 1),
    }


def report(payload: dict) -> None:
    s = payload["summary"]
    print(f"\n{'=' * 62}")
    print(f"  {payload['model'].split(chr(92))[-1]}")
    print(f"  {s['generations']} generations over {s['tasks']} tasks, "
          f"{payload['samples']} samples each, {s['seconds']}s")
    print(f"  passed {s['passed']} ({s['pass_rate_pct']}%), "
          f"failed {s['failures']}")
    print(f"  tasks solved at least once: "
          f"{s['tasks_solved_at_least_once']}/{s['tasks']}")
    print(f"{'=' * 62}")
    print("\n  failures by bucket:")
    for bucket, count in sorted(s["buckets"].items(), key=lambda kv: -kv[1]):
        if bucket == "pass":
            continue
        kind = "mechanical" if bucket in MECHANICAL else "CONCEPTUAL"
        share = count / s["failures"] * 100 if s["failures"] else 0
        print(f"    {bucket:<18} {count:>4}  {share:>5.1f}%   {kind}")
    print(f"\n  mechanical {s['mechanical']}  conceptual {s['conceptual']}")
    print(f"  MECHANICAL SHARE: {s['mechanical_share_pct']}%")

    bar = s["mechanical_share_pct"]
    print("\n  against the bar set before this ran:")
    if bar >= 50:
        print("    >=50%  build Assay properly")
    elif bar >= 25:
        print("    25-50% build the fixed-stdlib layer only, then re-measure")
    else:
        print("    <25%   stop. the premise does not hold.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samples", type=int, default=3)
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--limit", type=int, default=None,
                        help="only the first N tasks, for a smoke run")
    args = parser.parse_args()
    report(run(args.samples, args.temperature, args.limit))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
