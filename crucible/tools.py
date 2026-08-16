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

import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .ledger import Ledger
from .policy import Policy

MAX_CHARS = 24_000
MAX_MATCHES = 60
TEST_TIMEOUT_SECONDS = 120


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
        self.scratch = self.workspace / ".crucible-scratch"
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
        """Arguments as recorded: long content becomes its length.

        The ledger is published with the run. A full file body written into it
        would make the record enormous and would copy whatever the agent was
        handling into a second place.
        """
        recorded = {}
        for key, value in args.items():
            text = str(value)
            recorded[key] = text if len(text) <= 300 else f"<{len(text)} chars>"
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
            if any(part.startswith(".") for part in entry.relative_to(root).parts):
                continue
            if entry.is_file():
                rows.append(f"{entry.relative_to(root).as_posix()}  ({entry.stat().st_size} bytes)")
        return "\n".join(rows) if rows else "(no files)"

    def _do_search(self, args: dict) -> str:
        """Literal or regex search across the workspace, with line numbers."""
        import re

        root = Path(args["path"]).resolve()
        pattern = str(args.get("pattern", ""))
        if not pattern:
            raise ValueError("search needs a pattern")
        try:
            rx = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"bad pattern: {exc}") from exc

        hits: list[str] = []
        targets = [root] if root.is_file() else sorted(root.rglob("*.py"))
        for file in targets:
            if not file.is_file():
                continue
            if any(part.startswith(".") for part in file.parts):
                continue
            try:
                text = file.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for number, line in enumerate(text.splitlines(), 1):
                if rx.search(line):
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
