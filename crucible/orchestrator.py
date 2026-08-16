"""The run: plan, hunt in parallel, then try to destroy what was found.

The shape here is the argument the whole project makes. Generating findings is
easy and almost worthless, because a model asked to find bugs will always find
bugs, and most of them will be confident and wrong. The value is in the second
half, where every finding is handed to independent verifiers whose instructions
are to refute it, who never see each other's verdicts, and who are told to call
it refuted when they cannot construct a concrete failure.

So the headline number a run produces is not how many findings it made. It is
how few survived.

Three roles, three price points. The planner divides the ground once. Hunters
fan out cheap and wide, each blind to the others so they do not converge on the
same easy answer. Verifiers are expensive because that is the seat where being
right matters, and there are several of them per finding.
"""

from __future__ import annotations

import json
import re
import threading
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field, asdict
from pathlib import Path

from .ledger import Ledger
from .policy import Policy
from .providers import Budget, BudgetExceeded, Tier
from .tools import TOOL_MANUAL, Toolbox

MAX_AGENT_STEPS = 14
VERIFIERS_PER_FINDING = 3
# A finding needs a clear majority of verifiers to fail to refute it. With three
# verifiers that means two, so a single sympathetic verifier cannot carry a
# finding through on its own.
SURVIVAL_THRESHOLD = 2


# --------------------------------------------------------------------- types

@dataclass
class Finding:
    id: str
    lane: str
    title: str
    file: str
    line: int
    severity: str
    summary: str
    failure_scenario: str
    evidence: str = ""
    verdicts: list = field(default_factory=list)
    survived: bool = False

    @property
    def refutations(self) -> int:
        return sum(1 for v in self.verdicts if v.get("refuted"))

    @property
    def survivals(self) -> int:
        return sum(1 for v in self.verdicts if not v.get("refuted"))


@dataclass
class Report:
    run_id: str
    task: str
    lanes: list
    raised: int
    survived: int
    findings: list
    spend_usd: float
    calls: int
    tool_calls: int
    refusals: int
    ledger_head: str
    seconds: float
    halted: str = ""


# ----------------------------------------------------------------- prompting

PLANNER_SYSTEM = """\
You divide a code review into independent lanes of investigation.

You are given a file listing for a codebase. Produce between 4 and 6 lanes.
Each lane is a distinct class of defect, chosen for THIS codebase rather than
from a generic list, and each names the files most worth reading for it.

Lanes must not overlap. Two hunters working the same ground is waste, and it is
also how a review ends up with four copies of the same easy finding.

Reply with JSON only:
{"lanes": [{"name": "short name", "brief": "what to look for and why here",
            "files": ["path", "path"]}]}
"""

HUNTER_SYSTEM = """\
You are hunting for real defects in one lane of a code review.

{manual}

Work by reading the code. Do not guess from filenames. A finding you cannot
point at a specific line for is not a finding.

The bar: a defect is a concrete wrong behaviour. Given some input or ordering,
the code produces a wrong result, corrupts state, crashes, or lets something
through it should not. Style, naming, missing docstrings and "could be clearer"
are not defects and reporting them costs you credibility.

Every step, reply with EXACTLY ONE JSON object and nothing else.

To use a tool:
{{"tool": "read_file", "args": {{"path": "C:/full/path/file.py"}}}}

When finished:
{{"done": true, "findings": [
  {{"title": "short label",
    "file": "path as given to you",
    "line": 42,
    "severity": "high|medium|low",
    "summary": "one sentence: what is wrong",
    "failure_scenario": "concrete inputs or ordering -> the wrong outcome",
    "evidence": "the lines you read that show it"}}]}}

Finding nothing is a legitimate answer: {{"done": true, "findings": []}}
Do not pad. Three real defects beat ten guesses.
"""

VERIFIER_SYSTEM = """\
You are trying to REFUTE a claimed defect. You are not reviewing the code and
you are not looking for other problems. One claim, one question: is it real?

{manual}

Read the actual code before deciding. The claim may misquote it.

Refute it if ANY of these hold:
- the described behaviour does not actually occur
- the code path is unreachable from any public entry point
- something upstream already prevents it
- it is a style preference rather than a wrong behaviour
- you cannot construct concrete inputs that produce the wrong outcome

**Default to refuted.** Uncertainty is a refutation. A finding that survives you
should be one you could demonstrate to the author with specific values in hand.

You are one of several verifiers working independently. You will not see their
verdicts and they will not see yours, so do not hedge toward an imagined
consensus.

Every step, reply with EXACTLY ONE JSON object and nothing else. Tools as above.

Final answer:
{{"done": true, "refuted": true|false,
  "confidence": "high|medium|low",
  "concrete_failure": "exact inputs and the wrong output, or empty if refuted",
  "reasoning": "one or two sentences"}}
"""


def _extract_json(text: str) -> dict | None:
    """Pull one JSON object out of a model reply.

    Models wrap JSON in prose and in code fences no matter how firmly they are
    told not to. Three attempts, cheapest first: the whole string, a fenced
    block, then the outermost balanced braces. Returning None rather than
    raising lets the caller nudge the agent instead of ending the run.
    """
    text = text.strip()
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fenced:
        try:
            return json.loads(fenced.group(1))
        except json.JSONDecodeError:
            pass
    depth, start = 0, None
    for i, ch in enumerate(text):
        if ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0 and start is not None:
                try:
                    return json.loads(text[start:i + 1])
                except json.JSONDecodeError:
                    start = None
    return None


# ------------------------------------------------------------- orchestrator

class Orchestrator:
    def __init__(self, provider, workspace: Path, policy: Policy, ledger: Ledger,
                 budget: Budget, emit=None, *, max_workers: int = 6):
        self.provider = provider
        self.workspace = Path(workspace).resolve()
        self.policy = policy
        self.ledger = ledger
        self.budget = budget
        self.max_workers = max_workers
        self._emit = emit or (lambda event: None)
        self._emit_lock = threading.Lock()
        self.toolbox = Toolbox(self.workspace, policy, ledger)

    def emit(self, kind: str, **payload) -> None:
        """One event out to whoever is watching, serialised.

        The stream is the demonstration, so it is written to from every agent
        thread. Without the lock two events interleave into one malformed line
        and the page showing them stops mid-run.
        """
        with self._emit_lock:
            self._emit({"kind": kind, **payload})

    # ---------------------------------------------------------- agent loop

    def _agent(self, agent_id: str, system: str, opening: str, tier: Tier,
               *, max_steps: int = MAX_AGENT_STEPS) -> dict | None:
        """One agent, looping tool call and result until it answers.

        Returns the agent's final object, or None if it ran out of steps or the
        budget stopped it. None is a real outcome that gets reported, rather
        than an exception that would take the whole run down with it.
        """
        tools = self.toolbox.for_agent(agent_id)
        transcript = opening
        for step in range(max_steps):
            try:
                reply = self.provider.complete(
                    system, transcript, tier, self.budget, max_output=2200
                )
            except BudgetExceeded as exc:
                self.emit("agent_halted", agent=agent_id, reason=str(exc))
                return None
            except RuntimeError as exc:
                self.emit("agent_error", agent=agent_id, reason=str(exc)[:200])
                return None

            self.emit("agent_thought", agent=agent_id, step=step,
                      model=reply.model, cost=round(reply.cost_usd, 5),
                      seconds=round(reply.seconds, 1))

            parsed = _extract_json(reply.text)
            if parsed is None:
                transcript += (
                    f"\n\nYour reply was not a single JSON object. Reply with one "
                    f"JSON object and nothing else."
                )
                continue

            if parsed.get("done"):
                self.emit("agent_done", agent=agent_id, step=step)
                return parsed

            tool = parsed.get("tool")
            if not tool:
                transcript += "\n\nReply with either a tool call or a done object."
                continue

            result = tools.invoke(tool, parsed.get("args", {}) or {})
            self.emit(
                "tool", agent=agent_id, tool=tool,
                args=self.toolbox._safe_args(parsed.get("args", {}) or {}),
                refused=result.refused, reason=result.reason,
                bytes=len(result.content),
            )
            transcript += (
                f"\n\n>>> you called {tool}({json.dumps(parsed.get('args', {}))})"
                f"\n{result.as_text()}"
            )

        self.emit("agent_exhausted", agent=agent_id, steps=max_steps)
        return None

    # -------------------------------------------------------------- phases

    def plan(self, task: str) -> list[dict]:
        self.emit("phase", phase="plan")
        listing = self.toolbox.for_agent("planner").invoke(
            "list_dir", {"path": str(self.workspace)}
        )
        prompt = (
            f"TASK: {task}\n\nWORKSPACE: {self.workspace}\n\n"
            f"FILES:\n{listing.as_text()}\n\nDivide this into lanes."
        )
        try:
            reply = self.provider.complete(
                PLANNER_SYSTEM, prompt, Tier.PLANNER, self.budget, max_output=1600
            )
        except (BudgetExceeded, RuntimeError) as exc:
            self.emit("phase_failed", phase="plan", reason=str(exc)[:200])
            return []
        parsed = _extract_json(reply.text) or {}
        lanes = [l for l in parsed.get("lanes", []) if l.get("name")][:6]
        self.ledger.append("plan", lanes=[l["name"] for l in lanes])
        for lane in lanes:
            self.emit("lane", name=lane["name"], brief=lane.get("brief", ""))
        return lanes

    def hunt(self, task: str, lanes: list[dict]) -> list[Finding]:
        self.emit("phase", phase="hunt", lanes=len(lanes))
        findings: list[Finding] = []
        lock = threading.Lock()

        def one_lane(index: int, lane: dict) -> None:
            agent_id = f"hunter-{index + 1}"
            self.emit("agent_started", agent=agent_id, role="hunter",
                      lane=lane["name"])
            files = "\n".join(f"  {f}" for f in lane.get("files", []))
            opening = (
                f"TASK: {task}\n\nYOUR LANE: {lane['name']}\n"
                f"{lane.get('brief', '')}\n\n"
                f"WORKSPACE ROOT: {self.workspace}\n"
                f"Files most worth your attention:\n{files}\n\n"
                f"Begin. Read before you conclude."
            )
            answer = self._agent(
                agent_id, HUNTER_SYSTEM.format(manual=TOOL_MANUAL),
                opening, Tier.WORKER,
            )
            raw = (answer or {}).get("findings", []) or []
            produced = []
            for item in raw:
                if not item.get("title") or not item.get("summary"):
                    continue
                produced.append(Finding(
                    id=uuid.uuid4().hex[:8],
                    lane=lane["name"],
                    title=str(item.get("title"))[:140],
                    file=str(item.get("file", "")),
                    line=int(item.get("line") or 0),
                    severity=str(item.get("severity", "medium")).lower(),
                    summary=str(item.get("summary"))[:500],
                    failure_scenario=str(item.get("failure_scenario", ""))[:800],
                    evidence=str(item.get("evidence", ""))[:1200],
                ))
            with lock:
                findings.extend(produced)
            for finding in produced:
                self.emit("finding_raised", id=finding.id, lane=finding.lane,
                          title=finding.title, file=finding.file,
                          line=finding.line, severity=finding.severity,
                          summary=finding.summary)
            self.emit("agent_finished", agent=agent_id, found=len(produced))

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(one_lane, i, lane) for i, lane in enumerate(lanes)]
            for future in as_completed(futures):
                future.result()
        return findings

    def verify(self, findings: list[Finding]) -> None:
        """Every finding, several independent attempts to destroy it."""
        self.emit("phase", phase="verify", findings=len(findings))
        jobs = [(f, n) for f in findings for n in range(VERIFIERS_PER_FINDING)]
        lock = threading.Lock()

        def one_verdict(finding: Finding, n: int) -> None:
            agent_id = f"verifier-{finding.id}-{n + 1}"
            self.emit("agent_started", agent=agent_id, role="verifier",
                      target=finding.id)
            opening = (
                f"WORKSPACE ROOT: {self.workspace}\n\n"
                f"CLAIMED DEFECT\n"
                f"  file: {finding.file}\n  line: {finding.line}\n"
                f"  title: {finding.title}\n  summary: {finding.summary}\n"
                f"  claimed failure: {finding.failure_scenario}\n"
                f"  quoted evidence: {finding.evidence}\n\n"
                f"Read the code and decide. Default to refuted."
            )
            answer = self._agent(
                agent_id, VERIFIER_SYSTEM.format(manual=TOOL_MANUAL),
                opening, Tier.VERIFIER, max_steps=10,
            )
            # A verifier that failed to answer counts as a refutation. The
            # alternative is letting an unverified finding through because a
            # process died, which is the wrong direction to fail in.
            verdict = {
                "refuted": True, "confidence": "low",
                "reasoning": "verifier did not return a verdict",
                "concrete_failure": "",
            } if answer is None else {
                "refuted": bool(answer.get("refuted", True)),
                "confidence": str(answer.get("confidence", "low")),
                "reasoning": str(answer.get("reasoning", ""))[:400],
                "concrete_failure": str(answer.get("concrete_failure", ""))[:600],
            }
            with lock:
                finding.verdicts.append(verdict)
            self.emit("verdict", finding=finding.id, agent=agent_id,
                      refuted=verdict["refuted"], confidence=verdict["confidence"],
                      reasoning=verdict["reasoning"])

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(one_verdict, f, n) for f, n in jobs]
            for future in as_completed(futures):
                future.result()

        for finding in findings:
            finding.survived = finding.survivals >= SURVIVAL_THRESHOLD
            self.ledger.append(
                "finding_judged", finding=finding.id, title=finding.title,
                survived=finding.survived,
                refuted_by=finding.refutations, survived_by=finding.survivals,
            )
            self.emit("finding_settled", id=finding.id, survived=finding.survived,
                      refuted_by=finding.refutations,
                      survived_by=finding.survivals)

    # ------------------------------------------------------------------ run

    def run(self, task: str) -> Report:
        import time

        started = time.time()
        run_id = uuid.uuid4().hex[:12]
        halted = ""
        self.ledger.append(
            "run_started", run_id=run_id, task=task,
            workspace=str(self.workspace), policy=self.policy.as_dict(),
            budget_ceiling_usd=self.budget.ceiling_usd,
        )
        self.emit("run_started", run_id=run_id, task=task,
                  policy=self.policy.as_dict(),
                  ceiling=self.budget.ceiling_usd)

        findings: list[Finding] = []
        try:
            lanes = self.plan(task)
            if lanes:
                findings = self.hunt(task, lanes)
                if findings:
                    self.verify(findings)
            else:
                halted = "the planner produced no lanes"
        except BudgetExceeded as exc:
            halted = str(exc)
            self.emit("halted", reason=halted)

        survived = [f for f in findings if f.survived]
        # Highest severity first, then the ones the verifiers agreed on most.
        rank = {"high": 0, "medium": 1, "low": 2}
        survived.sort(key=lambda f: (rank.get(f.severity, 1), -f.survivals))

        report = Report(
            run_id=run_id, task=task, lanes=[l["name"] for l in lanes] if lanes else [],
            raised=len(findings), survived=len(survived),
            findings=[asdict(f) for f in survived],
            spend_usd=round(self.budget.spent_usd, 5), calls=self.budget.calls,
            tool_calls=self.toolbox.calls, refusals=self.toolbox.refusals,
            ledger_head="", seconds=round(time.time() - started, 1),
            halted=halted,
        )
        # Closing entry first, then the head. A head published before the last
        # entry covers everything except the line that says how the run ended,
        # which is the one line someone checking the record would most want
        # covered.
        self.ledger.append(
            "run_finished", run_id=run_id, raised=report.raised,
            survived=report.survived, spend_usd=report.spend_usd, halted=halted,
        )
        report.ledger_head = self.ledger.head
        self.emit("run_finished", **asdict(report))
        return report
