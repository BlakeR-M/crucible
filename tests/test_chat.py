"""Checks for the conversational agent a visitor talks to.

Plain script, no test framework, no network and no key:

    python tests/test_chat.py

The model is replaced by a stand-in that answers from a script, and the run
store by a stub holding event buffers that were built by hand. Both are here
because the property this file is actually defending is that the agent never
speaks about a run it cannot see, and the only way to check that is to hand it a
world where every fact is known in advance.

The checks that matter most are the ones about refusing. A tool that is not on
the surface is refused rather than reached for. A refusal comes back as text the
model can read rather than as an exception that ends the turn. A run that does
not exist produces a sentence saying so, never a plausible number. And a session
that has spent its allowance stops calling the model at all.
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crucible.chat import (  # noqa: E402
    EXPLAINER_TOPICS, ChatAgent, ChatSurface, ChatTool, RunView,
    visitor_surface,
)
from crucible.ledger import Ledger  # noqa: E402
from crucible.providers import Budget, Completion, Tier  # noqa: E402

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


# --------------------------------------------------------------- stand-ins

class ScriptedChat:
    """A model that says what it was told to say, in order.

    One conversation at a time and one thread, so a queue is safe here in a way
    it would not be for the orchestrator's concurrent agents.
    """

    name = "scripted"

    def __init__(self, replies, *, price=0.0, explode=False):
        self.replies = list(replies)
        self.prompts: list[str] = []
        self.systems: list[str] = []
        self.calls = 0
        self.price = price
        self.explode = explode

    def complete(self, system, user, tier, budget, *, max_output=2000,
                 reasoning="low"):
        self.calls += 1
        self.prompts.append(user)
        self.systems.append(system)
        if self.explode:
            raise RuntimeError("provider went down")
        text = self.replies.pop(0) if self.replies else json.dumps(
            {"answer": "nothing left in the script"})
        # Reserve then settle exactly as the real provider does, so a ceiling
        # bites in a test instead of being bypassed by a stand-in that only
        # books costs afterwards.
        held = budget.reserve("gpt-5-mini", len(user) // 4, max_output)
        cost = budget.settle("gpt-5-mini", held, len(user) // 4,
                             max(1, len(text) // 4))
        if self.price:
            cost += budget.record("gpt-5-mini", 0, int(self.price * 500_000))
        return Completion(text, "scripted", len(user) // 4, len(text) // 4,
                          cost, 0.0)


def say(text: str) -> str:
    return json.dumps({"answer": text})


def call(tool: str, **args) -> str:
    return json.dumps({"tool": tool, "args": args})


class StubRuns:
    """Run state without a server: buffers in a dict, keyed by run id."""

    def __init__(self, tmp: Path, *, buffers=None, key=None, refuse=""):
        self.tmp = tmp
        self.buffers: dict[str, list[dict]] = dict(buffers or {})
        self.key = list(key or [])
        self.refuse = refuse
        self.started: list[str] = []
        self.next_id = "run0001"

    def tasks(self):
        return {"full": "everything", "money": "money handling",
                "concurrency": "races", "auth": "access control"}

    def start(self, task_key):
        self.started.append(task_key)
        if self.refuse:
            return None, self.refuse
        run_id = self.next_id
        self.buffers.setdefault(run_id, list(EVENTS))
        return run_id, ""

    def view(self, run_id):
        events = self.buffers.get(run_id)
        if events is None:
            return None
        return RunView(run_id, events, ledger=self.ledger(run_id))

    def ledger(self, run_id):
        path = self.tmp / f"{run_id}.jsonl"
        return Ledger(path) if path.is_file() else None

    def policy(self):
        from crucible.policy import review_policy

        return review_policy(self.tmp).as_dict()

    def answer_key(self):
        return list(self.key)


# One run, written out as the stream that produced it. Two findings: the first
# survives two of three, the second is refuted by all three. A third was raised
# and merged into the first as a duplicate, which is what the real dedupe does.
EVENTS = [
    {"kind": "run_started", "run_id": "run0001", "task": "money handling",
     "policy": {"name": "review", "description": "read only",
                "tools": {"read_file": {"path_scopes": ["/ws"], "commands": [],
                                        "url_hosts": [], "max_bytes": 1000}}},
     "ceiling": 0.6},
    {"kind": "phase", "phase": "plan"},
    {"kind": "lane", "name": "money", "brief": "rounding"},
    {"kind": "agent_thought", "agent": "hunter-1", "step": 0, "cost": 0.01},
    {"kind": "tool", "agent": "hunter-1", "tool": "read_file", "refused": False},
    {"kind": "tool", "agent": "hunter-1", "tool": "read_file", "refused": True,
     "reason": "path sits outside the workspace"},
    {"kind": "finding_raised", "id": "aaa1", "lane": "money",
     "title": "discount rounds the wrong way", "file": "bookings/invoicing.py",
     "line": 70, "severity": "high", "summary": "float creeps into Decimal",
     "failure_scenario": "150.75 at 10 per cent bills a cent short"},
    {"kind": "finding_raised", "id": "bbb2", "lane": "money",
     "title": "totals drift", "file": "bookings/money.py", "line": 12,
     "severity": "low", "summary": "sum order matters",
     "failure_scenario": "three items in a different order differ"},
    {"kind": "finding_raised", "id": "ccc3", "lane": "invoicing",
     "title": "same rounding bug", "file": "bookings/invoicing.py", "line": 71,
     "severity": "high", "summary": "duplicate of the first",
     "failure_scenario": "same thing"},
    {"kind": "finding_merged", "into": "aaa1", "dropped": "ccc3",
     "title": "same rounding bug"},
    {"kind": "phase", "phase": "verify", "findings": 2},
    {"kind": "verdict", "finding": "aaa1", "agent": "verifier-aaa1-1",
     "refuted": False, "confidence": "high",
     "reasoning": "reproduced it: 15.075 rounds down under banker's rounding"},
    {"kind": "verdict", "finding": "aaa1", "agent": "verifier-aaa1-2",
     "refuted": False, "confidence": "medium",
     "reasoning": "money.percentage_of exists and is not used here"},
    {"kind": "verdict", "finding": "aaa1", "agent": "verifier-aaa1-3",
     "refuted": True, "confidence": "low",
     "reasoning": "could not construct inputs where the cent matters"},
    {"kind": "finding_settled", "id": "aaa1", "survived": True,
     "survived_by": 2, "refuted_by": 1},
    {"kind": "verdict", "finding": "bbb2", "agent": "verifier-bbb2-1",
     "refuted": True, "confidence": "high",
     "reasoning": "Decimal addition is associative here, the claim is wrong"},
    {"kind": "verdict", "finding": "bbb2", "agent": "verifier-bbb2-2",
     "refuted": True, "confidence": "high",
     "reasoning": "no ordering produces a different total"},
    {"kind": "verdict", "finding": "bbb2", "agent": "verifier-bbb2-3",
     "refuted": True, "confidence": "medium", "reasoning": "style preference"},
    {"kind": "finding_settled", "id": "bbb2", "survived": False,
     "survived_by": 0, "refuted_by": 3},
    {"kind": "run_finished", "run_id": "run0001", "task": "money handling",
     "lanes": ["money"], "raised": 2, "survived": 1, "spend_usd": 0.1834,
     "calls": 9, "tool_calls": 6, "refusals": 1, "seconds": 71.2,
     "ledger_head": "f" * 64, "halted": "",
     # The report's verdicts are the orchestrator's stored dicts, which carry a
     # concrete failure and no verifier name. The stream carries the name and
     # not the failure, which is why the two are merged rather than one
     # replacing the other.
     "findings": [{"id": "aaa1", "evidence": "line 70: float(subtotal)",
                   "failure_scenario": "150.75 at 10 per cent bills a cent short",
                   "verdicts": [
                       {"refuted": False, "confidence": "high",
                        "reasoning": "reproduced it: 15.075 rounds down under "
                                     "banker's rounding",
                        "concrete_failure": "150.75 at 10% gives 15.07 not 15.08"},
                       {"refuted": False, "confidence": "medium",
                        "reasoning": "money.percentage_of exists and is not "
                                     "used here",
                        "concrete_failure": ""},
                       {"refuted": True, "confidence": "low",
                        "reasoning": "could not construct inputs where the cent "
                                     "matters",
                        "concrete_failure": ""}]}]},
]

ANSWER_KEY = [
    {"id": "D3", "file": "bookings/invoicing.py", "line": 70,
     "difficulty": "medium", "summary": "apply_discount drops into float"},
    {"id": "D8", "file": "bookings/auth.py", "line": 43, "difficulty": "hard",
     "summary": "level_for treats a level of 0 as missing"},
]


def agent(tmp: Path, replies, *, ceiling=1.0, runs=None, started=False,
          max_turns=12, **kw) -> tuple[ChatAgent, ScriptedChat, StubRuns]:
    provider = ScriptedChat(replies, **kw)
    runs = runs if runs is not None else StubRuns(tmp, buffers={"run0001": EVENTS},
                                                  key=ANSWER_KEY)
    chat = ChatAgent(provider, runs, Budget(ceiling_usd=ceiling),
                     max_turns=max_turns, session_id="test")
    if started:
        chat.run_ids.append("run0001")
    return chat, provider, runs


# ------------------------------------------------------------------ checks

def surface_checks() -> None:
    section("the tool surface is deny-by-default")
    surface = visitor_surface(("full", "money"))

    check("every advertised tool is declared",
          set(surface.tools) == {
              "start_review", "run_status", "list_findings", "finding_detail",
              "policy_summary", "ledger_summary", "missed_defects", "explain"})
    check("a listed tool with good arguments is permitted",
          bool(surface.check("list_findings", {"which": "standing"})))
    check("an unlisted tool is refused",
          not surface.check("send_email", {"to": "blake@example.com"}))
    check("the refusal names the tool and the surface",
          "send_email" in surface.check("send_email", {}).reason
          and "visitor" in surface.check("send_email", {}).reason)
    check("the refusal lists what is actually available",
          "run_status" in surface.check("send_email", {}).reason)
    check("a tool that reads files is not on this surface",
          not surface.check("read_file", {"path": "/etc/passwd"}))
    check("an undeclared argument is refused",
          not surface.check("run_status", {"path": "/etc/passwd"}))
    check("a missing required argument is refused",
          not surface.check("finding_detail", {}))
    check("an empty required argument is refused",
          not surface.check("explain", {"topic": ""}))
    check("a value outside the permitted set is refused",
          not surface.check("list_findings", {"which": "everything"}))
    check("the choice refusal quotes the permitted values",
          "standing" in surface.check(
              "list_findings", {"which": "everything"}).reason)
    check("a task key the arena does not have is refused",
          not surface.check("start_review", {"task_key": "auth"}))
    check("a task key it does have is permitted",
          bool(surface.check("start_review", {"task_key": "money"})))
    check("a non-numeric finding number is refused",
          not surface.check("finding_detail", {"n": "the auth one"}))
    check("a numeric string is accepted as a number",
          bool(surface.check("finding_detail", {"n": "3"})))
    check("a boolean is not a number",
          not surface.check("finding_detail", {"n": True}))
    check("arguments arriving as something other than an object are refused",
          not surface.check("list_findings", ["standing"]))
    check("a checker that raises refuses rather than throws",
          not ChatSurface("x", {"boom": ChatTool("", {"a": "a"},
                                                 choices={"a": None})}
                          ).check("boom", {"a": "1"}))
    check("every explainer topic on the surface has text behind it",
          set(EXPLAINER_TOPICS) == set(__import__(
              "crucible.chat", fromlist=["EXPLAINERS"]).EXPLAINERS))
    check("the manual is generated from the declarations",
          "missed_defects()" in surface.manual()
          and "standing" in surface.manual())
    check("the surface renders as data",
          surface.as_dict()["tools"]["explain"]["required"] == ["topic"])


def view_checks() -> None:
    section("run state is read from the stream, not invented")
    view = RunView("run0001", EVENTS)

    check("both judged findings are present", view.raised == 2)
    check("the merged duplicate is not a third finding",
          [f["id"] for f in view.findings] == ["aaa1", "bbb2"])
    check("corroboration is kept on the survivor",
          view.findings[0]["duplicates"] == 1)
    check("findings are numbered for a person to refer to",
          [f["n"] for f in view.findings] == [1, 2])
    check("the standing pile holds the survivor",
          [f["id"] for f in view.standing] == ["aaa1"])
    check("the refuted pile holds the one that was killed",
          [f["id"] for f in view.refuted] == ["bbb2"])
    check("every verifier verdict is kept, refutations included",
          len(view.findings[0]["verdicts"]) == 3
          and len(view.findings[1]["verdicts"]) == 3)
    check("verifier reasoning survives verbatim",
          "banker" in view.findings[0]["verdicts"][0]["reasoning"])
    check("a refuted finding keeps its claimed failure, which the report drops",
          view.findings[1]["failure_scenario"].startswith("three items"))
    check("the closing report's fuller detail is merged over the stream",
          "15.07" in view.findings[0]["verdicts"][0]["concrete_failure"]
          and view.findings[0]["evidence"].startswith("line 70"))
    check("and the merge keeps the verifier name the report does not carry",
          view.findings[0]["verdicts"][0]["agent"] == "verifier-aaa1-1")
    check("a verdict the stream never carried is still picked up",
          len(RunView("late", [EVENTS[6], EVENTS[-1]]).findings[0]["verdicts"]) == 3)
    check("the exact spend comes from the report",
          view.spend_usd == 0.1834 and view.spend_is_exact)
    check("counts come from the report", view.calls == 9 and view.tool_calls == 6
          and view.refusals == 1)
    check("the run is known to have finished", view.finished)

    live = RunView("run0001", EVENTS[:11])
    check("a live run reports the phase it is in", live.phase == "verify")
    check("a live run has not settled anything",
          not any(f["settled"] for f in live.findings))
    check("live spend is added up from the stream and flagged inexact",
          abs(live.spend_usd - 0.01) < 1e-9 and not live.spend_is_exact)
    check("live tool calls and refusals are counted",
          live.tool_calls == 2 and live.refusals == 1)
    check("an empty stream is an empty run, not a crash",
          RunView("nothing", []).raised == 0)
    check("junk on the stream is ignored rather than fatal",
          RunView("junk", [None, "text", {"kind": "unknown"}]).raised == 0)


def dispatch_checks(tmp: Path) -> None:
    section("every listed tool dispatches against real state")
    chat, _, runs = agent(tmp, [], started=True)
    (tmp / "run0001.jsonl").unlink(missing_ok=True)
    ledger = Ledger(tmp / "run0001.jsonl")
    ledger.append("run_started", run_id="run0001")
    ledger.append("tool_denied", tool="read_file", reason="outside the workspace")

    status = chat.use_tool("run_status", {})
    check("run_status answers from the run", status.ok)
    check("and reports the counts it was given",
          "2 raised" in status.content and "1 standing" in status.content)
    check("and the real spend", "0.1834" in status.content)

    listing = chat.use_tool("list_findings", {"which": "all"})
    check("list_findings lists both piles under 'all'",
          "STANDING" in listing.content and "refuted" in listing.content)
    check("standing lists only the survivor",
          "invoicing.py" in chat.use_tool(
              "list_findings", {"which": "standing"}).content
          and "money.py" not in chat.use_tool(
              "list_findings", {"which": "standing"}).content)
    check("refuted lists only the one that was killed",
          "money.py" in chat.use_tool(
              "list_findings", {"which": "refuted"}).content)

    detail = chat.use_tool("finding_detail", {"n": 2})
    check("finding_detail opens the refuted finding", detail.ok)
    check("and gives file and line",
          "bookings/money.py:12" in detail.content)
    check("and the claimed failure",
          "three items in a different order" in detail.content)
    check("and quotes every verifier's reasoning",
          detail.content.count("reasoning:") == 3
          and "associative" in detail.content)
    check("and says plainly that it was thrown out",
          "REFUTED" in detail.content)
    check("a finding number that does not exist is reported, not guessed",
          "no finding numbered 9" in chat.use_tool(
              "finding_detail", {"n": 9}).as_text())

    policy = chat.use_tool("policy_summary", {})
    check("policy_summary quotes the policy the run recorded",
          policy.ok and "read_file" in policy.content)
    check("and states what is refused outright",
          "network" in policy.content)

    led = chat.use_tool("ledger_summary", {})
    check("ledger_summary counts the entries", led.ok and "entries: 2" in led.content)
    check("and reports the chain verifying", "verifies clean" in led.content)
    check("and the root hash", ledger.head in led.content)
    check("and counts the refusals in the record",
          "policy refusals recorded: 1" in led.content)

    missed = chat.use_tool("missed_defects", {})
    check("missed_defects scores against the answer key", missed.ok)
    check("and names the defect the run walked past",
          "D8" in missed.content and "auth.py" in missed.content)
    check("and does not claim the one it found",
          "D3" not in missed.content)
    check("and says how many of them were found",
          "1 of 2 planted defects" in missed.content)

    for topic in EXPLAINER_TOPICS:
        result = chat.use_tool("explain", {"topic": topic})
        check(f"explain({topic}) returns real text",
              result.ok and len(result.content) > 200)
    check("explain(verification) describes the majority rule",
          "two of the three" in chat.use_tool(
              "explain", {"topic": "verification"}).content)
    check("explain(hardware) is honest that the thinking is not local",
          "not local" in chat.use_tool(
              "explain", {"topic": "hardware"}).content)

    section("starting a run")
    chat2, _, runs2 = agent(tmp, [])
    started = chat2.use_tool("start_review", {"task_key": "money"})
    check("start_review launches against the demo target", started.ok)
    check("and the task reached the run store", runs2.started == ["money"])
    check("and the session now has a run to talk about",
          chat2.run_ids == ["run0001"])
    check("a run the server refuses comes back as the server's reason",
          "two runs are already in flight" in ChatAgent(
              ScriptedChat([]), StubRuns(tmp, refuse="two runs are already in "
                                                     "flight"),
              Budget(ceiling_usd=1.0)).use_tool(
              "start_review", {"task_key": "full"}).as_text())


def refusal_checks(tmp: Path) -> None:
    section("a refusal is text, not an exception")
    chat, provider, _ = agent(tmp, [], started=True)

    result = chat.use_tool("send_email", {"to": "someone@example.com"})
    check("an unlisted tool is refused at the door", result.refused)
    check("the refusal is a result rather than a raise", not result.ok)
    check("it comes back as text the model can read",
          result.as_text().startswith("REFUSED BY POLICY")
          and "send_email" in result.as_text())
    check("it was recorded",
          any(r["event"] == "chat_tool_denied" and r["tool"] == "send_email"
              for r in chat.record))
    check("and the handler never ran", not result.content)

    check("a listed tool with a bad argument is refused the same way",
          chat.use_tool("list_findings", {"which": "../../etc"}).refused)
    check("arguments sent as a list are refused rather than crashing the door",
          chat.use_tool("list_findings", ["standing"]).refused)
    check("a finding number written as a float still opens that finding",
          chat.use_tool("finding_detail", {"n": 1.0}).ok)
    check("a tool declared but not implemented is reported as a mistake",
          "not implemented" in ChatAgent(
              ScriptedChat([]), StubRuns(tmp), Budget(ceiling_usd=1.0),
              surface=ChatSurface("x", {"ghost": ChatTool("a ghost")})
          ).use_tool("ghost", {}).as_text())

    section("a refused tool does not end the conversation")
    chat, provider, _ = agent(
        tmp,
        [call("delete_run", run_id="run0001"),
         say("That one is not mine to call. Here is what I can do instead.")],
        started=True)
    answer = chat.send("delete the run please")
    check("the model was told about the refusal",
          "REFUSED BY POLICY" in provider.prompts[-1])
    check("and the turn still produced an answer",
          answer.startswith("That one is not mine"))
    check("the refusal was recorded on the session",
          any(r["event"] == "chat_tool_denied" for r in chat.record))


def no_run_checks(tmp: Path) -> None:
    section("it cannot answer about a run that does not exist")
    chat, provider, _ = agent(tmp, [])

    for tool, args in (("run_status", {}), ("list_findings", {"which": "all"}),
                       ("finding_detail", {"n": 1}), ("ledger_summary", {}),
                       ("missed_defects", {})):
        result = chat.use_tool(tool, args)
        check(f"{tool} with no run says so rather than answering",
              not result.ok and "no run has been started" in result.as_text())
        check(f"{tool} returns nothing that could be quoted as a number",
              result.content == "")

    section("a run the server no longer holds")
    chat.run_ids.append("vanished")
    gone = chat.use_tool("run_status", {})
    check("a run id the store does not know is reported as missing",
          not gone.ok and "no longer held" in gone.as_text())
    check("and the run id is named so the visitor can check",
          "vanished" in gone.as_text())

    section("no answer key, no scoring")
    bare = ChatAgent(ScriptedChat([]), StubRuns(tmp, buffers={"run0001": EVENTS}),
                     Budget(ceiling_usd=1.0))
    bare.run_ids.append("run0001")
    check("missed_defects without a key refuses to guess",
          "no answer key" in bare.use_tool("missed_defects", {}).as_text())

    section("the transcript tells the model there is no run")
    chat2, provider2, _ = agent(tmp, [say("Nothing has run yet.")])
    chat2.send("what did the last run find?")
    check("the prompt states plainly that no run exists",
          "NO RUN HAS BEEN STARTED IN THIS SESSION" in provider2.prompts[0])
    check("the system prompt forbids inventing figures",
          "Never estimate a number" in provider2.systems[0])


def conversation_checks(tmp: Path) -> None:
    section("a turn that reads state before answering")
    chat, provider, _ = agent(
        tmp,
        [call("list_findings", which="refuted"),
         call("finding_detail", n=2),
         say("Finding 2 was thrown out by all three verifiers. One of them "
             "reproduced the arithmetic and found Decimal addition associative "
             "there, so the claim was wrong.")],
        started=True)
    events = list(chat.stream("why was finding 2 thrown out?"))
    kinds = [e["kind"] for e in events]

    check("the turn announced itself", kinds[0] == "chat_started")
    check("both tool calls reached the stream",
          sum(1 for k in kinds if k == "chat_tool") == 2)
    check("the answer arrived in pieces",
          sum(1 for k in kinds if k == "chat_delta") > 1)
    check("the turn closed with the whole answer", kinds[-1] == "chat_answer")
    check("the closing event carries the session spend",
          events[-1]["spend_usd"] > 0)
    check("the tool results were fed back to the model",
          "associative" in provider.prompts[-1])
    check("send returns the same text the stream ended with",
          events[-1]["text"].startswith("Finding 2 was thrown out"))

    section("the shapes a model actually writes")
    for label, reply in [
        ("the documented envelope", json.dumps(
            {"tool": "run_status", "args": {}})),
        ("arguments beside the name", json.dumps(
            {"tool": "explain", "topic": "numbers"})),
        ("the tool name as the only key", json.dumps(
            {"explain": {"topic": "numbers"}})),
        ("a bare argument under the tool name", json.dumps(
            {"explain": "numbers"})),
    ]:
        chat2, provider2, _ = agent(tmp, [reply, say("done")], started=True)
        chat2.send("tell me")
        check(f"a tool call written as {label} is understood",
              provider2.calls == 2 and ">>> you called" in provider2.prompts[-1])

    chat3, _, _ = agent(tmp, ["Plain prose, no JSON envelope at all."],
                        started=True)
    check("prose is a legitimate answer rather than a protocol error",
          chat3.send("hello") == "Plain prose, no JSON envelope at all.")

    chat4, provider4, _ = agent(tmp, [json.dumps({"answer": "run_status"})],
                                started=True)
    check("an answer that merely mentions a tool name is not a tool call",
          chat4.send("what can you do") == "run_status" and provider4.calls == 1)

    section("opening and personality")
    chat5, _, _ = agent(tmp, [])
    check("the opening makes a specific offer rather than inviting a question",
          "start a run" in chat5.opening()
          and "thrown out" in chat5.opening())
    check("the system prompt sets the voice",
          "Australian" in chat5.system_prompt()
          and "Do not compliment the visitor" in chat5.system_prompt())
    check("the system prompt carries the generated manual",
          "missed_defects()" in chat5.system_prompt())

    section("a model call that fails")
    chat6, _, _ = agent(tmp, [], explode=True)
    answer = chat6.send("anything")
    check("a provider failure comes back as a sentence, not a stack trace",
          "model call failed" in answer and "RuntimeError" in answer)


def bounds_checks(tmp: Path) -> None:
    section("history is capped and the oldest turns fall off")
    chat, provider, _ = agent(
        tmp, [say(f"answer {i}") for i in range(8)], max_turns=3, started=True)
    for i in range(8):
        chat.send(f"question {i}")
    check("history holds no more than the cap", len(chat.history) == 3)
    check("it holds the newest turns", chat.history[-1]["visitor"] == "question 7")
    check("and the oldest are gone",
          all(t["visitor"] != "question 0" for t in chat.history))
    check("the trimmed transcript is what the model sees",
          "question 0" not in provider.prompts[-1]
          and "question 6" in provider.prompts[-1])

    section("stored turns are capped in size as well as in number")
    chat2, provider2, _ = agent(tmp, [say("x" * 4000), say("ok")], started=True)
    chat2.send("y" * 6000)
    check("a long visitor message is clipped before it is stored",
          len(chat2.history[-1]["visitor"]) < 6000)
    check("and so is a long answer",
          len(chat2.history[-1]["agent"]) < 4000)

    section("the spend cap refuses further calls")
    spent = ChatAgent(ScriptedChat([say("should never be reached")]),
                      StubRuns(tmp), Budget(ceiling_usd=0.0))
    provider = spent.provider
    answer = spent.send("hello?")
    check("a session with nothing left does not call the model",
          provider.calls == 0)
    check("and says the allowance is gone", "allowance" in answer)

    budget = Budget(ceiling_usd=0.02)
    chat3 = ChatAgent(ScriptedChat([say("first"), say("second")]),
                      StubRuns(tmp), budget)
    first = chat3.send("one")
    budget.record("gpt-5-mini", 0, 1_000_000)  # burn the rest of the ceiling
    second = chat3.send("two")
    check("a session answers until the ceiling is reached", first == "first")
    check("and refuses afterwards without raising", "allowance" in second)
    check("the refusal quotes the ceiling", "$0.02" in second)

    section("a provider that trips the ceiling mid-call is caught")
    tight = ChatAgent(ScriptedChat([say("never")]), StubRuns(tmp),
                      Budget(ceiling_usd=0.000001))
    check("BudgetExceeded comes back as a sentence",
          "allowance" in tight.send("hello"))

    section("one turn at a time")
    chat4, _, _ = agent(tmp, [say("a"), say("b")], started=True)
    chat4._turn_lock.acquire()
    try:
        check("a second concurrent turn is turned away rather than interleaved",
              "One message at a time" in chat4.send("while busy"))
    finally:
        chat4._turn_lock.release()

    section("tool steps are bounded")
    chat5, provider5, _ = agent(
        tmp, [call("run_status") for _ in range(10)], started=True)
    answer = chat5.send("go round in circles")
    check("a model that only ever calls tools is stopped",
          provider5.calls <= 4)
    check("and the visitor is told rather than given an invented summary",
          "without landing on an answer" in answer)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        surface_checks()
        view_checks()
        dispatch_checks(tmp)
        refusal_checks(tmp)
        no_run_checks(tmp)
        conversation_checks(tmp)
        bounds_checks(tmp)
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
