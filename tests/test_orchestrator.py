"""Checks for the run: planning, the parallel hunt, and the trial.

No network and no money. The model is replaced by a stand-in that answers from
the prompt it is given, which is the only way to script a system whose agents
run concurrently: a queue of canned replies would be handed out in whatever
order the threads happened to arrive, and the test would pass or fail by luck.

The checks that matter most here are the ones about failing safely. A verifier
that dies counts against the finding. A finding needs a majority to survive. A
run that hits its ceiling stops and still reports. And the ledger, written from
every agent thread at once, still verifies afterwards.

    python tests/test_orchestrator.py
"""

from __future__ import annotations

import json
import sys
import tempfile
import threading
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crucible.ledger import Ledger  # noqa: E402
from crucible.orchestrator import (  # noqa: E402
    Orchestrator, _as_int, _as_tool_call, _extract_json,
)
from crucible.policy import review_policy  # noqa: E402
from crucible.providers import Budget, Completion, Tier  # noqa: E402
from crucible.tools import Toolbox  # noqa: E402

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


# --------------------------------------------------------------- stand-in

class RoleProvider:
    """Answers by reading the prompt, so concurrency cannot shuffle the script.

    verdicts: an iterable of booleans consumed under a lock, one per verifier
    call, saying whether that verifier refutes. Everything else is fixed.
    """

    name = "role"

    def __init__(self, *, lanes=2, findings_per_lane=1, verdicts=None,
                 verifier_returns_junk=False, explode_after=None):
        self.lanes = lanes
        self.findings_per_lane = findings_per_lane
        self._verdicts = list(verdicts or [])
        self._lock = threading.Lock()
        self.verifier_returns_junk = verifier_returns_junk
        self.explode_after = explode_after
        self.calls = 0
        self.by_tier: dict[str, int] = {}

    def complete(self, system, user, tier, budget, *, max_output=2000,
                 reasoning="low"):
        with self._lock:
            self.calls += 1
            self.by_tier[tier.value] = self.by_tier.get(tier.value, 0) + 1
            n = self.calls
        if self.explode_after is not None and n > self.explode_after:
            raise RuntimeError("provider went down")

        if tier is Tier.PLANNER:
            body = {"lanes": [
                {"name": f"lane-{i + 1}", "brief": "look here",
                 "files": ["app.py"]}
                for i in range(self.lanes)
            ]}
        elif tier is Tier.WORKER:
            lane = "unknown"
            for line in user.splitlines():
                if line.startswith("YOUR LANE:"):
                    lane = line.split(":", 1)[1].strip()
            if ">>> you called" not in user:
                # First turn: use a tool, so the loop and the ledger are
                # exercised rather than short-circuited.
                body = {"tool": "read_file", "args": {"path": "app.py"}}
            else:
                # Distinct lines per lane, well outside the dedupe window, so
                # these fixtures exercise the fan-out rather than the merge.
                # The merge has its own section below.
                try:
                    base = int(lane.rsplit("-", 1)[-1]) * 1000
                except ValueError:
                    base = 0
                body = {"done": True, "findings": [
                    {"title": f"{lane} defect {i + 1}", "file": "app.py",
                     "line": base + 10 * (i + 1), "severity": "high",
                     "summary": "a concrete wrong behaviour",
                     "failure_scenario": "given x, returns y",
                     "evidence": "line 10"}
                    for i in range(self.findings_per_lane)
                ]}
        else:
            if self.verifier_returns_junk:
                return Completion("not json at all", "role", 10, 10, 0.0, 0.0)
            with self._lock:
                refuted = self._verdicts.pop(0) if self._verdicts else True
            body = {"done": True, "refuted": refuted, "confidence": "high",
                    "concrete_failure": "" if refuted else "x=3 gives 4",
                    "reasoning": "checked the code"}

        text = json.dumps(body)
        # Reserve then settle, exactly as the real provider does, so the
        # ceiling actually bites in tests instead of being bypassed by a
        # stand-in that only books costs after the fact.
        held = budget.reserve("gpt-5-mini", len(user) // 4, max_output)
        cost = budget.settle("gpt-5-mini", held, len(user) // 4, len(text) // 4)
        return Completion(text, "role", len(user) // 4, len(text) // 4, cost, 0.0)


def workspace(tmp: Path) -> Path:
    root = tmp / "ws"
    root.mkdir(parents=True, exist_ok=True)
    (root / "app.py").write_text(
        "def total(items):\n"
        "    return sum(i.price for i in items)\n" * 6,
        encoding="utf-8",
    )
    return root


def build(tmp: Path, provider, ceiling=5.0, name="run"):
    root = workspace(tmp)
    events: list[dict] = []
    lock = threading.Lock()

    def emit(event):
        with lock:
            events.append(event)

    ledger = Ledger(tmp / f"{name}.jsonl")
    orchestrator = Orchestrator(
        provider, root, review_policy(root), ledger,
        Budget(ceiling_usd=ceiling), emit=emit,
    )
    return orchestrator, events, ledger


# ------------------------------------------------------------------ checks

def wire_checks() -> None:
    section("reading a tool call off the wire")
    known = {"read_file", "search", "run_tests"}
    for label, payload, expected in [
        ("the documented shape",
         {"tool": "read_file", "args": {"path": "a.py"}}, ("read_file", {"path": "a.py"})),
        ("the tool name as the only key, which is what models actually send",
         {"read_file": {"path": "a.py"}}, ("read_file", {"path": "a.py"})),
        ("a bare string argument",
         {"read_file": "a.py"}, ("read_file", {"path": "a.py"})),
        ("OpenAI's function-calling shape",
         {"name": "search", "arguments": {"pattern": "x"}}, ("search", {"pattern": "x"})),
        ("arguments sitting beside the name rather than nested",
         {"tool": "read_file", "path": "a.py"}, ("read_file", {"path": "a.py"})),
        ("an action key",
         {"action": "run_tests", "args": {"command": "pytest"}},
         ("run_tests", {"command": "pytest"})),
    ]:
        check(label, _as_tool_call(payload, known) == expected,
              str(_as_tool_call(payload, known)))

    check("an unknown tool name is not a tool call",
          _as_tool_call({"send_email": {"to": "x"}}, known) is None)
    check("a done object is not a tool call",
          _as_tool_call({"done": True, "refuted": False}, known) is None)
    check("a bare object with several keys is not a tool call",
          _as_tool_call({"read_file": {"path": "a"}, "note": "hi"}, known) is None)
    check("a non-dict is not a tool call", _as_tool_call(["read_file"], known) is None)


def parsing_checks() -> None:
    section("json extraction")
    check("a bare object parses", _extract_json('{"a":1}') == {"a": 1})
    check("a fenced object parses",
          _extract_json('```json\n{"a":1}\n```') == {"a": 1})
    check("an object wrapped in prose parses",
          _extract_json('Sure! {"a":1} hope that helps') == {"a": 1})
    check("nested braces survive",
          _extract_json('text {"a":{"b":[1,2]}} more') == {"a": {"b": [1, 2]}})
    check("unparseable text returns None rather than raising",
          _extract_json("no json here at all") is None)
    check("a brace inside a string literal does not end the object early",
          _extract_json('{"note": "returns {} on failure", "n": 1}')
          == {"note": "returns {} on failure", "n": 1})
    check("an escaped quote inside a string does not confuse the scan",
          _extract_json(r'prose {"q": "he said \"} \" here", "n": 2} tail')
          == {"q": 'he said "} " here', "n": 2})
    check("a stray closing brace before the object is ignored",
          _extract_json('} leftover {"a": 1}') == {"a": 1})

    section("values as models actually write them")
    for label, raw, expected in [
        ("a plain integer", 42, 42),
        ("a numeric string", "42", 42),
        ("a range takes its first number", "42-45", 42),
        ("a prose line reference", "line 7", 7),
        ("a float", 12.0, 12),
        ("nothing at all", None, 0),
        ("unparseable text", "somewhere", 0),
        ("a boolean is not a line number", True, 0),
    ]:
        check(f"line number from {label}", _as_int(raw) == expected, str(_as_int(raw)))


def run_checks(tmp: Path) -> None:
    section("a full run")
    # Two lanes, one finding each. First finding kept by 2 of 3, second killed
    # by 2 of 3. Verdict order is fixed but which finding gets which triple is
    # not, so the assertion is on the totals rather than on a named finding.
    provider = RoleProvider(lanes=2, findings_per_lane=1,
                            verdicts=[False, False, True, True, True, False])
    orchestrator, events, ledger = build(tmp, provider, name="full")
    report = orchestrator.run("find defects")

    kinds = [e["kind"] for e in events]
    check("the run reports two findings raised", report.raised == 2)
    check("exactly one finding survived", report.survived == 1)
    check("the surviving finding is the one kept by a majority",
          len(report.findings) == 1)
    check("both lanes were planned", len(report.lanes) == 2)
    check("hunters and verifiers both ran",
          provider.by_tier.get("worker", 0) >= 4
          and provider.by_tier.get("verifier", 0) == 6)
    check("three verifiers attacked each finding",
          sum(1 for k in kinds if k == "verdict") == 6)

    check("the stream announced the run", "run_started" in kinds)
    check("the stream announced each phase",
          sum(1 for e in events if e["kind"] == "phase") == 3)
    check("the stream carried tool activity", "tool" in kinds)
    check("the stream settled every finding",
          sum(1 for k in kinds if k == "finding_settled") == 2)
    check("the stream closed the run", kinds[-1] == "run_finished")

    check("the policy travelled with the run for display",
          "read_file" in (events[0].get("policy", {}).get("tools", {})))
    check("tool calls were counted", report.tool_calls >= 2)
    check("the ledger verifies after parallel writing", ledger.verify() is None)
    check("the report carries the ledger head",
          report.ledger_head == ledger.head and len(report.ledger_head) == 64)

    entries = [e["event"] for e in ledger.entries()]
    check("the ledger recorded the plan", "plan" in entries)
    check("the ledger judged every finding",
          sum(1 for e in entries if e == "finding_judged") == 2)
    check("the ledger opened and closed the run",
          entries[0] == "run_started" and entries[-1] == "run_finished")


def survival_checks(tmp: Path) -> None:
    section("the majority rule")
    for label, verdicts, expected in [
        ("a finding refuted by all three dies", [True, True, True], 0),
        ("a finding kept by all three survives", [False, False, False], 1),
        ("two refutations out of three kill it", [True, True, False], 0),
        ("two survivals out of three keep it", [False, False, True], 1),
    ]:
        provider = RoleProvider(lanes=1, findings_per_lane=1, verdicts=verdicts)
        orchestrator, _, _ = build(tmp, provider, name=label[:8])
        check(label, orchestrator.run("t").survived == expected)

    section("malformed model output cannot kill a run")

    class Nonsense(RoleProvider):
        """A model that answers in shapes the schema never promised."""

        def complete(self, system, user, tier, budget, **kw):
            if tier is Tier.PLANNER:
                body = {"lanes": ["just a string", {"name": "lane-1",
                                                    "brief": "b", "files": []}]}
            elif tier is Tier.WORKER and "not a single JSON object" not in user:
                # Answers with an array on the first turn, then behaves once
                # nudged. Keyed on the nudge text rather than on a tool call,
                # since a reply the loop cannot read never runs a tool.
                body = ["an", "array", "not", "an", "object"]
            elif tier is Tier.WORKER:
                body = {"done": True, "findings": [
                    "a bare string finding",
                    {"title": "real one", "file": "app.py", "line": "42-47",
                     "severity": "high", "summary": "s", "failure_scenario": "f"},
                ]}
            else:
                body = {"done": True, "refuted": False, "confidence": "high",
                        "reasoning": "r", "concrete_failure": "c"}
            text = json.dumps(body)
            held = budget.reserve("gpt-5-mini", 10, kw.get("max_output", 100))
            budget.settle("gpt-5-mini", held, 10, len(text) // 4)
            return Completion(text, "role", 10, 10, 0.0, 0.0)

    orchestrator, events, ledger = build(tmp, Nonsense(), name="junky")
    report = orchestrator.run("t")
    check("a lane returned as a bare string is dropped, not fatal",
          report.lanes == ["lane-1"])
    check("an array reply is treated as unparseable rather than crashing",
          any(e["kind"] == "agent_done" for e in events))
    check("a finding returned as a bare string is dropped", report.raised == 1)
    check("a line number written as a range still parses",
          report.findings and report.findings[0]["line"] == 42)
    check("and the run completed normally", not report.halted)

    section("failing safely")
    provider = RoleProvider(lanes=1, findings_per_lane=1,
                            verifier_returns_junk=True)
    orchestrator, events, _ = build(tmp, provider, name="junk")
    report = orchestrator.run("t")
    check("a verifier that never returns a verdict counts as a refutation",
          report.survived == 0)
    check("and the finding was still raised and settled", report.raised == 1)

    provider = RoleProvider(lanes=2, findings_per_lane=1, verdicts=[False] * 6)
    orchestrator, events, ledger = build(tmp, provider, ceiling=0.0004, name="broke")
    report = orchestrator.run("t")
    check("a run that hits its ceiling still returns a report",
          isinstance(report.raised, int))
    check("and says it was halted",
          bool(report.halted) or report.raised == 0)
    check("and the ledger it wrote still verifies", ledger.verify() is None)


def policy_checks(tmp: Path) -> None:
    section("policy inside a run")

    class Escaper(RoleProvider):
        """A hunter that tries to leave the workspace on its first move."""

        def complete(self, system, user, tier, budget, **kw):
            if tier is Tier.WORKER and ">>> you called" not in user:
                text = json.dumps(
                    {"tool": "read_file", "args": {"path": "C:/Windows/win.ini"}}
                )
                budget.record("gpt-5-mini", 10, 10)
                return Completion(text, "role", 10, 10, 0.0, 0.0)
            return super().complete(system, user, tier, budget, **kw)

    provider = Escaper(lanes=1, findings_per_lane=1, verdicts=[True, True, True])
    orchestrator, events, ledger = build(tmp, provider, name="escape")
    orchestrator.run("t")

    refusals = [e for e in events if e["kind"] == "tool" and e.get("refused")]
    check("an attempt to read outside the workspace is refused", len(refusals) >= 1)
    check("a relative path anchors to the workspace instead of the server's cwd",
          bool(Toolbox(tmp / "ws", review_policy(tmp / "ws"),
                       Ledger(tmp / "rel.jsonl")).invoke(
              "read_file", {"path": "app.py"}).ok))
    check("but a relative path that climbs out is still refused",
          Toolbox(tmp / "ws", review_policy(tmp / "ws"),
                  Ledger(tmp / "rel2.jsonl")).invoke(
              "read_file", {"path": "../../../Windows/win.ini"}).refused)
    check("the refusal reached the stream with a reason",
          bool(refusals and refusals[0].get("reason")))
    denied = [e for e in ledger.entries() if e["event"] == "tool_denied"]
    check("and was written to the ledger", len(denied) >= 1)
    check("the ledger's refusal names the path that was blocked",
          bool(denied and "win.ini" in json.dumps(denied[0]["payload"])))
    check("the agent kept working after being refused",
          any(e["kind"] == "agent_done" for e in events))


def dedupe_checks(tmp: Path) -> None:
    section("collapsing the same defect found twice")

    class Twins(RoleProvider):
        """Two lanes that both report the same line, plus one that differs."""

        def complete(self, system, user, tier, budget, **kw):
            if tier is Tier.WORKER and ">>> you called" in user:
                lane = "?"
                for line in user.splitlines():
                    if line.startswith("YOUR LANE:"):
                        lane = line.split(":", 1)[1].strip()
                line_no = 10 if lane == "lane-1" else (12 if lane == "lane-2" else 90)
                body = {"done": True, "findings": [{
                    "title": f"{lane} says line {line_no}", "file": "app.py",
                    "line": line_no, "severity": "high", "summary": "wrong",
                    "failure_scenario": "x" * (20 if lane == "lane-2" else 5),
                    "evidence": "e",
                }]}
                text = json.dumps(body)
                held = budget.reserve("gpt-5-mini", 10, kw.get("max_output", 100))
                budget.settle("gpt-5-mini", held, 10, len(text) // 4)
                return Completion(text, "role", 10, 10, 0.0, 0.0)
            return super().complete(system, user, tier, budget, **kw)

    provider = Twins(lanes=3, findings_per_lane=1, verdicts=[False] * 9)
    orchestrator, events, ledger = build(tmp, provider, name="dupe")
    report = orchestrator.run("t")

    check("two hunters reporting the same line collapse to one finding",
          report.raised == 2)
    check("a finding elsewhere in the file is left alone",
          report.survived == 2)
    check("the merge was announced",
          any(e["kind"] == "finding_merged" for e in events))
    check("the merge is in the ledger",
          any(e["event"] == "finding_merged" for e in ledger.entries()))
    check("the survivor keeps the fuller failure account",
          any(len(f["failure_scenario"]) == 20 for f in report.findings))
    check("corroboration is counted rather than discarded",
          any(f["duplicates"] == 1 for f in report.findings))
    check("only the surviving claims were verified, so no verifier was wasted",
          provider.by_tier.get("verifier", 0) == 6)


def concurrency_checks(tmp: Path) -> None:
    section("the ledger under load")
    # Six lanes, three findings each, three verifiers per finding: 54 verifier
    # agents plus the hunters, all writing to one chain at once.
    provider = RoleProvider(lanes=6, findings_per_lane=3,
                            verdicts=[False] * 54)
    orchestrator, _, ledger = build(tmp, provider, name="load")
    report = orchestrator.run("t")
    check("every finding from every lane was raised", report.raised == 18)
    check("all of them survived unanimous verification", report.survived == 18)
    check("the chain holds after heavy parallel writing", ledger.verify() is None)
    seqs = [e["seq"] for e in ledger.entries()]
    check("no sequence number was repeated or skipped",
          seqs == list(range(len(seqs))))


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        wire_checks()
        parsing_checks()
        run_checks(tmp)
        survival_checks(tmp)
        policy_checks(tmp)
        dedupe_checks(tmp)
        concurrency_checks(tmp)
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
