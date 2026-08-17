"""What the model actually wrote, judged before anything forgives it.

The arena's parser is deliberately generous. It reads a tool call in whichever
shape the model chose, pulls JSON out of prose and code fences, and rescues
line numbers written as "42-45". That generosity was worth building: it took a
real run from nothing surviving to nine findings out of ten, because verifiers
that cannot be understood are recorded as having failed, and a finding whose
verifiers all failed is a finding that gets thrown out.

Which makes it exactly the wrong instrument to measure with. Run the
unconstrained arm through a parser built to rescue it and the arm looks
healthy, because every rescue is invisible. The grammar would then appear to
fix a problem that had already been hidden.

So every reply is judged three ways:

  strict    the documented protocol, exactly. json.loads on the raw text
            returns an object that is a tool call with a known name and a dict
            of arguments, or a done object. Nothing was forgiven.
  salvaged  not strict, but the arena's tolerant path recovered a usable
            action. The step still worked. It only worked because of code
            written to absorb this.
  dead      neither. The step is burned: the agent is told its reply could not
            be read and loses one of its allowance.

Only the third costs a run anything directly, and it is the honest headline.
The second is the interesting number, because it measures how much the tolerant
parser is carrying.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from crucible.orchestrator import _as_tool_call, _extract_json

# The canonical spelling, as the tool manual and every system prompt document
# it. Anything else is a deviation even when the arena accepts it.
CANONICAL_CALL_KEYS = {"tool", "args"}


@dataclass
class Verdict:
    grade: str          # strict | salvaged | dead
    kind: str           # call | done | answer | none
    tool: str = ""
    reason: str = ""    # why it was not strict, when it was not


def _is_strict_call(obj: dict, known: set[str]) -> tuple[bool, str]:
    if set(obj) != CANONICAL_CALL_KEYS:
        extra = sorted(set(obj) - CANONICAL_CALL_KEYS)
        missing = sorted(CANONICAL_CALL_KEYS - set(obj))
        bits = []
        if missing:
            bits.append(f"missing {', '.join(missing)}")
        if extra:
            bits.append(f"unexpected {', '.join(extra)}")
        return False, "; ".join(bits) or "wrong keys"
    if not isinstance(obj.get("tool"), str) or obj["tool"] not in known:
        return False, f"unknown tool {obj.get('tool')!r}"
    if not isinstance(obj.get("args"), dict):
        return False, "args is not an object"
    return True, ""


def classify(text: str, known: set[str], role: str = "") -> Verdict:
    """Grade one raw model reply.

    The role matters because the planner does not play the same game as the
    others. It never calls a tool and never says done; its whole answer is
    {"lanes": [...]}, which is a correct reply that looks like neither shape.
    Graded without knowing that, every planner call in every arm scores dead
    while the run visibly proceeds on the lanes it produced, and the dead count
    carries a constant lie in it.
    """
    raw = text.strip()

    if role == "planner":
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError as exc:
            obj = None
            why = f"not valid JSON: {exc.msg}"
        else:
            why = ""
        if isinstance(obj, dict) and isinstance(obj.get("lanes"), list):
            return Verdict("strict", "answer")
        salvaged = _extract_json(raw)
        if isinstance(salvaged, dict) and isinstance(salvaged.get("lanes"), list):
            return Verdict("salvaged", "answer", reason=why or "wrapped")
        return Verdict("dead", "none", reason=why or "no lanes in reply")

    # ---- strict: the whole reply is the object, spelled as documented.
    strict_obj = None
    strict_error = ""
    try:
        candidate = json.loads(raw)
        if isinstance(candidate, dict):
            strict_obj = candidate
        else:
            strict_error = f"top level is {type(candidate).__name__}, not an object"
    except json.JSONDecodeError as exc:
        strict_error = f"not valid JSON: {exc.msg}"

    if strict_obj is not None:
        if strict_obj.get("done"):
            return Verdict("strict", "done")
        ok, why = _is_strict_call(strict_obj, known)
        if ok:
            return Verdict("strict", "call", tool=strict_obj["tool"])
        strict_error = why

    # ---- salvaged: the arena's tolerant path gets something usable out of it.
    parsed = _extract_json(raw)
    if isinstance(parsed, dict):
        if parsed.get("done"):
            return Verdict("salvaged", "done", reason=strict_error or "wrapped")
        call = _as_tool_call(parsed, known)
        if call is not None:
            return Verdict("salvaged", "call", tool=call[0],
                           reason=strict_error or "non-canonical shape")

    return Verdict("dead", "none", reason=strict_error or "no object found")


@dataclass
class Tally:
    """Counts for one arm, kept per role as well as overall."""

    strict: int = 0
    salvaged: int = 0
    dead: int = 0
    calls: int = 0
    dones: int = 0
    reasons: dict = field(default_factory=dict)
    by_role: dict = field(default_factory=dict)

    @property
    def total(self) -> int:
        return self.strict + self.salvaged + self.dead

    def add(self, verdict: Verdict, role: str) -> None:
        setattr(self, verdict.grade, getattr(self, verdict.grade) + 1)
        if verdict.kind == "call":
            self.calls += 1
        elif verdict.kind in ("done", "answer"):
            self.dones += 1
        if verdict.grade != "strict" and verdict.reason:
            key = verdict.reason[:70]
            self.reasons[key] = self.reasons.get(key, 0) + 1
        slot = self.by_role.setdefault(
            role, {"strict": 0, "salvaged": 0, "dead": 0}
        )
        slot[verdict.grade] += 1

    def rate(self, grade: str) -> float:
        return (getattr(self, grade) / self.total * 100) if self.total else 0.0

    def as_dict(self) -> dict:
        return {
            "total": self.total,
            "strict": self.strict, "salvaged": self.salvaged, "dead": self.dead,
            "strict_pct": round(self.rate("strict"), 1),
            "salvaged_pct": round(self.rate("salvaged"), 1),
            "dead_pct": round(self.rate("dead"), 1),
            "calls": self.calls, "dones": self.dones,
            "by_role": self.by_role,
            "top_reasons": dict(sorted(self.reasons.items(),
                                       key=lambda kv: -kv[1])[:8]),
        }
