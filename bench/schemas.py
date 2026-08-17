"""The constraint under test, one schema per seat in the run.

What llama.cpp does with these is the point. The schema is compiled to a
grammar and consulted during sampling, so at every token the sampler masks out
anything that would take the output outside the language. A malformed tool call
is therefore not rejected after the fact, it is never generated. That is a
different mechanism from asking a model nicely and validating afterwards, and
whether it actually helps a 9B model do agentic work is the question.

Delivered as JSON Schema through response_format rather than as hand-authored
GBNF. Two reasons, and the second is the honest one. Schema is what a
practitioner would actually reach for, so the result transfers. And this
build's GBNF parser rejected the canonical negated character class from
llama.cpp's own json.gbnf, which cost an hour and would have cost more, whereas
the schema converter is the same code path the project tests itself. The
mechanism being measured is identical either way: constrained sampling.

Each seat gets its own schema because the answer shapes genuinely differ. A
single permissive union covering all four would let a verifier reply in the
hunter's shape, which is one of the failures worth preventing rather than one
worth allowing.
"""

from __future__ import annotations


def _obj(props: dict, required: list[str]) -> dict:
    return {
        "type": "object",
        "properties": props,
        "required": required,
        "additionalProperties": False,
    }


_STR = {"type": "string"}
_LEVEL = {"enum": ["high", "medium", "low"]}


def _tool(name: str, args: dict, required: list[str]) -> dict:
    """One tool call, spelled exactly as the manual documents it.

    The tool name is a const rather than an enum shared across the union, which
    is what ties each name to its own argument object. Without that the model
    could emit read_file carrying a pattern, which is well-formed and useless.
    """
    return _obj(
        {"tool": {"const": name}, "args": _obj(args, required)},
        ["tool", "args"],
    )


# The five tools, each with only the arguments it actually takes.
TOOL_CALLS = [
    _tool("read_file", {"path": _STR}, ["path"]),
    _tool("list_dir", {"path": _STR}, ["path"]),
    _tool("search", {"path": _STR, "pattern": _STR}, ["path", "pattern"]),
    _tool("write_scratch", {"path": _STR, "content": _STR}, ["path", "content"]),
    _tool("run_tests", {"path": _STR, "command": _STR}, ["path", "command"]),
]

FINDING = _obj(
    {
        "title": _STR,
        "file": _STR,
        "line": {"type": "integer"},
        "severity": _LEVEL,
        "summary": _STR,
        "failure_scenario": _STR,
        "evidence": _STR,
    },
    ["title", "file", "line", "severity", "summary", "failure_scenario", "evidence"],
)

HUNTER_DONE = _obj(
    {"done": {"const": True}, "findings": {"type": "array", "items": FINDING}},
    ["done", "findings"],
)

VERIFIER_DONE = _obj(
    {
        "done": {"const": True},
        "refuted": {"type": "boolean"},
        "confidence": _LEVEL,
        "concrete_failure": _STR,
        "reasoning": _STR,
    },
    ["done", "refuted", "confidence", "concrete_failure", "reasoning"],
)

PROBER_DONE = _obj(
    {
        "done": {"const": True},
        "got_through": {"type": "array", "items": _STR},
        "notes": _STR,
    },
    ["done", "got_through", "notes"],
)

# The planner answers once and calls no tools, so its schema admits only the
# answer. Bounded at six lanes because the orchestrator takes the first six
# anyway, and a model allowed to produce twenty would spend its allowance
# writing lanes nobody reads.
PLANNER = _obj(
    {
        "lanes": {
            "type": "array",
            "minItems": 1,
            "maxItems": 6,
            "items": _obj(
                {"name": _STR, "brief": _STR,
                 "files": {"type": "array", "items": _STR}},
                ["name", "brief", "files"],
            ),
        }
    },
    ["lanes"],
)

HUNTER = {"oneOf": TOOL_CALLS + [HUNTER_DONE]}
VERIFIER = {"oneOf": TOOL_CALLS + [VERIFIER_DONE]}
PROBER = {"oneOf": TOOL_CALLS + [PROBER_DONE]}

ROLES = {
    "planner": PLANNER,
    "hunter": HUNTER,
    "verifier": VERIFIER,
    "prober": PROBER,
}

# Which seat a system prompt belongs to. The provider sees the prompt and
# nothing else, so the role is recognised from a phrase unique to each one.
# The tool manual is pasted into three of the four, so the marker has to come
# from the wording around it rather than from the manual itself.
_SIGNATURES = (
    ("You divide a code review", "planner"),
    ("You are hunting for real defects", "hunter"),
    ("You are trying to REFUTE", "verifier"),
    ("Your lane is the boundary itself", "prober"),
)


def role_for(system: str) -> str:
    for marker, role in _SIGNATURES:
        if marker in system:
            return role
    return "unknown"


def schema_for(system: str):
    """The schema for whichever seat this prompt belongs to.

    None for a prompt that matches nothing, which leaves that call
    unconstrained rather than forcing it into the wrong shape. A silent
    mismatch would look exactly like the constraint failing to help, so the
    harness counts unknown roles and says so rather than letting them pass.
    """
    return ROLES.get(role_for(system))
