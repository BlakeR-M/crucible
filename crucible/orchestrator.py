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
    settled: bool = False
    # How many other hunters independently raised the same thing. Reported
    # rather than hidden: two lanes arriving at one defect separately is
    # corroboration and worth a reader knowing.
    duplicates: int = 0

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


def _as_tool_call(parsed: dict, known: set[str]) -> tuple[str, dict] | None:
    """Recognise a tool call in whichever shape the model chose to write it.

    The documented protocol is {"tool": name, "args": {...}}. Models reliably
    write something else instead: the tool name as the only key, OpenAI's
    function-calling shape, or the arguments inline beside the name. Insisting
    on one spelling does not make the model comply, it makes the agent spend
    every step being told it is wrong and then get recorded as having failed.
    That is not a hypothetical: it refuted an entire run's findings, because a
    verifier that never answers counts against the finding it was judging.

    So the wire format is read generously and the record still shows exactly
    which tool ran with which arguments. Nothing is widened by this: an
    unrecognised name falls through to None, and the policy checks the call
    afterwards either way.
    """
    if not isinstance(parsed, dict):
        return None

    # {"tool": "read_file", "args": {...}} and its near neighbours.
    for name_key in ("tool", "name", "action", "function"):
        name = parsed.get(name_key)
        if isinstance(name, str) and name in known:
            for args_key in ("args", "arguments", "parameters", "input"):
                args = parsed.get(args_key)
                if isinstance(args, dict):
                    return name, args
            # Arguments sitting beside the name rather than nested under it.
            inline = {k: v for k, v in parsed.items()
                      if k not in ("tool", "name", "action", "function")}
            return name, inline

    # {"read_file": {"path": "..."}} — the tool name as the only key.
    keys = [k for k in parsed if k in known]
    if len(keys) == 1 and len(parsed) == 1:
        args = parsed[keys[0]]
        if isinstance(args, dict):
            return keys[0], args
        if isinstance(args, str):
            # {"read_file": "path/to/file.py"}
            return keys[0], {"path": args}
    return None


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
    # Balanced-brace scan, skipping anything inside a string literal. Without
    # the string tracking a brace in prose ("returns {} on failure") or in an
    # escaped quote throws the depth count off, and the scanner either cuts the
    # object short or never closes it. Findings quote code, so braces inside
    # strings are the normal case here rather than the exotic one.
    depth, start, in_string, escaped = 0, None, False, False
    for i, ch in enumerate(text):
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            if depth == 0:
                start = i
            depth += 1
        elif ch == "}":
            if depth > 0:
                depth -= 1
                if depth == 0 and start is not None:
                    try:
                        return json.loads(text[start:i + 1])
                    except json.JSONDecodeError:
                        start = None
    return None


def _as_int(value, default: int = 0) -> int:
    """A line number as the model chose to write it.

    Models return "42", 42, "42-45" and "line 42". int() raises on three of
    those, and an exception here kills an agent that had already done the work.
    """
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    match = re.search(r"\d+", str(value or ""))
    return int(match.group()) if match else default


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
        # Verifiers settle their own finding the moment its panel completes, so
        # two threads can reach the same finding at once. One decision each.
        self._settle_lock = threading.Lock()
        self.toolbox = Toolbox(self.workspace, policy, ledger)
        # Taken from the policy rather than hardcoded, so a tool added to the
        # policy is recognised on the wire without a second edit here.
        self._tool_names = set(policy.rules)

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
            # A reply that parses as a JSON array is valid JSON and not a
            # message this loop can act on. Treated as unparseable rather than
            # allowed to reach .get and raise AttributeError.
            if not isinstance(parsed, dict):
                parsed = None
            if parsed is None:
                transcript += (
                    "\n\nYour reply was not a single JSON object. Reply with one "
                    'JSON object and nothing else, for example '
                    '{"tool": "read_file", "args": {"path": "app.py"}}'
                )
                continue

            if parsed.get("done"):
                self.emit("agent_done", agent=agent_id, step=step)
                return parsed

            call = _as_tool_call(parsed, self._tool_names)
            if call is None:
                # Say what was wrong with the reply that was actually sent, and
                # show the shape again. A bare "do it properly" produces the
                # same reply on the next turn and the turn after that.
                transcript += (
                    f"\n\nThat was not a tool call I could read, and no tool ran. "
                    f"Available tools: {', '.join(sorted(self._tool_names))}. "
                    f'Use exactly this shape: '
                    f'{{"tool": "read_file", "args": {{"path": "app.py"}}}} '
                    f'or finish with {{"done": true, ...}}.'
                )
                continue

            tool, args = call
            result = tools.invoke(tool, args)
            self.emit(
                "tool", agent=agent_id, tool=tool,
                args=self.toolbox._safe_args(args),
                refused=result.refused, reason=result.reason,
                bytes=len(result.content),
            )
            transcript += (
                f"\n\n>>> you called {tool}({json.dumps(args)})"
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
        # Only well-formed lanes. A planner that returns a list of bare strings
        # would otherwise crash the run at the first .get on a str.
        lanes = [
            l for l in (parsed.get("lanes") or [])
            if isinstance(l, dict) and l.get("name")
        ][:6]
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
                # A model that returns a list of strings, or a finding with no
                # summary, is a model that did not follow the shape. Skip it
                # rather than let it raise out of the worker, because an
                # exception here takes the whole lane's work with it.
                if not isinstance(item, dict):
                    continue
                if not item.get("title") or not item.get("summary"):
                    continue
                produced.append(Finding(
                    id=uuid.uuid4().hex[:8],
                    lane=lane["name"],
                    title=str(item.get("title"))[:140],
                    file=str(item.get("file", "")),
                    line=_as_int(item.get("line")),
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
                # One worker throwing must not discard what the others found.
                # Left unhandled, result() re-raises here and the entire phase
                # is lost along with every finding already in hand, which is
                # the most expensive possible way to fail.
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    self.emit("worker_failed", reason=f"{type(exc).__name__}: {exc}"[:200])
                    self.ledger.append("worker_failed",
                                       reason=f"{type(exc).__name__}: {exc}"[:300])
        return findings

    def _source_around(self, finding: Finding, span: int = 45) -> str:
        """The code the claim is about, handed over rather than hunted for.

        A verifier that has to locate the file first spends its steps on
        navigation, and a run's worth of them spending steps that way is a run
        where nothing gets judged on its merits. The window is generous and the
        tools stay available, so a verdict that needs wider reading can still
        go and get it.
        """
        try:
            path = Path(finding.file)
            if not path.is_absolute():
                path = self.workspace / path
            path = path.resolve()
            if self.workspace not in path.parents and path != self.workspace:
                return "(the claimed file sits outside the workspace)"
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return f"(could not open {finding.file}; use the tools to locate it)"

        centre = max(1, finding.line or 1)
        start = max(1, centre - span // 2)
        end = min(len(lines), start + span)
        width = len(str(end))
        body = "\n".join(
            f"{i:>{width}}  {lines[i - 1]}" for i in range(start, end + 1)
        )
        return (f"SOURCE, {path.name} lines {start} to {end} of {len(lines)}:\n"
                f"{body}\n")

    def dedupe(self, findings: list[Finding]) -> list[Finding]:
        """Collapse the same defect found by more than one hunter.

        Hunters work blind to each other, which is what stops them converging
        on one easy finding and reporting nothing else. The cost of that is
        real duplicates: two lanes reading the same file both notice the same
        floor division, and without this the report carries it twice and three
        extra verifiers are paid to judge a claim already being judged.

        Same file and within three lines is the test. Deliberately tight,
        because two genuinely different defects can sit close together and
        merging those would hide one.
        """
        kept: list[Finding] = []
        for finding in findings:
            twin = next(
                (k for k in kept
                 if Path(k.file).name.lower() == Path(finding.file).name.lower()
                 and abs(k.line - finding.line) <= 3),
                None,
            )
            if twin is None:
                kept.append(finding)
                continue
            # Keep the fuller account of the two, since one hunter usually
            # explains the failure better than the other.
            if len(finding.failure_scenario) > len(twin.failure_scenario):
                twin.failure_scenario = finding.failure_scenario
            if len(finding.evidence) > len(twin.evidence):
                twin.evidence = finding.evidence
            twin.duplicates += 1
            self.ledger.append("finding_merged", into=twin.id, dropped=finding.id,
                               file=finding.file, line=finding.line)
            self.emit("finding_merged", into=twin.id, dropped=finding.id,
                      title=finding.title)
        if len(kept) != len(findings):
            self.emit("deduped", before=len(findings), after=len(kept))
        return kept

    def _settle(self, finding: Finding) -> None:
        """Decide one finding, once. Safe to call from any verifier thread."""
        with self._settle_lock:
            if finding.settled:
                return
            finding.settled = True
            finding.survived = finding.survivals >= SURVIVAL_THRESHOLD
        self.ledger.append(
            "finding_judged", finding=finding.id, title=finding.title,
            survived=finding.survived,
            refuted_by=finding.refutations, survived_by=finding.survivals,
        )
        self.emit("finding_settled", id=finding.id, survived=finding.survived,
                  refuted_by=finding.refutations, survived_by=finding.survivals)

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
                f"{self._source_around(finding)}\n"
                f"Check the claim against the code. Widen your reading with the "
                f"tools if the answer depends on something outside this window, "
                f"such as a caller or a validator upstream. Default to refuted."
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
                complete = len(finding.verdicts) >= VERIFIERS_PER_FINDING
            self.emit("verdict", finding=finding.id, agent=agent_id,
                      refuted=verdict["refuted"], confidence=verdict["confidence"],
                      reasoning=verdict["reasoning"])
            # Settle the moment this finding's own panel is complete, rather
            # than waiting for every other finding's verifiers to finish too.
            # Otherwise a run holds a dozen specimens open for the whole phase
            # and resolves them all in one silent instant at the end, which is
            # both a worse thing to watch and a worse account of what happened:
            # each finding was in fact decided at a particular moment.
            if complete:
                self._settle(finding)

        with ThreadPoolExecutor(max_workers=self.max_workers) as pool:
            futures = [pool.submit(one_verdict, f, n) for f, n in jobs]
            for future in as_completed(futures):
                # One worker throwing must not discard what the others found.
                # Left unhandled, result() re-raises here and the entire phase
                # is lost along with every finding already in hand, which is
                # the most expensive possible way to fail.
                try:
                    future.result()
                except Exception as exc:  # noqa: BLE001
                    self.emit("worker_failed", reason=f"{type(exc).__name__}: {exc}"[:200])
                    self.ledger.append("worker_failed",
                                       reason=f"{type(exc).__name__}: {exc}"[:300])

        # Anything the pool failed to complete a panel for still has to be
        # decided, and decided against, since an unjudged finding must never
        # be reported as one that survived.
        for finding in findings:
            if not finding.settled:
                self._settle(finding)

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
        # Bound before the try, because the report below reads it. Assigned
        # only inside, a planner that raises on the budget leaves it unbound
        # and the run dies with UnboundLocalError while building the very
        # report that was meant to explain the failure.
        lanes: list[dict] = []
        try:
            lanes = self.plan(task)
            if lanes:
                findings = self.dedupe(self.hunt(task, lanes))
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
            run_id=run_id, task=task, lanes=[l["name"] for l in lanes],
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
