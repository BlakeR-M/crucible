"""The visitor's way in: one agent, talked to in plain language.

Everything else in this project is watched rather than used. A run streams past
and the visitor reads it. This is the surface where someone arrives with a
question instead of a task, so the agent has to be able to answer as well as
act, and the two capabilities need different guarantees.

Acting is the easy half and it is governed the same way agents are governed
everywhere else here: a declared tool surface, checked before any handler runs,
deny by default, a refusal carrying a sentence rather than an exception. The
declaration lives in one object that can be printed. An unlisted tool is refused
with its reason, the refusal is recorded, and the text goes back to the model as
something it can reason about and route around. That is the same contract
`tools.py` gives the hunters, for the same reason: the one call site that forgot
to check is the one an agent will find, so there is one call site.

Answering is the half that can quietly destroy the whole demonstration. A model
asked about a run it cannot see will describe one anyway, fluently, with
plausible numbers. Every claim this agent makes about a run therefore has to
arrive through a tool result inside this conversation, and when the data is not
there the honest answer is that it is not there. A demonstration that fabricates
is worse than no demonstration, because it discredits the parts that were real.
Three things enforce that beyond asking the model nicely: the only path from run
state into an answer is tool text, the tools report absence explicitly rather
than returning something empty and shrug-shaped, and the run this session can
see is the run this session started.

Two smaller decisions worth stating. Conversation lives in this object and is
bounded in both directions, turns and dollars, because an unbounded session on a
public page is a bill and a context overflow waiting for the same visitor to
find both. And the reply is delivered as a stream of the same event dicts the
orchestrator emits, so the server pushes chat down the channel it already has
rather than growing a second one.
"""

from __future__ import annotations

import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from .ledger import Ledger
from .orchestrator import _extract_json
from .policy import Decision
from .providers import Budget, BudgetExceeded, Tier
from .tools import ToolResult, _clip

# Bounds on one visitor session. Turns are pairs, so twelve is a long
# conversation and still a transcript that fits comfortably in a prompt.
MAX_TURNS = 12
MAX_VISITOR_CHARS = 2000
MAX_STORED_ANSWER_CHARS = 1400
MAX_STORED_TOOL_CHARS = 1400
# How many tool calls one turn may make before it has to answer with what it
# has. Four is generous for questions of the shape "why was finding 7 thrown
# out", which needs a listing and then a detail.
MAX_TOOL_STEPS = 4
# The chat seat is volume work and it is not the seat where being wrong is
# expensive, so it runs on the cheap tier. The money in this project belongs to
# the verifiers.
CHAT_TIER = Tier.WORKER
CHAT_MAX_OUTPUT = 900

# Keys a model uses when it means "here is the answer" rather than "run this".
# Checked before any tool name is looked for, so {"answer": "..."} is never read
# as a call to a tool called answer.
ANSWER_KEYS = ("answer", "say", "reply", "response", "message", "text")
NAME_KEYS = ("tool", "name", "action", "function")
ARG_KEYS = ("args", "arguments", "parameters", "input")

# The task keys the arena ships with. Held here as a fallback rather than
# imported from the server, so this module stays importable on its own and the
# server can import it without a cycle. A run source that knows better is asked
# first.
DEFAULT_TASK_KEYS = ("full", "money", "concurrency", "auth")


# ------------------------------------------------------------- tool surface

@dataclass(frozen=True)
class ChatTool:
    """What one conversational tool is, declared rather than discovered.

    summary    the line the model is shown in its manual.
    arguments  argument name to a description, also shown to the model.
    required   arguments that have to be present.
    choices    argument name to the only values permitted for it.
    integers   arguments that have to be whole numbers.
    """

    summary: str
    arguments: dict[str, str] = field(default_factory=dict)
    required: tuple[str, ...] = ()
    choices: dict[str, tuple[str, ...]] = field(default_factory=dict)
    integers: tuple[str, ...] = ()

    @property
    def sole_argument(self) -> str:
        """The one argument, when there is exactly one. Lets a model that
        writes {"explain": "hardware"} be understood rather than corrected."""
        return next(iter(self.arguments)) if len(self.arguments) == 1 else ""


class ChatSurface:
    """A named set of conversational tools, and the check every call passes.

    The same deny-by-default shape as `policy.Policy`, and it returns the same
    `Decision`, so a refusal here reads identically to a refusal at the file
    boundary and the two can be shown side by side. Every level denies: an
    unlisted tool, an argument that was not declared, a missing required
    argument, a value outside the permitted set, and anything at all that raises
    while the check is being made.
    """

    def __init__(self, name: str, tools: dict[str, ChatTool], *,
                 description: str = ""):
        self.name = name
        self.tools = tools
        self.description = description

    def __contains__(self, tool: str) -> bool:
        return tool in self.tools

    def check(self, tool: str, args: dict) -> Decision:
        """Deny by default, and deny on any error while deciding."""
        try:
            rule = self.tools.get(tool)
            if rule is None:
                available = ", ".join(sorted(self.tools))
                return Decision(
                    False,
                    f"tool '{tool}' is not on the '{self.name}' surface. "
                    f"Available: {available}",
                    tool,
                )
            args = args or {}
            if not isinstance(args, dict):
                return Decision(
                    False, f"arguments to '{tool}' arrived as "
                           f"{type(args).__name__} rather than an object", tool)
            for name in args:
                if name not in rule.arguments:
                    declared = ", ".join(rule.arguments) or "none"
                    return Decision(
                        False,
                        f"'{tool}' takes no argument called '{name}' "
                        f"(it takes: {declared})",
                        tool,
                    )
            for name in rule.required:
                if name not in args or args[name] in (None, ""):
                    return Decision(
                        False, f"'{tool}' needs the '{name}' argument", tool)
            for name, permitted in rule.choices.items():
                if name in args and str(args[name]).strip().lower() not in permitted:
                    return Decision(
                        False,
                        f"'{args[name]}' is not a value '{tool}' accepts for "
                        f"'{name}' (permitted: {', '.join(permitted)})",
                        tool,
                    )
            for name in rule.integers:
                if name in args and not _looks_whole(args[name]):
                    return Decision(
                        False,
                        f"'{name}' on '{tool}' has to be a whole number, and "
                        f"'{args[name]}' is not one",
                        tool,
                    )
            return Decision(True, "permitted by the chat surface", tool)
        except Exception as exc:  # noqa: BLE001 - a failed check is a refusal
            return Decision(False, f"refused while checking '{tool}': {exc}", tool)

    def manual(self) -> str:
        """The tool list as the model is shown it.

        Generated from the declarations rather than written beside them, so a
        tool cannot be added without the model being told it exists, and a tool
        cannot be removed and still be advertised.
        """
        lines = []
        for name in sorted(self.tools):
            rule = self.tools[name]
            signature = f"{name}({', '.join(rule.arguments)})"
            lines.append(f"{signature:<28} {rule.summary}")
            for arg, description in rule.arguments.items():
                permitted = rule.choices.get(arg)
                suffix = f" One of: {', '.join(permitted)}." if permitted else ""
                lines.append(f"    {arg}: {description}{suffix}")
        return "\n".join(lines)

    def as_dict(self) -> dict:
        """The surface as data, for a run record or a page that shows it."""
        return {
            "name": self.name,
            "description": self.description,
            "tools": {
                name: {
                    "summary": rule.summary,
                    "arguments": rule.arguments,
                    "required": list(rule.required),
                    "choices": {k: list(v) for k, v in rule.choices.items()},
                }
                for name, rule in sorted(self.tools.items())
            },
        }


def _looks_whole(value) -> bool:
    if isinstance(value, bool):
        return False
    if isinstance(value, int):
        return True
    if isinstance(value, float):
        return float(value).is_integer()
    return bool(re.fullmatch(r"\s*\d+\s*", str(value)))


EXPLAINER_TOPICS = ("verification", "refutation", "numbers", "hardware", "limits")


def visitor_surface(task_keys=DEFAULT_TASK_KEYS) -> ChatSurface:
    """The authority a visitor's agent gets.

    It can start one review against the fixed demo target, read the state of
    the run this session started, and explain the machinery. It cannot choose a
    workspace, cannot reach a run somebody else started, and cannot touch a
    file, because none of those are on the list and the list is the authority.
    """
    return ChatSurface(
        "visitor",
        {
            "start_review": ChatTool(
                "Launch a review run against the demo target.",
                {"task_key": "which review to run"},
                required=("task_key",),
                choices={"task_key": tuple(task_keys)},
            ),
            "run_status": ChatTool(
                "Phase, counts and spend of this session's run.",
            ),
            "list_findings": ChatTool(
                "List the run's findings.",
                {"which": "which pile to list"},
                required=("which",),
                choices={"which": ("all", "standing", "refuted")},
            ),
            "finding_detail": ChatTool(
                "One finding in full, with every verifier's verdict and reasoning.",
                {"n": "the number shown beside the finding in list_findings"},
                required=("n",),
                integers=("n",),
            ),
            "policy_summary": ChatTool(
                "What the reviewing agents are and are not allowed to do.",
            ),
            "ledger_summary": ChatTool(
                "Entry count, root hash, and whether the run's hash chain verifies.",
            ),
            "missed_defects": ChatTool(
                "Planted defects in the demo target that the run did not find.",
            ),
            "explain": ChatTool(
                "A standing explanation of how part of the arena works.",
                {"topic": "what to explain"},
                required=("topic",),
                choices={"topic": EXPLAINER_TOPICS},
            ),
        },
        description=(
            "Start one review against the fixed demo target, read this "
            "session's run, and explain the machinery. No file access, no "
            "other visitor's run, no choice of workspace."
        ),
    )


# ---------------------------------------------------------------- run state

class RunView:
    """One run, read back off the events that were published while it ran.

    The event stream is the only account that carries everything. The closing
    report holds survivors alone, which is exactly the wrong half for a
    conversation whose most valuable question is "why was that one thrown out",
    and the ledger records that a finding was judged without recording the
    reasoning that judged it. So the buffer is replayed here and both piles are
    rebuilt from it, with the closing report merged over the top where it has
    more detail.

    Nothing is inferred. A field the stream never carried stays empty, and the
    tools above say so rather than filling the gap.
    """

    def __init__(self, run_id: str, events, *, ledger: Ledger | None = None):
        self.run_id = run_id
        self.ledger = ledger
        self.task = ""
        self.phase = "waiting"
        self.lanes: list[str] = []
        self.policy: dict = {}
        self.halted = ""
        self.failed = ""
        self.finished = False
        self.seconds = 0.0
        self.ledger_head = ""
        self.calls = 0
        self.tool_calls = 0
        self.refusals = 0
        self.spend_usd = 0.0
        # Whether the spend figure came from the closing report or was added up
        # from the stream. Reported, because the stream carries no cost line for
        # the planner's call and a live figure is therefore slightly low.
        self.spend_is_exact = False
        self._findings: dict[str, dict] = {}
        for event in events or []:
            self._absorb(event)
        for number, finding in enumerate(self._findings.values(), 1):
            finding["n"] = number

    # ------------------------------------------------------------ assembly

    def _absorb(self, event: dict) -> None:
        if not isinstance(event, dict):
            return
        kind = event.get("kind")
        if kind == "run_started":
            self.task = str(event.get("task", ""))
            self.policy = event.get("policy") or {}
        elif kind == "phase":
            self.phase = str(event.get("phase", self.phase))
        elif kind == "lane":
            self.lanes.append(str(event.get("name", "")))
        elif kind == "agent_thought":
            self.calls += 1
            try:
                self.spend_usd += float(event.get("cost") or 0.0)
            except (TypeError, ValueError):
                pass
        elif kind == "tool":
            self.tool_calls += 1
            if event.get("refused"):
                self.refusals += 1
        elif kind == "finding_raised":
            finding_id = str(event.get("id", ""))
            if finding_id:
                self._findings[finding_id] = {
                    "id": finding_id,
                    "n": 0,
                    "lane": str(event.get("lane", "")),
                    "title": str(event.get("title", "")),
                    "file": str(event.get("file", "")),
                    "line": event.get("line", 0),
                    "severity": str(event.get("severity", "")),
                    "summary": str(event.get("summary", "")),
                    "failure_scenario": str(event.get("failure_scenario", "")),
                    "evidence": "",
                    "verdicts": [],
                    "settled": False,
                    "survived": False,
                    "duplicates": 0,
                }
        elif kind == "finding_merged":
            # A duplicate collapsed into another finding. It was never judged,
            # so it is not a pile of its own; the corroboration is recorded on
            # the survivor instead.
            dropped = self._findings.pop(str(event.get("dropped", "")), None)
            into = self._findings.get(str(event.get("into", "")))
            if into is not None and dropped is not None:
                into["duplicates"] += 1
        elif kind == "verdict":
            finding = self._findings.get(str(event.get("finding", "")))
            if finding is not None:
                finding["verdicts"].append({
                    "agent": str(event.get("agent", "")),
                    "refuted": bool(event.get("refuted", True)),
                    "confidence": str(event.get("confidence", "")),
                    "reasoning": str(event.get("reasoning", "")),
                    "concrete_failure": str(event.get("concrete_failure", "")),
                })
        elif kind == "finding_settled":
            finding = self._findings.get(str(event.get("id", "")))
            if finding is not None:
                finding["settled"] = True
                finding["survived"] = bool(event.get("survived"))
        elif kind == "run_failed":
            self.failed = str(event.get("reason", ""))
        elif kind == "run_finished":
            self._absorb_report(event)

    def _absorb_report(self, report: dict) -> None:
        self.finished = True
        self.phase = "finished"
        self.task = str(report.get("task", "") or self.task)
        self.lanes = [str(x) for x in (report.get("lanes") or self.lanes)]
        self.halted = str(report.get("halted", ""))
        self.ledger_head = str(report.get("ledger_head", ""))
        for attribute, key in (("calls", "calls"), ("tool_calls", "tool_calls"),
                               ("refusals", "refusals")):
            value = report.get(key)
            if isinstance(value, int):
                setattr(self, attribute, value)
        try:
            self.spend_usd = float(report.get("spend_usd") or 0.0)
            self.spend_is_exact = True
        except (TypeError, ValueError):
            pass
        try:
            self.seconds = float(report.get("seconds") or 0.0)
        except (TypeError, ValueError):
            pass
        # The report carries the fuller text for the survivors: the evidence it
        # quoted and each verdict's concrete failure. Merged over what the
        # stream already built rather than replacing it, because the refuted
        # pile exists only in the stream.
        for item in report.get("findings") or []:
            if not isinstance(item, dict):
                continue
            finding = self._findings.get(str(item.get("id", "")))
            if finding is None:
                continue
            for key in ("failure_scenario", "evidence", "summary"):
                if item.get(key) and not finding.get(key):
                    finding[key] = str(item[key])
            # Merged field by field and matched by position, rather than the
            # report's verdict list replacing the stream's. The two carry the
            # same panel in the same order, but not the same columns: the
            # stream names the verifier that spoke and the report keeps the
            # concrete failure it wrote. Replacing wholesale would drop
            # whichever half the report happens not to hold.
            verdicts = [v for v in (item.get("verdicts") or []) if isinstance(v, dict)]
            for index, raw in enumerate(verdicts):
                if index < len(finding["verdicts"]):
                    target = finding["verdicts"][index]
                    for key in ("agent", "confidence", "reasoning",
                                "concrete_failure"):
                        if raw.get(key) and not target.get(key):
                            target[key] = str(raw[key])
                    continue
                finding["verdicts"].append({
                    "agent": str(raw.get("agent", "")),
                    "refuted": bool(raw.get("refuted", True)),
                    "confidence": str(raw.get("confidence", "")),
                    "reasoning": str(raw.get("reasoning", "")),
                    "concrete_failure": str(raw.get("concrete_failure", "")),
                })

    # -------------------------------------------------------------- access

    @property
    def findings(self) -> list[dict]:
        return list(self._findings.values())

    @property
    def standing(self) -> list[dict]:
        return [f for f in self._findings.values() if f["survived"]]

    @property
    def refuted(self) -> list[dict]:
        return [f for f in self._findings.values()
                if f["settled"] and not f["survived"]]

    @property
    def raised(self) -> int:
        return len(self._findings)

    def finding(self, number: int) -> dict | None:
        return next((f for f in self._findings.values() if f["n"] == number), None)


class ServerRuns:
    """Run state as the web server holds it.

    The server is imported inside the methods rather than at the top of the
    module, so importing chat does not start the server's module-level state and
    the server is free to import chat. Everything this class needs is behind the
    registry's own lock except the event list, and that is copied before it is
    read.

    Any object with these four methods will do, which is how the tests supply a
    stub with no server, no network and no key.
    """

    def __init__(self, registry=None, *, runs_dir: Path | None = None,
                 target: Path | None = None, session_id: str = ""):
        self._registry = registry
        self._runs_dir = runs_dir
        self._target = target
        # Which visitor is asking. A run the agent starts belongs to the
        # session, so it runs on that session's key when one is attached.
        self._session_id = session_id

    def _server(self):
        from . import server  # noqa: PLC0415 - deliberate, see the docstring
        return server

    def tasks(self) -> dict[str, str]:
        return dict(self._server().TASKS)

    def start(self, task_key: str) -> tuple[str, str]:
        return self._server().start_run(task_key, sid=self._session_id)

    def view(self, run_id: str) -> RunView | None:
        registry = self._registry or self._server().REGISTRY
        state = registry.get(run_id)
        if state is None:
            return None
        # A copy, because a live run is appending to this list from another
        # thread while it is read.
        return RunView(run_id, list(state["events"]), ledger=self.ledger(run_id))

    def ledger(self, run_id: str) -> Ledger | None:
        root = (self._runs_dir or self._server().RUNS).resolve()
        path = (root / f"{run_id}.jsonl").resolve()
        # Containment on the resolved path, the same rule the agents live
        # under, because the run id reaches here from a language model.
        if root not in path.parents or not path.is_file():
            return None
        return Ledger(path)

    def policy(self) -> dict:
        from .policy import review_policy  # noqa: PLC0415
        target = self._target or self._server().TARGET
        return review_policy(target).as_dict()

    def answer_key(self) -> list[dict]:
        target = self._target or self._server().TARGET
        path = Path(target) / ".answer_key.json"
        if not path.is_file():
            return []
        import json  # noqa: PLC0415

        try:
            return json.loads(path.read_text(encoding="utf-8")).get("defects", [])
        except (OSError, ValueError):
            return []


# ------------------------------------------------------------- explainers

EXPLAINERS: dict[str, str] = {
    "verification": (
        "A run has three phases. A planner divides the codebase into four to "
        "six lanes that do not overlap. Hunters take one lane each, read the "
        "code with real tools, and raise findings; they are blind to each "
        "other, which is what stops four of them reporting the same easy bug "
        "and nothing else. Then every finding goes to three verifiers whose "
        "instruction is to refute it, not to review it. Each verifier is blind "
        "to the other two, each is told that uncertainty counts as a "
        "refutation, and a finding needs two of the three to fail to refute it "
        "before it is reported. A verifier that crashes or never answers counts "
        "against the finding it was judging, because the wrong way to fail is "
        "the one where an unverified claim gets published because a process "
        "died."
    ),
    "refutation": (
        "Generating findings is close to worthless. Point any model at a "
        "codebase and it will find bugs, and a good share of them will be "
        "confident, well written and wrong. Reading those costs more time than "
        "the review saves. So the weight of this design sits on the second "
        "half: the arena throws away most of what it produces, on purpose, and "
        "shows you the count. A finding is thrown out when a verifier shows the "
        "described behaviour does not occur, or the path is unreachable, or "
        "something upstream already prevents it, or it is a style preference, "
        "or nobody could construct concrete inputs that produce the wrong "
        "outcome. The kill count is the headline number, not the find count."
    ),
    "numbers": (
        "Survived over raised is the one that matters: how many findings were "
        "reported against how many were made. A run that raises forty and "
        "reports nine is working correctly. Spend is real US dollars against a "
        "per-run ceiling that is held before each call rather than totalled "
        "afterwards, so the ceiling holds against work already in flight. Calls "
        "are model calls, reads are tool calls the agents made against the "
        "code, and refusals are tool calls the policy blocked. The ledger head "
        "is the SHA-256 tip of the run's hash chain: quote it and anyone can "
        "check later that the file they were handed is the file that was "
        "written."
    ),
    "hardware": (
        "The arena itself is a standard-library Python process with no "
        "dependencies, so it runs on anything Python runs on; the hosted demo "
        "sits on Railway. The thinking is not local. The planner and the "
        "verifiers call gpt-5 and the hunters call gpt-5-mini over the OpenAI "
        "API, which is the seat split that keeps a run around twenty cents. "
        "The sovereign question is costed properly in docs/SOVEREIGN.md against "
        "a 16 GB RTX 5080: one card of that class reaches roughly gpt-5-mini "
        "capability for worker seats and most of the way for planning, and it "
        "does not reach gpt-5 class. Matching gpt-5 on this workload inside a "
        "boundary starts around USD 30-45k of hardware, and around USD "
        "370-450k for an eight-GPU node with headroom. One design note survives "
        "the move: the verifier should come from a different model family than "
        "the hunter, because a critic drawn from the same training "
        "distribution misses the errors its sibling makes."
    ),
    "limits": (
        "Worth knowing before you judge the output. The demo target is "
        "synthetic, so recall on it says little about recall on a real "
        "codebase with history. Runs vary: three consecutive runs against the "
        "same nine planted defects found seven, seven and six, and not the same "
        "seven. Three verifiers is a small panel, chosen so a run finishes "
        "while someone is watching; cutting false positives further wants more "
        "of them or a verifier from another model family. The ledger is "
        "tamper-evident rather than tamper-proof, and a chain truncated at the "
        "end still verifies, which is why the head hash is published with the "
        "report. And findings are not fixes: writing a correct fix for a hard "
        "defect is materially harder than recognising one."
    ),
}


CHAT_SYSTEM = """\
You are the agent that runs Crucible, an arena where AI agents review code and
every finding they raise is then attacked by three independent verifiers whose
instruction is to destroy it. Only findings that survive a majority are
reported. You are talking to one visitor on the website.

HOW YOU TALK
Direct, technical, Australian. Short sentences and complete ones. No corporate
filler, no "great question", no "I would be happy to", no bullet-point sales
copy. Do not compliment the visitor and do not apologise. When the arena got
something wrong, missed a defect or threw away a finding it should have kept,
say so plainly and say what it cost. That honesty is the product: this system
argues that software should show what it discarded, and that applies to you.

WHAT YOU MAY SAY
Only what a tool has told you in this conversation. Every number, finding,
verdict, dollar figure and hash comes from a tool result and from nowhere else.
You have no memory of other runs, no access to the visitor's own code, and no
way to guess. If you do not have the data, call the tool that would get it, or
say plainly that you cannot answer that. Never estimate a number, never
reconstruct a verdict you have not read, and never describe a finding that has
not come back from a tool. An answer you invented is worse than no answer, and
it is the single failure that would discredit everything else here.

A tool that refuses you comes back with a reason. Read it, tell the visitor what
happened if it matters to them, and take another route.

YOUR TOOLS
{manual}

PROTOCOL
Every reply is EXACTLY ONE JSON object and nothing else.

To use a tool:
{{"tool": "list_findings", "args": {{"which": "standing"}}}}

To answer the visitor:
{{"answer": "your reply, in plain prose"}}

Call a tool whenever the answer depends on the run, the policy, the ledger or
the answer key. Answer directly when it does not. Two or three sentences is
usually right; go longer only when the visitor asked for the detail.

HOW TO TALK
Tool names are yours, not the visitor's. They never appear in an answer. Asked
what you can do, describe it the way a person would: you can start a review,
say how one is going, explain why a particular finding was thrown out, show
what the agents are and are not allowed to touch, or admit which planted
defects were walked straight past. Never write start_review, run_status,
list_findings or any other name from your manual into a reply, and never tell
a visitor to "ask for" one of them. If you want them to be able to follow up,
offer it in plain words: "I can tell you why the third one died."

Never instruct a visitor to wait and ask again. A review takes a couple of
minutes and they can watch it happening beside you. Say what stage it is at
and what you will be able to tell them once it lands.

Refusals need framing or they read as breakage. Most of them come from one
agent whose entire job is testing the boundary, so a run with a dozen refusals
is a run where the limits held a dozen times. Say that, rather than reporting
a bare count that sounds like something went wrong.
"""

OPENING = (
    "This is Crucible. Agents review a codebase, then every finding they raise "
    "gets attacked by three verifiers whose job is to kill it, and only what "
    "survives is reported. Say the word and I will start a run against the demo "
    "target, money handling or access control or the lot. If a run has already "
    "gone through, ask me why a particular finding was thrown out and I will "
    "quote the verifier that killed it, or ask which planted defects we walked "
    "straight past and I will tell you that too."
)


# ---------------------------------------------------------------- the agent

class ChatAgent:
    """One visitor, one conversation, one run they can see.

    Constructed per session. The instance holds the transcript and the spend
    ceiling for that session, so a visitor cannot spend another visitor's
    allowance and cannot read another visitor's run: the only run this object
    will talk about is one it started itself.
    """

    def __init__(self, provider, runs, budget: Budget, *, emit=None,
                 ledger: Ledger | None = None, max_turns: int = MAX_TURNS,
                 session_id: str = "", surface: ChatSurface | None = None):
        self.provider = provider
        self.runs = runs
        self.budget = budget
        self.max_turns = max(1, int(max_turns))
        self.session_id = session_id or f"s{int(time.time() * 1000) % 1_000_000:06d}"
        self._emit = emit or (lambda event: None)
        self.ledger = ledger
        self.surface = surface or visitor_surface(self._task_keys())
        self.history: list[dict] = []
        self.run_ids: list[str] = []
        # Every tool call and every refusal this session made, in order. The
        # refusal has to be recorded somewhere the operator can read even when
        # no ledger was handed in, since "it was refused and nobody knows" is
        # not a record.
        self.record: list[dict] = []
        # A visitor with two browser tabs, or one impatient enough to submit
        # twice, gets two threads into the same session. Turns are serialised
        # and the second is told to wait rather than allowed to interleave a
        # second conversation into the first one's transcript.
        self._turn_lock = threading.Lock()

    def _task_keys(self) -> tuple[str, ...]:
        """Ask the run source what reviews exist, and fall back to the shipped
        list if it cannot say. A surface built from a guess would refuse a real
        task or admit one that does not exist."""
        try:
            keys = tuple(self.runs.tasks())
        except Exception:  # noqa: BLE001 - a source that cannot say is not fatal
            keys = ()
        return keys or DEFAULT_TASK_KEYS

    # ------------------------------------------------------------ the turn

    def opening(self) -> str:
        """The first thing the visitor reads. A specific offer, not an
        invitation to think of something."""
        return OPENING

    def send(self, message: str) -> str:
        """One turn, answered. The whole reply as a string."""
        answer = ""
        for event in self.stream(message):
            if event.get("kind") == "chat_answer":
                answer = event.get("text", "")
        return answer

    def stream(self, message: str):
        """One turn, as a stream of events.

        The events are the same dicts the orchestrator emits, with a "kind"
        key, so the server pushes them down the channel it already has.

        Being straight about what streams here: the provider interface returns a
        whole completion, so the text deltas are this method chunking a finished
        answer rather than tokens arriving from the model. What is genuinely
        incremental is the sequence around them. A turn that reads the findings
        and then opens one emits its tool calls, its refusals and its results as
        they happen, which is the part a watcher actually learns anything from.
        """
        if not self._turn_lock.acquire(blocking=False):
            yield from self._single(
                "chat_answer",
                text="One message at a time. I am still working on the last one.",
                refused=True,
            )
            return
        try:
            yield from self._turn(message)
        finally:
            self._turn_lock.release()

    def _single(self, kind: str, **payload):
        event = {"kind": kind, "session": self.session_id, **payload}
        self._emit(event)
        yield event

    def _turn(self, message: str):
        message = _clip(str(message or "").strip(), MAX_VISITOR_CHARS)
        if not message:
            yield from self._single(
                "chat_answer", text="Ask me something and I will answer it.")
            return

        yield from self._single("chat_started", text=message)
        transcript = self._transcript(message)
        notes: list[str] = []
        answer = ""

        for step in range(MAX_TOOL_STEPS):
            if self.budget.remaining_usd <= 0:
                answer = self._spent_message()
                break
            try:
                reply = self.provider.complete(
                    self.system_prompt(), transcript, CHAT_TIER, self.budget,
                    max_output=CHAT_MAX_OUTPUT,
                )
            except BudgetExceeded:
                # The ceiling is a refusal, not a crash. The visitor gets a
                # sentence saying the session is done rather than a stack trace
                # and a blank panel.
                answer = self._spent_message()
                break
            except Exception as exc:  # noqa: BLE001 - reported, never leaked
                # Whatever went wrong upstream, the visitor gets the shape of it
                # and never the key, the URL or the body of the request.
                answer = (f"The model call failed: {type(exc).__name__}. "
                          f"Nothing was changed. Try that again.")
                yield from self._single("chat_error", reason=type(exc).__name__)
                break

            yield from self._single(
                "chat_thought", step=step, model=reply.model,
                cost=round(reply.cost_usd, 5), seconds=round(reply.seconds, 1))

            intent = self._read(reply.text)
            if intent[0] == "say":
                answer = intent[1]
                break

            tool, args = intent[1], intent[2]
            result = self.use_tool(tool, args)
            yield from self._single(
                "chat_tool", tool=tool, args=args, refused=result.refused,
                ok=result.ok, reason=result.reason,
                bytes=len(result.content))
            if result.refused:
                yield from self._single(
                    "chat_refusal", tool=tool, reason=result.reason)
            note = f"you called {tool}({args})\n{result.as_text()}"
            notes.append(_clip(note, MAX_STORED_TOOL_CHARS))
            transcript += f"\n\n>>> {note}"
        else:
            # Out of steps with no answer. Say that, rather than inventing a
            # summary of tool output the model never got to read.
            answer = ("I went round the tools a few times without landing on an "
                      "answer. Ask me again more narrowly and I will get there.")

        answer = answer.strip() or (
            "I do not have an answer to that one. Ask me about the run, the "
            "policy, the ledger, or what the arena missed.")
        for chunk in _chunks(answer):
            yield from self._single("chat_delta", text=chunk)
        self._remember(message, answer, notes)
        yield from self._single(
            "chat_answer", text=answer,
            spend_usd=round(self.budget.spent_usd, 5),
            remaining_usd=round(self.budget.remaining_usd, 5),
            turns=len(self.history))

    def _spent_message(self) -> str:
        return (f"This session has spent its ${self.budget.ceiling_usd:.2f} "
                f"allowance, so I am done answering. The run and its ledger are "
                f"still there to read. Start a fresh session to carry on.")

    def system_prompt(self) -> str:
        return CHAT_SYSTEM.format(manual=self.surface.manual())

    # ----------------------------------------------------------- transcript

    def _transcript(self, message: str) -> str:
        parts = []
        if self.run_ids:
            parts.append(f"THE RUN YOU CAN SEE: {self.run_ids[-1]}")
        else:
            parts.append(
                "NO RUN HAS BEEN STARTED IN THIS SESSION. You cannot see any "
                "other run. Offer to start one rather than describing a run "
                "you do not have.")
        for turn in self.history:
            parts.append(f"VISITOR: {turn['visitor']}")
            for note in turn["tools"]:
                parts.append(f">>> {note}")
            parts.append(f"YOU: {turn['agent']}")
        parts.append(f"VISITOR: {message}")
        return "\n\n".join(parts)

    def _remember(self, message: str, answer: str, notes: list[str]) -> None:
        """Keep the turn, bounded, and drop the oldest when over the cap.

        Every step re-sends the whole transcript, so an unbounded history costs
        money on a curve rather than a line. Both the number of turns and the
        size of each one are capped, since one visitor pasting a stack trace
        would otherwise blow the context on its own.
        """
        self.history.append({
            "visitor": message,
            "agent": _clip(answer, MAX_STORED_ANSWER_CHARS),
            "tools": notes,
        })
        while len(self.history) > self.max_turns:
            self.history.pop(0)

    # ------------------------------------------------------- reading a reply

    def _read(self, text: str) -> tuple:
        """Work out whether the model asked for a tool or answered.

        Prose is a legitimate final answer here, which is the one place this
        differs from the orchestrator's loop. There, a reply that is not JSON is
        a protocol failure worth nudging, because the deliverable is structured.
        Here the deliverable is a sentence to a person, so a model that skips
        the envelope and simply answers has done the job and is not corrected
        for it.

        Answer keys are read before tool names, so {"answer": "..."} is never
        mistaken for a call to a tool named answer. An attempted call to a tool
        that does not exist is returned as a call, not swallowed, so the surface
        gets to refuse it by name and the model gets told why.
        """
        parsed = _extract_json(text or "")
        if not isinstance(parsed, dict):
            return ("say", (text or "").strip())

        for key in ANSWER_KEYS:
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return ("say", value.strip())

        for key in NAME_KEYS:
            name = parsed.get(key)
            if isinstance(name, str) and name.strip():
                for args_key in ARG_KEYS:
                    args = parsed.get(args_key)
                    if isinstance(args, dict):
                        return ("tool", name.strip(), args)
                inline = {k: v for k, v in parsed.items()
                          if k not in NAME_KEYS and k != "done"}
                return ("tool", name.strip(), inline)

        # {"list_findings": {"which": "all"}} and {"explain": "hardware"}, which
        # is what models write when they are being helpful about shape.
        if len(parsed) == 1:
            name, value = next(iter(parsed.items()))
            if isinstance(value, dict):
                return ("tool", str(name), value)
            rule = self.surface.tools.get(str(name))
            if rule is not None and rule.sole_argument and not isinstance(value, (list, dict)):
                return ("tool", str(name), {rule.sole_argument: value})

        return ("say", (text or "").strip())

    # ------------------------------------------------------- the tool door

    def use_tool(self, tool: str, args: dict | None = None) -> ToolResult:
        """Check, record, run, record. In that order, with no way around it.

        The same order as the agents' toolbox, and for the same reason: the
        decision is made on the raw arguments before any handler code runs, and
        a refusal is an ordinary result carrying a sentence rather than an
        exception that ends the turn.
        """
        # Copied when it is an object and passed through untouched when it is
        # not. Coercing first would raise on a model that sent a list, which
        # would be this method failing before the check that exists to refuse
        # exactly that.
        args = dict(args) if isinstance(args, dict) else (args or {})
        decision = self.surface.check(tool, args)
        if not decision.allowed:
            self._record("chat_tool_denied", tool=tool, args=args,
                         reason=decision.reason)
            return ToolResult(False, tool, "", refused=True,
                              reason=decision.reason)

        handler = getattr(self, f"_do_{tool}", None)
        if handler is None:
            # Declared on the surface and not implemented here. A configuration
            # mistake rather than an attack, and reported as one.
            reason = f"'{tool}' is on the chat surface but not implemented"
            self._record("chat_tool_error", tool=tool, reason=reason)
            return ToolResult(False, tool, "", reason=reason)

        self._record("chat_tool_call", tool=tool, args=args)
        started = time.time()
        try:
            content = handler(args)
            result = ToolResult(True, tool, _clip(content, 6000),
                                seconds=time.time() - started)
        except _NoData as exc:
            # The tool ran and there is nothing to report. Said out loud, since
            # an empty success is exactly what a model papers over with
            # invention.
            result = ToolResult(False, tool, "", reason=str(exc),
                                seconds=time.time() - started)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            result = ToolResult(False, tool, "",
                                reason=f"{type(exc).__name__}: {exc}",
                                seconds=time.time() - started)
        self._record("chat_tool_result", tool=tool, ok=result.ok,
                     bytes=len(result.content), error=result.reason or None)
        return result

    def _record(self, event: str, **payload) -> None:
        entry = {"event": event, "session": self.session_id, **payload}
        self.record.append(entry)
        if self.ledger is not None:
            try:
                self.ledger.append(event, **payload)
            except Exception:  # noqa: BLE001 - a chat turn is not worth a crash
                pass

    # ------------------------------------------------------------- the run

    def _view(self) -> RunView:
        """The run this session started, or an explanation instead.

        Raising rather than returning None is deliberate: every caller here
        would otherwise have to remember to check, and the one that forgot would
        be the one that let the model answer about a run nobody ran.
        """
        if not self.run_ids:
            raise _NoData(
                "no run has been started in this session, so there is nothing "
                "to report on. Offer to start one with start_review.")
        run_id = self.run_ids[-1]
        try:
            view = self.runs.view(run_id)
        except Exception as exc:  # noqa: BLE001
            raise _NoData(f"the run store could not be read: "
                          f"{type(exc).__name__}") from exc
        if view is None:
            raise _NoData(
                f"run {run_id} is no longer held by the server, so its detail "
                f"cannot be quoted. Completed runs are reaped after an hour.")
        return view

    # --------------------------------------------------------------- tools

    def _do_start_review(self, args: dict) -> str:
        task_key = str(args["task_key"]).strip().lower()
        run_id, refusal = self.runs.start(task_key)
        if not run_id:
            return (f"the run did not start: {refusal}" if refusal
                    else "the run did not start, and the server gave no reason")
        self.run_ids.append(run_id)
        return (f"run {run_id} started on the '{task_key}' task against the "
                f"demo target. It takes a couple of minutes. Findings appear as "
                f"hunters raise them, then each one goes to three verifiers.")

    def _do_run_status(self, args: dict) -> str:
        view = self._view()
        lines = [f"run {view.run_id}"]
        if view.task:
            lines.append(f"task: {view.task}")
        state = "finished" if view.finished else f"running, phase '{view.phase}'"
        lines.append(f"state: {state}")
        if view.lanes:
            lines.append(f"lanes: {', '.join(view.lanes)}")
        settled = [f for f in view.findings if f["settled"]]
        lines.append(
            f"findings: {view.raised} raised, {len(view.standing)} standing, "
            f"{len(view.refuted)} refuted, "
            f"{view.raised - len(settled)} still being judged")
        lines.append(f"spend: ${view.spend_usd:.4f} over {view.calls} model calls")
        if not view.spend_is_exact:
            lines.append(
                "that spend is added up from the live stream, so it understates "
                "the run slightly: the planner's call carries no cost line on "
                "the stream. The closing report has the exact figure.")
        lines.append(f"tools: {view.tool_calls} calls, "
                     f"{view.refusals} refused by policy")
        if view.seconds:
            lines.append(f"elapsed: {view.seconds}s")
        if view.halted:
            lines.append(f"halted: {view.halted}")
        if view.failed:
            lines.append(f"failed: {view.failed}")
        return "\n".join(lines)

    def _do_list_findings(self, args: dict) -> str:
        view = self._view()
        which = str(args["which"]).strip().lower()
        pile = {"all": view.findings, "standing": view.standing,
                "refuted": view.refuted}[which]
        if not pile:
            if not view.findings:
                return (f"run {view.run_id} has raised nothing yet "
                        f"(phase '{view.phase}').")
            return f"the {which} pile is empty for run {view.run_id}."
        lines = [f"{len(pile)} {which} of {view.raised} raised in run "
                 f"{view.run_id}:"]
        for finding in pile:
            kept = sum(1 for v in finding["verdicts"] if not v["refuted"])
            total = len(finding["verdicts"])
            if not finding["settled"]:
                mark = "judging"
            else:
                mark = "STANDING" if finding["survived"] else "refuted"
            lines.append(
                f"  {finding['n']}  [{finding['severity'] or 'medium'}] "
                f"{mark} {kept}/{total} kept  "
                f"{finding['file']}:{finding['line']}  {finding['title']}")
        return "\n".join(lines)

    def _do_finding_detail(self, args: dict) -> str:
        view = self._view()
        # The surface has already established this is a whole number, so the
        # only work left is the shape a model writes it in: 3, "3" and 3.0 all
        # arrive and all mean the third finding.
        number = int(float(str(args["n"]).strip()))
        finding = view.finding(number)
        if finding is None:
            raise _NoData(
                f"run {view.run_id} has no finding numbered {number}; it has "
                f"{view.raised}. List them first.")
        verdict_count = len(finding["verdicts"])
        kept = sum(1 for v in finding["verdicts"] if not v["refuted"])
        state = ("still being judged" if not finding["settled"]
                 else ("STANDING, it survived verification" if finding["survived"]
                       else "REFUTED, it was thrown out"))
        lines = [
            f"finding {finding['n']} of run {view.run_id} (id {finding['id']})",
            f"title: {finding['title']}",
            f"where: {finding['file']}:{finding['line']}",
            f"severity: {finding['severity'] or 'medium'}",
            f"lane: {finding['lane']}",
            f"outcome: {state} ({kept} of {verdict_count} verifiers failed to "
            f"refute it)",
            f"summary: {finding['summary']}",
        ]
        lines.append(
            f"claimed failure: {finding['failure_scenario']}"
            if finding["failure_scenario"]
            else "claimed failure: the stream carries no failure scenario for "
                 "this one, so I will not invent it")
        if finding["evidence"]:
            lines.append(f"quoted evidence: {finding['evidence']}")
        if finding["duplicates"]:
            lines.append(f"corroboration: {finding['duplicates']} other hunter"
                         f"{'s' if finding['duplicates'] > 1 else ''} raised "
                         f"the same defect independently")
        if not finding["verdicts"]:
            lines.append("verdicts: none recorded yet")
        for index, verdict in enumerate(finding["verdicts"], 1):
            call = "refuted" if verdict["refuted"] else "could not refute"
            lines.append(
                f"verifier {index} ({verdict['agent'] or 'unnamed'}): {call}, "
                f"confidence {verdict['confidence'] or 'unstated'}")
            lines.append(f"  reasoning: {verdict['reasoning'] or '(none given)'}")
            if verdict["concrete_failure"]:
                lines.append(f"  concrete failure: {verdict['concrete_failure']}")
        return "\n".join(lines)

    def _do_policy_summary(self, args: dict) -> str:
        policy: dict = {}
        source = ""
        if self.run_ids:
            try:
                policy = self._view().policy or {}
                source = "recorded at the start of this session's run"
            except _NoData:
                policy = {}
        if not policy:
            try:
                policy = self.runs.policy() or {}
                source = ("the policy this arena runs, declared but not yet "
                          "exercised by a run in this session")
            except Exception:  # noqa: BLE001
                policy = {}
        if not policy:
            raise _NoData("no policy is on hand to quote")
        lines = [f"policy '{policy.get('name', 'unnamed')}' ({source})"]
        if policy.get("description"):
            lines.append(policy["description"])
        for tool, rule in (policy.get("tools") or {}).items():
            bits = []
            if rule.get("path_scopes"):
                bits.append("within " + ", ".join(rule["path_scopes"]))
            if rule.get("commands"):
                bits.append("commands: " + ", ".join(rule["commands"]))
            if rule.get("url_hosts"):
                bits.append("hosts: " + ", ".join(rule["url_hosts"]))
            if rule.get("max_bytes"):
                bits.append(f"{rule['max_bytes']} byte ceiling")
            lines.append(f"  {tool}: {'; '.join(bits) or 'no arguments'}")
        lines.append(
            "Not on the list and therefore refused: any network call, any write "
            "outside the scratch directory, any path outside the workspace, and "
            "any command that is not one of the permitted binaries. The check "
            "runs before the tool does, and a check that raises is a refusal.")
        return "\n".join(lines)

    def _do_ledger_summary(self, args: dict) -> str:
        view = self._view()
        ledger = view.ledger
        if ledger is None:
            raise _NoData(f"no ledger file is readable for run {view.run_id}")
        entries = ledger.entries()
        if not entries:
            raise _NoData(f"the ledger for run {view.run_id} is empty")
        broken = ledger.verify()
        denied = sum(1 for e in entries if e.get("event") == "tool_denied")
        lines = [
            f"ledger for run {view.run_id}",
            f"entries: {len(entries)}",
            f"root hash: {ledger.head}",
            f"chain: {'verifies clean' if broken is None else f'BREAKS at entry {broken.seq}: {broken.reason}'}",
            f"policy refusals recorded: {denied}",
            "Each entry carries the SHA-256 of the one before it, so an edit, a "
            "deletion, an insertion or a swap all show up. It is tamper-evident "
            "rather than tamper-proof, and the whole file is downloadable so you "
            "can recompute the chain yourself.",
        ]
        return "\n".join(lines)

    def _do_missed_defects(self, args: dict) -> str:
        view = self._view()
        planted = self.runs.answer_key()
        if not planted:
            raise _NoData(
                "there is no answer key for this workspace, so I cannot say "
                "what was missed. Only the synthetic demo target ships one.")
        standing = view.standing
        if not view.finished:
            note = ("The run is still going, so this is what stands right now "
                    "rather than a final score.\n")
        else:
            note = ""
        missed = [d for d in planted
                  if not any(_same_defect(d, f) for f in standing)]
        found = len(planted) - len(missed)
        lines = [f"{note}{found} of {len(planted)} planted defects were found "
                 f"and survived verification in run {view.run_id}."]
        if not missed:
            lines.append("Nothing on the key was missed.")
            return "\n".join(lines)
        lines.append(f"Missed, {len(missed)} of them:")
        for defect in missed:
            lines.append(
                f"  {defect.get('id', '?')}  [{defect.get('difficulty', '?')}]  "
                f"{defect.get('file', '?')}:{defect.get('line', '?')}")
            lines.append(f"    {defect.get('summary', '')}")
        return "\n".join(lines)

    def _do_explain(self, args: dict) -> str:
        topic = str(args["topic"]).strip().lower()
        return EXPLAINERS[topic]


class _NoData(Exception):
    """The tool ran and the data is not there.

    Its own type rather than a returned empty string, because absence is the
    case a model most reliably fills in for itself. Every one of these carries a
    sentence saying what is missing and, where there is one, what would get it.
    """


def _same_defect(defect: dict, finding: dict) -> bool:
    """Same file, and within twelve lines.

    Deliberately generous on the line number: a defect reported at the call site
    rather than at the declaration is still that defect found. The same rule the
    CLI scores with, kept here rather than imported so that the conversational
    surface does not depend on the command-line entry point.
    """
    left = Path(str(defect.get("file", ""))).name.lower()
    right = Path(str(finding.get("file", ""))).name.lower()
    if not left or left != right:
        return False
    try:
        return abs(int(defect.get("line") or 0) - int(finding.get("line") or 0)) <= 12
    except (TypeError, ValueError):
        return False


def _chunks(text: str, size: int = 32):
    """Break an answer on word boundaries for delivery.

    Splitting mid-word makes a finished string look like a model typing, which
    it is not. Whole words arriving in groups is honest about what this is: a
    completed answer delivered progressively.
    """
    words = (text or "").split(" ")
    buffer = ""
    for word in words:
        candidate = f"{buffer} {word}" if buffer else word
        if len(candidate) >= size:
            yield candidate
            buffer = ""
        else:
            buffer = candidate
    if buffer:
        yield buffer
