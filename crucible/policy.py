"""Authority for an agent, written down.

An agent's power is usually whatever its tools happen to allow, discovered by
reading the tool implementations and hoping. Here it is a document: a policy
names the tools that exist, the paths they may touch, and the commands they may
run, and every call is checked against it before the tool sees the arguments.

Two things follow from putting it here rather than inside each tool. A refusal
carries a reason a person can read, which is what the arena puts on screen. And
the policy is one object you can print, diff and hand to someone who wants to
know what the thing was allowed to do, without reading any code.

The check is deny-by-default at every level: an unlisted tool is refused, a
listed tool with no rule for an argument the rule set covers is refused, and
anything that raises while being checked is refused rather than allowed.
"""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass, field
from pathlib import Path

# Characters that hand the rest of a command line to a shell. The runner never
# uses shell=True, so these cannot chain a second command today. They are still
# refused, because the allowlist below is meaningless if a permitted binary can
# carry an unpermitted one in its arguments.
SHELL_METACHARACTERS = re.compile(r"[;&|`$><\n\r]|\$\(|\)\s*\{")

# Binaries that will run whatever you hand them.
INTERPRETERS = {"python", "python2", "python3", "py", "node", "nodejs", "deno", "bun"}

# The flags that turn a permitted interpreter into arbitrary code execution.
# This is the hole an allowlist of binary names leaves wide open: `python -c`
# needs no shell metacharacter, so the metacharacter guard never fires, and it
# grants every capability the rest of the policy just refused. Reading a file
# outside the workspace, writing past the size ceiling, editing the code under
# review and opening a socket are all one argument away.
CODE_EXECUTION_FLAGS = {
    "-c", "--command", "-e", "--eval", "--eval-file", "-i", "--interactive",
    "-p", "--print", "-r", "--require", "--input-type", "--experimental-eval",
    "-x", "--exec",
}

# `python -m X` runs module X, so the module needs an allowlist of its own.
PERMITTED_MODULES = {"pytest", "unittest", "nose2", "green"}


def _as_list(value) -> list:
    """A list from whatever was recorded, refusing to iterate a bare string."""
    if value is None:
        return []
    if isinstance(value, (str, bytes)):
        return [value]
    try:
        return list(value)
    except TypeError:
        return [value]


@dataclass(frozen=True)
class Decision:
    """The answer, and the sentence shown to whoever is watching."""

    allowed: bool
    reason: str
    tool: str = ""

    def __bool__(self) -> bool:
        return self.allowed


@dataclass(frozen=True)
class ToolRule:
    """What one tool may do.

    path_scopes  directories the tool may touch, inclusive of subdirectories.
                 An empty list means the tool takes no path argument.
    commands     permitted executable basenames, for the command runner only.
    url_hosts    permitted hostnames, for the fetch tool only.
    max_bytes    ceiling on a write, so a permitted tool cannot fill a disk.
    """

    path_scopes: list[Path] = field(default_factory=list)
    commands: list[str] = field(default_factory=list)
    url_hosts: list[str] = field(default_factory=list)
    max_bytes: int = 1_000_000


class Policy:
    """A named set of tool rules, and the check every call passes through."""

    def __init__(self, name: str, rules: dict[str, ToolRule], *, description: str = ""):
        self.name = name
        self.description = description
        self.rules = rules

    # ---------------------------------------------------------------- checks

    def check(self, tool: str, args: dict) -> Decision:
        """Deny by default, and deny on any error while deciding.

        A check that throws is a check that did not pass. Wrapping the whole
        body means a malformed argument produces a refusal with a reason rather
        than an exception that some caller upstream might swallow into an
        allow.
        """
        try:
            rule = self.rules.get(tool)
            if rule is None:
                return Decision(
                    False,
                    f"tool '{tool}' is not in policy '{self.name}'",
                    tool,
                )
            if "path" in args:
                verdict = self._check_path(tool, rule, str(args["path"]))
                if not verdict:
                    return verdict
            if "command" in args:
                verdict = self._check_command(tool, rule, args["command"])
                if not verdict:
                    return verdict
            if "url" in args:
                verdict = self._check_url(tool, rule, str(args["url"]))
                if not verdict:
                    return verdict
            if "content" in args:
                size = len(str(args["content"]).encode("utf-8"))
                if size > rule.max_bytes:
                    return Decision(
                        False,
                        f"write of {size} bytes exceeds the {rule.max_bytes} byte "
                        f"ceiling on '{tool}'",
                        tool,
                    )
            return Decision(True, "permitted by policy", tool)
        except Exception as exc:  # noqa: BLE001 - a failed check is a refusal
            return Decision(False, f"refused while checking '{tool}': {exc}", tool)

    def _check_path(self, tool: str, rule: ToolRule, raw: str) -> Decision:
        """Containment decided on the resolved path, never on the text.

        resolve() follows symlinks and flattens '..', so a path that reads as
        innocent and lands outside the scope is caught here. Doing this on the
        string instead is the classic hole: 'work/../../etc/passwd' passes a
        startswith check and reads a different file than it appears to.
        """
        if not rule.path_scopes:
            return Decision(False, f"'{tool}' takes no path argument", tool)
        target = Path(raw).expanduser().resolve()
        for scope in rule.path_scopes:
            root = scope.resolve()
            if target == root or root in target.parents:
                return Decision(True, "within scope", tool)
        scopes = ", ".join(str(s) for s in rule.path_scopes)
        return Decision(
            False,
            f"path '{target}' sits outside the scope of '{tool}' ({scopes})",
            tool,
        )

    def _check_command(self, tool: str, rule: ToolRule, raw) -> Decision:
        if not rule.commands:
            return Decision(False, f"'{tool}' runs no commands", tool)
        line = raw if isinstance(raw, str) else " ".join(str(p) for p in raw)
        if SHELL_METACHARACTERS.search(line):
            return Decision(
                False,
                f"command carries shell metacharacters, which could hide a "
                f"second command inside a permitted one: {line!r}",
                tool,
            )
        parts = shlex.split(line, posix=False)
        if not parts:
            return Decision(False, "empty command", tool)
        # Compared on the basename, lowercased, with any extension dropped, so
        # an absolute path to a permitted binary is still permitted and
        # 'PYTHON.EXE' does not slip past a rule written as 'python'.
        binary = Path(parts[0].strip('"')).name.lower()
        binary = binary.rsplit(".", 1)[0] if "." in binary else binary
        if binary not in [c.lower() for c in rule.commands]:
            allowed = ", ".join(rule.commands)
            return Decision(
                False,
                f"'{binary}' is not a permitted command for '{tool}' ({allowed})",
                tool,
            )

        # Everything past argv[0] is checked too, which is the whole point.
        # A binary allowlist is not a behaviour allowlist: every interpreter on
        # any sensible list will run caller-supplied code if asked in the right
        # way, and then path containment, the write ceiling and the no-network
        # rule are all decoration.
        args = [p.strip('"') for p in parts[1:]]
        for token in args:
            if token.lower() in CODE_EXECUTION_FLAGS:
                return Decision(
                    False,
                    f"'{token}' hands code to '{binary}' to execute, which would "
                    f"reach past every other limit in this policy",
                    tool,
                )

        if binary in INTERPRETERS:
            # An interpreter may run a test module or a file inside the
            # workspace. Nothing else, because nothing else is what this tool
            # is for.
            if not args:
                return Decision(False, f"'{binary}' with no arguments opens a "
                                       f"shell, which is refused", tool)
            if args[0] == "-m":
                module = args[1].lower() if len(args) > 1 else ""
                if module not in PERMITTED_MODULES:
                    permitted = ", ".join(sorted(PERMITTED_MODULES))
                    return Decision(
                        False,
                        f"'{binary} -m {module or '(nothing)'}' is refused; "
                        f"permitted modules are {permitted}",
                        tool,
                    )
            elif args[0].startswith("-"):
                return Decision(
                    False,
                    f"'{binary} {args[0]}' is refused; run a test module with "
                    f"-m or a file inside the workspace",
                    tool,
                )
            else:
                verdict = self._check_path(tool, rule, args[0])
                if not verdict:
                    return Decision(
                        False,
                        f"'{binary}' may only run a file inside the workspace: "
                        f"{verdict.reason}",
                        tool,
                    )

        # Any remaining argument that names an existing file has to be inside
        # the workspace too, so a permitted runner cannot be pointed at a
        # config or test file somewhere else on the disk.
        for token in args:
            if token.startswith("-") or not token:
                continue
            try:
                candidate = Path(token)
            except (ValueError, OSError):
                continue
            if candidate.is_absolute() and not self._check_path(tool, rule, token):
                return Decision(
                    False,
                    f"'{token}' sits outside the workspace, so '{binary}' may "
                    f"not be pointed at it",
                    tool,
                )
        return Decision(True, "permitted command", tool)

    def _check_url(self, tool: str, rule: ToolRule, raw: str) -> Decision:
        if not rule.url_hosts:
            return Decision(False, f"'{tool}' makes no outbound requests", tool)
        from urllib.parse import urlparse

        parsed = urlparse(raw)
        if parsed.scheme not in ("http", "https"):
            return Decision(False, f"scheme '{parsed.scheme}' is refused", tool)
        host = (parsed.hostname or "").lower()
        for permitted in rule.url_hosts:
            permitted = permitted.lower()
            if host == permitted or host.endswith("." + permitted):
                return Decision(True, "permitted host", tool)
        return Decision(
            False,
            f"host '{host}' is not in the allowlist for '{tool}'",
            tool,
        )

    # ---------------------------------------------------------------- display

    @classmethod
    def from_dict(cls, data: dict) -> "Policy":
        """Rebuild a policy from what a run recorded about itself.

        This is what makes a ledger checkable by someone who was not there. The
        run writes the policy it ran under into its own first entry, so a
        reader can reconstruct that policy, replay every recorded call through
        it, and confirm that the allow and deny decisions in the file are the
        decisions this policy actually produces. Without it the record says
        "the boundary refused these" and the only thing backing that up is the
        word of the process that wrote it.
        """
        rules: dict[str, ToolRule] = {}
        for tool, rule in (data.get("tools") or {}).items():
            if not isinstance(rule, dict):
                continue
            # A bare string where a list belongs iterates per character, so
            # "D:/work" would rebuild as seven single-letter scopes and the
            # rule would permit nothing while looking populated.
            rules[str(tool)] = ToolRule(
                # str() first: a scope recorded as a number raises TypeError
                # out of Path(), and from_dict is called while verifying a file
                # that by definition may be malformed.
                path_scopes=[Path(str(p))
                             for p in _as_list(rule.get("path_scopes"))],
                commands=_as_list(rule.get("commands")),
                url_hosts=_as_list(rule.get("url_hosts")),
                # `or` treats a recorded 0 as absent and hands back the 1 MB
                # default, turning a rule that permitted no bytes at all into
                # one that permits a megabyte. Only a genuinely missing value
                # may fall back.
                max_bytes=int(rule["max_bytes"])
                if rule.get("max_bytes") is not None else 1_000_000,
            )
        return cls(
            str(data.get("name", "")), rules,
            description=str(data.get("description", "")),
        )

    def as_dict(self) -> dict:
        """The policy as data, for the run log and for the page that shows it."""
        return {
            "name": self.name,
            "description": self.description,
            "tools": {
                tool: {
                    "path_scopes": [str(p) for p in rule.path_scopes],
                    "commands": rule.commands,
                    "url_hosts": rule.url_hosts,
                    "max_bytes": rule.max_bytes,
                }
                for tool, rule in sorted(self.rules.items())
            },
        }


def review_policy(workspace: Path) -> Policy:
    """The authority a defect-hunting agent gets.

    It reads the checkout and writes only inside a scratch directory, so a
    finding is a claim about code rather than an edit to it. Tests run, since a
    reproduction that was never executed is an opinion. Nothing reaches the
    network, which is what makes an exfiltration attempt fail on the boundary
    rather than on the model's good judgement.
    """
    workspace = workspace.resolve()
    return Policy(
        "review",
        {
            "read_file": ToolRule(path_scopes=[workspace]),
            "list_dir": ToolRule(path_scopes=[workspace]),
            "search": ToolRule(path_scopes=[workspace]),
            "write_scratch": ToolRule(
                path_scopes=[workspace / ".crucible-scratch"], max_bytes=200_000
            ),
            "run_tests": ToolRule(
                path_scopes=[workspace], commands=["python", "pytest", "node", "npm"]
            ),
        },
        description=(
            "Read the checkout, run its tests, write only to scratch. "
            "No network, no edits to the code under review."
        ),
    )
