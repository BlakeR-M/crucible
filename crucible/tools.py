"""The only door an agent has to the world.

Every capability an agent has lives here, and every one of them is reached
through a single `invoke`. That matters more than it looks: a toolbox with a
side entrance is a toolbox with no policy, because the one call site that
forgot to check is the one an agent will find. There is one call site.

The order inside `invoke` is deliberate. The policy decides first, on the raw
arguments, before any tool code runs. A refusal is written to the ledger with
its reason and returned as an ordinary result, so a blocked agent gets a
sentence it can reason about rather than an exception that ends its turn. Then
the call is recorded, then it runs, then the outcome is recorded. An agent
cannot act without leaving both halves of the record behind.

Results are truncated on the way out. An agent that reads a 40,000 line file
does not get 40,000 lines; it gets the head, a marker, and the count. Context
is a budget like any other.
"""

from __future__ import annotations

import re
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .ledger import Ledger
from .policy import Policy

MAX_CHARS = 24_000
MAX_MATCHES = 60

# What a child process started by a tool is allowed to see of this process's
# environment. An allowlist rather than a denylist: the key that pays for the
# run, the server's own settings and anything shaped like a secret stay on
# this side of the boundary, and a test file that reads os.environ finds only
# what it needs to start an interpreter.
SUBPROCESS_ENV_KEEP = frozenset({
    "PATH", "HOME", "LANG", "LC_ALL", "LC_CTYPE", "TZ", "TMPDIR", "TEMP", "TMP",
    "USER", "USERNAME", "SHELL",
    # Windows needs these for any process to start and find its libraries.
    "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT",
    "USERPROFILE", "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES",
    "PROGRAMFILES(X86)", "COMMONPROGRAMFILES", "HOMEDRIVE", "HOMEPATH",
    "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE",
    # So the child interpreter reads and writes the same encoding as ours.
    "PYTHONIOENCODING", "PYTHONUTF8", "VIRTUAL_ENV",
})
SECRET_SUFFIXES = ("_KEY", "_TOKEN", "_SECRET", "_PASS", "_PASSWORD")


def scratch_dir(workspace: Path) -> Path:
    """Where an agent may write, which is beside the checkout and never in it.

    This used to live at workspace/.crucible-scratch, inside the tree that
    run_tests is scoped to, and that combination handed back every capability
    the policy takes away. The policy refuses `python -c` because, as its own
    comment says, that flag reaches past every other limit. Writing a .py file
    into scratch and asking the permitted interpreter to run it arrives at the
    same place by a door the guard was not watching: reads outside the
    workspace, writes past the size ceiling, edits to the code under review,
    and a socket.

    A sibling directory keeps scratch writable and keeps it out of every
    execution scope, so run_tests can only run code that was already in the
    checkout. That is the grant the policy docstring describes.
    """
    workspace = Path(workspace).resolve()
    return workspace.parent / (workspace.name + ".crucible-scratch")


def _is_hidden_within(entry: Path, root: Path) -> bool:
    """Whether a path is hidden by its position inside the workspace.

    Judged on the part of the path below the workspace root, so the
    directories the workspace happens to live under stay out of it. A file
    outside the root counts as hidden, since a caller asking about one has
    already left the ground this answer covers.
    """
    try:
        parts = entry.relative_to(root).parts
    except ValueError:
        return True
    # An entry that IS the root has no parts below it, and search reaches here
    # when it is pointed straight at one file. Its own name still decides.
    return any(part.startswith(".") for part in (parts or (entry.name,)))


def subprocess_env(parent: dict | None = None) -> dict:
    """The environment a tool's child process runs under.

    Kept: the allowlist above. Dropped regardless of the allowlist, so a
    future addition cannot let one through by accident: OPENAI_API_KEY, every
    CRUCIBLE_* variable, and anything ending in a secret-shaped suffix.
    """
    import os

    source = os.environ if parent is None else parent
    kept = {}
    for name, value in source.items():
        upper = name.upper()
        if upper == "OPENAI_API_KEY" or upper.startswith("CRUCIBLE_"):
            continue
        if upper.endswith(SECRET_SUFFIXES):
            continue
        if upper in SUBPROCESS_ENV_KEEP:
            kept[name] = value
    return kept

# How much of an argument the ledger keeps. Payloads are cut short so the record
# stays publishable; the arguments a policy decision is made on are kept whole,
# because a call recorded as its own length can never be re-decided by anyone
# checking the run afterwards.
MAX_ARG_CHARS = 300
DECISION_ARGS = frozenset({"path", "command", "url"})
DECISION_ARG_CHARS = 4_000
TEST_TIMEOUT_SECONDS = 120
SEARCH_TIMEOUT_SECONDS = 10
SEARCH_LINE_LIMIT = 240

# A group that is itself quantified and contains a quantifier: (a+)+, (a*)*,
# (.*)+ and friends. This is the shape that makes backtracking exponential in
# the length of the subject, which is why capping the line length does not
# save you. At 240 characters `(a+)+b` would still not finish this century.
#
# A heuristic, and worth being honest that it is one: it recognises the common
# constructions rather than proving anything about the language. The line cap
# and the deadline sit behind it for the cases it misses, and the real fix if
# this ever mattered commercially is a non-backtracking engine.
NESTED_QUANTIFIER = re.compile(r"\([^)]*[*+][^)]*\)\s*[*+]|\([^)]*[*+][^)]*\)\s*\{\d")


@dataclass(frozen=True)
class ToolResult:
    """What comes back from a tool, refusals included.

    A refusal is a result rather than an exception on purpose. The agent is
    supposed to read "that path is outside your scope" and try something else,
    which is behaviour worth observing. Raising would just end the turn and
    teach nobody anything.
    """

    ok: bool
    tool: str
    content: str
    refused: bool = False
    reason: str = ""
    seconds: float = 0.0

    def as_text(self) -> str:
        if self.refused:
            return f"REFUSED BY POLICY: {self.reason}"
        if not self.ok:
            return f"ERROR: {self.reason}"
        return self.content


def _clip(text: str, limit: int = MAX_CHARS) -> str:
    if len(text) <= limit:
        return text
    kept = text[:limit]
    dropped = len(text) - limit
    return f"{kept}\n\n[... {dropped} more characters not shown ...]"


class Toolbox:
    """Tools bound to one policy, one workspace and one run's ledger."""

    def __init__(self, workspace: Path, policy: Policy, ledger: Ledger,
                 *, agent_id: str = "unassigned", tally: dict | None = None):
        self.workspace = Path(workspace).resolve()
        self.policy = policy
        self.ledger = ledger
        self.agent_id = agent_id
        self.scratch = scratch_dir(self.workspace)
        # Shared with every per-agent view, so the run's totals are the run's
        # and not just whatever the unused parent happened to do. Each agent
        # gets its own Toolbox but they are all one run, and a report that said
        # "0 tool calls" after twenty agents read forty files would be a lie
        # told by an accounting detail.
        self._tally = tally if tally is not None else {"calls": 0, "refusals": 0}
        self._tally_lock = threading.Lock()

    def for_agent(self, agent_id: str) -> "Toolbox":
        """A view of the same tools that stamps a different agent on the record."""
        clone = Toolbox(self.workspace, self.policy, self.ledger,
                        agent_id=agent_id, tally=self._tally)
        clone.scratch = self.scratch
        clone._tally_lock = self._tally_lock
        return clone

    def _count(self, key: str) -> None:
        with self._tally_lock:
            self._tally[key] += 1

    @property
    def calls(self) -> int:
        return self._tally["calls"]

    @property
    def refusals(self) -> int:
        return self._tally["refusals"]

    # ------------------------------------------------------------- the door

    def _normalise(self, args: dict) -> dict:
        """Anchor a relative path to the workspace before anyone judges it.

        Left alone, `Path("app.py").resolve()` resolves against whatever
        directory the server process happens to be running in, which is not the
        code under review. An agent writing the obvious thing would be refused
        for a path it never meant, and would spend its steps learning that
        instead of reading code.

        This widens nothing. The policy still decides containment on the
        resolved result, so `../../secrets` anchored to the workspace resolves
        outside it and is refused exactly as before.
        """
        if "path" not in args:
            return args
        raw = str(args["path"]).strip().strip('"')
        candidate = Path(raw)
        if not candidate.is_absolute():
            candidate = self.workspace / candidate
        return {**args, "path": str(candidate)}

    def invoke(self, tool: str, args: dict) -> ToolResult:
        """Check, record, run, record. In that order, with no way around it."""
        args = self._normalise(args or {})
        decision = self.policy.check(tool, args)
        if not decision.allowed:
            self._count("refusals")
            self.ledger.append(
                "tool_denied",
                agent=self.agent_id,
                tool=tool,
                args=self._safe_args(args),
                reason=decision.reason,
            )
            return ToolResult(False, tool, "", refused=True, reason=decision.reason)

        handler = getattr(self, f"_do_{tool}", None)
        if handler is None:
            # The policy permits a tool this class does not implement. That is a
            # configuration mistake rather than an attack, and it is reported as
            # one instead of being silently treated as a refusal.
            reason = f"'{tool}' is permitted by policy but not implemented"
            self.ledger.append("tool_error", agent=self.agent_id, tool=tool, reason=reason)
            return ToolResult(False, tool, "", reason=reason)

        self._count("calls")
        self.ledger.append(
            "tool_call", agent=self.agent_id, tool=tool, args=self._safe_args(args)
        )
        started = time.time()
        try:
            content = handler(args)
            result = ToolResult(True, tool, _clip(content), seconds=time.time() - started)
        except Exception as exc:  # noqa: BLE001 - reported, never swallowed
            result = ToolResult(
                False, tool, "", reason=f"{type(exc).__name__}: {exc}",
                seconds=time.time() - started,
            )
        self.ledger.append(
            "tool_result",
            agent=self.agent_id,
            tool=tool,
            ok=result.ok,
            bytes=len(result.content),
            seconds=round(result.seconds, 3),
            error=result.reason or None,
        )
        return result

    @staticmethod
    def _safe_args(args: dict) -> dict:
        """Arguments as recorded: long payloads become their length.

        The ledger is published with the run. A full file body written into it
        would make the record enormous and would copy whatever the agent was
        handling into a second place.

        The arguments a policy decision turns on are exempt, and that exemption
        is the whole point rather than a convenience. A record exists so someone
        can rebuild the policy and re-decide every call in it; a call whose path
        or command was replaced by its own length cannot be re-decided at all,
        so truncating those quietly puts holes in the record and calls it
        complete. Found by running verify over a real run and watching three
        honest calls come back unverifiable.

        They keep a ceiling of their own, well above any genuine path or
        command, because an argument long enough to reach it is not a path being
        recorded but a payload smuggled into a field that escapes truncation.
        """
        recorded = {}
        for key, value in args.items():
            text = str(value)
            limit = DECISION_ARG_CHARS if key in DECISION_ARGS else MAX_ARG_CHARS
            recorded[key] = text if len(text) <= limit else f"<{len(text)} chars>"
        return recorded

    # -------------------------------------------------------------- tools

    def _do_read_file(self, args: dict) -> str:
        path = Path(args["path"]).resolve()
        if not path.is_file():
            raise FileNotFoundError(f"{path} is not a file")
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
        # Numbered, because a finding without a line number is not actionable
        # and because it lets the verifier check the claim against the source.
        width = len(str(len(lines)))
        return "\n".join(f"{i:>{width}}  {line}" for i, line in enumerate(lines, 1))

    def _do_list_dir(self, args: dict) -> str:
        root = Path(args["path"]).resolve()
        if not root.is_dir():
            raise NotADirectoryError(f"{root} is not a directory")
        rows = []
        for entry in sorted(root.rglob("*")):
            if _is_hidden_within(entry, root):
                continue
            if entry.is_file():
                rows.append(f"{entry.relative_to(root).as_posix()}  ({entry.stat().st_size} bytes)")
        return "\n".join(rows) if rows else "(no files)"

    def _do_search(self, args: dict) -> str:
        """Regex search across the workspace, bounded in every direction.

        The pattern comes from a language model, so it is untrusted input to a
        backtracking engine. Python's `re` offers no timeout, and a nested
        quantifier against a long line takes exponential time, which on a
        shared demo is a denial of service that needs no malice to trigger.

        Three bounds, since none alone is enough. The pattern is length-capped.
        Each line is truncated before matching, which is what actually caps the
        exponent, because backtracking blows up in the length of the subject.
        And a deadline is checked between lines, which catches the accumulation
        of many merely-slow matches that no single check would notice.
        """
        root = Path(args["path"]).resolve()
        pattern = str(args.get("pattern", ""))
        if not pattern:
            raise ValueError("search needs a pattern")
        if len(pattern) > 400:
            raise ValueError("pattern is too long")
        if NESTED_QUANTIFIER.search(pattern):
            raise ValueError(
                "pattern nests one quantifier inside another, which takes "
                "exponential time to fail. Rewrite it without the nesting."
            )
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"bad pattern: {exc}") from exc

        deadline = time.monotonic() + SEARCH_TIMEOUT_SECONDS
        hits: list[str] = []
        targets = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for file in targets:
            if not file.is_file():
                continue
            # Hidden files are skipped by their position INSIDE the workspace.
            # Judging the absolute path instead reads the directories above the
            # workspace too, so a checkout living anywhere dotted, ~/.local, a
            # cache directory, a temp root with a dot in it, matched every file
            # and returned "(no matches)" for every search in the run. Silent,
            # and it looked like a clean tree rather than a broken tool.
            if _is_hidden_within(file, root):
                continue
            try:
                text = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if time.monotonic() > deadline:
                    hits.append(f"[... search stopped after "
                                f"{SEARCH_TIMEOUT_SECONDS}s ...]")
                    return "\n".join(hits) if hits else "(search timed out)"
                if rx.search(line[:SEARCH_LINE_LIMIT]):
                    rel = file.relative_to(root) if root.is_dir() else file.name
                    hits.append(f"{rel}:{number}: {line.strip()[:200]}")
                    if len(hits) >= MAX_MATCHES:
                        hits.append(f"[... stopped at {MAX_MATCHES} matches ...]")
                        return "\n".join(hits)
        return "\n".join(hits) if hits else "(no matches)"

    def _do_write_scratch(self, args: dict) -> str:
        path = Path(args["path"]).resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(args.get("content", "")), encoding="utf-8")
        return f"wrote {len(str(args.get('content', '')))} characters to {path.name}"

    def _do_run_tests(self, args: dict) -> str:
        """Run a permitted command with no shell between here and the process.

        shell=False is the reason the metacharacter rule in the policy is a
        second line rather than the only one: there is no shell to interpret
        them, and the policy refuses them anyway.
        """
        import shlex

        raw = args["command"]
        parts = raw if isinstance(raw, list) else shlex.split(raw, posix=False)
        parts = [p.strip('"') for p in parts]
        cwd = Path(args.get("path", self.workspace)).resolve()
        try:
            proc = subprocess.run(
                parts, cwd=cwd, capture_output=True, text=True,
                timeout=TEST_TIMEOUT_SECONDS, shell=False,
                env=subprocess_env(), stdin=subprocess.DEVNULL,
            )
        except subprocess.TimeoutExpired:
            return f"TIMEOUT after {TEST_TIMEOUT_SECONDS}s: {' '.join(parts)}"
        tail = (proc.stdout + proc.stderr).strip()
        return f"exit={proc.returncode}\n{_clip(tail, 8000)}"


# The description the model sees. Kept beside the implementations so a tool
# cannot be added without the agent being told it exists, and worded so the
# limits are known up front rather than discovered by being refused.
TOOL_MANUAL = """\
read_file(path)              Read a file with line numbers.
list_dir(path)               List every file under a directory.
search(path, pattern)        Regex search across .py files. Returns file:line: text.
write_scratch(path, content) Write a note. Only inside .crucible-scratch/.
run_tests(path, command)     Run the project's own tests. Permitted: python, pytest, node, npm.

Every call is checked against a policy before it runs, and both the call and
its result are written to a tamper-evident ledger. Paths outside the workspace,
commands not on the list, and any network access are refused. A refusal is not
a failure on your part; read the reason and take a different route.
"""
