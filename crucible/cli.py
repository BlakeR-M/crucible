"""Crucible from a terminal, and from a pipeline.

The web interface is the demonstration. This is the tool. It is built for the
case where nobody is watching: a check that runs before something ships, blocks
the pipeline when a finding survives being attacked, and leaves behind a record
that someone can verify later without trusting the process that wrote it.

    crucible init                     write a starter crucible.toml
    crucible run .                    review a codebase
    crucible models                   show the configured seats and reach them
    crucible verify runs/abc123.jsonl check a run's record independently

Exit codes, because the point of a gate is that something downstream reads it:

    0  the run completed and nothing survived verification
    1  a finding survived every verifier
    2  the run could not complete, or the configuration is wrong

The difference between 0 and 2 is the one that matters. A tool that reports
success when it failed to run is worse than no tool, because a pipeline cannot
tell the difference between "checked and clean" and "never checked".
"""

from __future__ import annotations

import argparse
import json
import sys
import uuid
from dataclasses import asdict
from pathlib import Path

from .archive import score_against_key
from .config import CONFIG_NAME, STARTER, Config, ConfigError, find_config, load
from .ledger import Ledger
from .orchestrator import Orchestrator
from .policy import Policy, review_policy
from .providers import Budget, OpenAIProvider, Tier
from .tools import DECISION_ARGS

ROOT = Path(__file__).resolve().parent.parent

EXIT_CLEAN = 0
EXIT_SURVIVED = 1
EXIT_FAILED = 2


# --------------------------------------------------------------------- output

def _rule(title: str = "") -> None:
    print(f"\n{title}\n{'-' * 62}" if title else "-" * 62)


def _reporter(quiet: bool):
    """Live lines while a run happens, or silence in a pipeline."""

    def show(event: dict) -> None:
        if quiet:
            return
        kind = event.get("kind")
        if kind == "lane":
            print(f"  lane      {event['name']}")
        elif kind == "agent_started":
            target = event.get("lane") or event.get("target", "")
            print(f"  start     {event['agent']}  {target}")
        elif kind == "tool" and event.get("refused"):
            print(f"  REFUSED   {event['agent']}  {event.get('reason', '')[:80]}")
        elif kind == "finding_raised":
            print(f"  raised    {event['id']}  {event['title'][:66]}")
        elif kind == "finding_settled":
            mark = "SURVIVED" if event["survived"] else "refuted "
            kept = event["survived_by"]
            print(f"  {mark}  {event['id']}  {kept}/"
                  f"{kept + event['refuted_by']} verifiers failed to refute it")
        elif kind == "phase":
            _rule(f"== {event['phase']} ==")
        elif kind in ("agent_halted", "agent_error", "run_failed"):
            print(f"  !!        {event.get('reason', '')[:110]}")

    return show


def _credibility(report) -> str:
    """Why this run cannot be believed, or an empty string if it can.

    A review that found nothing and a review that never happened both produce
    zero findings, an empty `halted` and a calm report. Anything that reads an
    exit code has to be able to tell them apart, so every way a run can be
    hollow is named here and every one of them fails the run.

    The boundary is in this list on purpose. A run whose agents got past the
    policy did complete, and its findings may even be sound, but the conditions
    it ran under were not the ones the record claims. That is a failed run.
    """
    if report.halted:
        return report.halted
    if not report.lanes:
        return "the planner produced no lanes, so nothing was reviewed"
    if report.probe and not report.probe.get("held"):
        return (f"the enforced boundary did not hold: "
                f"{report.probe.get('note', 'see the record')}")
    if report.agent_failures:
        parts = []
        if report.agents_silent:
            parts.append(f"{report.agents_silent} returned nothing at all")
        if report.agents_exhausted:
            parts.append(f"{report.agents_exhausted} ran out of steps")
        return (f"{report.agent_failures} of {report.agents_run} agents did "
                f"not finish ({'; '.join(parts)}), so this review is incomplete")
    return ""


def _print_report(report, config: Config, ledger_path: Path) -> None:
    _rule()
    print(f"  {report.survived} of {report.raised} findings survived "
          f"adversarial verification")
    if report.agent_failures:
        print(f"  {report.agent_failures} of {report.agents_run} agents failed "
              f"to answer")
    print(f"  ${report.spend_usd:.4f} over {report.calls} model calls, "
          f"{report.tool_calls} tool calls, {report.refusals} refused")
    print(f"  {report.seconds}s   ledger {ledger_path}")
    print(f"  head {report.ledger_head[:32]}")
    if report.probe:
        held = report.probe.get("held")
        print(f"  boundary  {'held' if held else 'NOT ESTABLISHED'}: "
              f"{report.probe.get('note', '')[:80]}")
    if report.halted:
        print(f"  halted:   {report.halted}")
    _rule()

    for finding in report.findings:
        print(f"\n  [{finding['severity']}] {finding['title']}")
        print(f"    {finding['file']}:{finding['line']}")
        print(f"    {finding['summary']}")
        if finding.get("failure_scenario"):
            print(f"    -> {finding['failure_scenario'][:220]}")
    if not report.findings:
        # Only claim the verifiers did this when they actually ran. Saying
        # "refuted by two of three verifiers" after a collapsed verification
        # phase is the report asserting work that never happened, which is
        # worse than saying nothing.
        if _credibility(report):
            print("\n  Nothing is being reported, and this run cannot support "
                  "the claim that there was nothing to report.")
        elif report.raised:
            print("\n  Nothing survived. Every finding raised was refuted by "
                  "at least two of its three verifiers.")
        else:
            print("\n  No findings were raised.")


def _print_score(report, workspace: Path) -> None:
    score = score_against_key(report, workspace)
    if score is None:
        print("\n  No answer key in this workspace, so nothing to score.")
        return
    print(f"\n  scored against {score['planted']} planted defects")
    for difficulty in ("easy", "medium", "hard"):
        row = score["by_difficulty"].get(difficulty)
        if row:
            print(f"    {difficulty:7} {row['found']}/{row['planted']}")
    print(f"    {'total':7} {len(score['found'])}/{score['planted']}")
    for defect in score["missed"]:
        print(f"    missed  {defect['id']}  {defect['file']}:{defect['line']}  "
              f"{defect['summary'][:60]}")


# ------------------------------------------------------------------- commands

def cmd_init(args) -> int:
    directory = Path(args.path).resolve()
    if not directory.is_dir():
        print(f"no such directory: {directory}", file=sys.stderr)
        return EXIT_FAILED
    target = directory / CONFIG_NAME
    if target.exists() and not args.force:
        print(f"{target} already exists. Pass --force to overwrite it.")
        return EXIT_FAILED
    target.write_text(STARTER, encoding="utf-8")
    print(f"wrote {target}")
    print("Edit [provider] and [models], then run: crucible run .")
    return EXIT_CLEAN


def _config_for(args) -> Config:
    explicit = Path(args.config).resolve() if args.config else None
    if explicit and not explicit.is_file():
        raise ConfigError(f"no config file at {explicit}")
    path = explicit or find_config(Path(getattr(args, "path", ".")).resolve())
    config = load(path)
    # Flags win over the file, so a one-off never needs an edit and a commit.
    if getattr(args, "task", None):
        config.task = args.task
    if getattr(args, "ceiling", None) is not None:
        config.ceiling_usd = args.ceiling
    if getattr(args, "fail_on", None):
        config.fail_on = args.fail_on
    return config


def cmd_models(args) -> int:
    import urllib.error
    import urllib.request

    config = _config_for(args)
    print(f"  config    {config.source}")
    print(f"  endpoint  {config.base_url}")
    print(f"  billing   {'metered' if config.metered else 'unmetered (free)'}")
    print(f"  key       ${config.api_key_env} "
          f"{'is set' if config.api_key else 'is EMPTY'}")
    for tier in Tier:
        print(f"  {tier.value:9} {config.models[tier]}")

    # Reaching the endpoint is the half of the configuration that a file cannot
    # tell you about, and the half that fails at three in the morning.
    url = f"{config.base_url}/models"
    request = urllib.request.Request(
        url, headers={"Authorization": f"Bearer {config.api_key or 'none'}"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"\n  endpoint unreachable: {exc}")
        return EXIT_FAILED

    served = {str(m.get("id")) for m in (payload.get("data") or [])
              if isinstance(m, dict)}
    print(f"\n  endpoint answered, serving {len(served)} model(s)")
    missing = sorted({config.models[t] for t in Tier} - served) if served else []
    for name in missing:
        # A warning rather than a failure: hosted endpoints do not always list
        # every model an account can call, so absence here is not proof.
        print(f"  note: '{name}' is not in the endpoint's list")
    if not config.metered:
        print("  billing is off, so the budget ceiling will not stop this run")
    return EXIT_CLEAN


def cmd_run(args) -> int:
    workspace = Path(args.path).resolve()
    if not workspace.is_dir():
        print(f"no such directory: {workspace}", file=sys.stderr)
        return EXIT_FAILED

    config = _config_for(args)
    provider = OpenAIProvider(
        config.require_key(), config.models,
        base_url=config.base_url, metered=config.metered,
    )

    # Under the working directory rather than beside the source. Installed with
    # pip, ROOT is inside site-packages, which is somewhere a tool has no
    # business writing and may not be able to.
    runs = (Path(args.ledger_dir).resolve() if args.ledger_dir
            else Path.cwd() / "runs")
    try:
        runs.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        print(f"cannot write run records to {runs}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    # Unique from the first byte. A fixed staging name means two runs sharing a
    # directory write the same file, and the first to finish renames the
    # second's half-written record out from under it.
    ledger_path = runs / f".pending-{uuid.uuid4().hex[:12]}.jsonl"

    if not args.quiet:
        print(f"  workspace {workspace}")
        print(f"  config    {config.source}")
        print(f"  provider  {config.describe()}")
        print(f"  ceiling   ${config.ceiling_usd:.2f}")

    budget = Budget(ceiling_usd=config.ceiling_usd,
                    unmetered=not config.metered)
    orchestrator = Orchestrator(
        provider, workspace, review_policy(workspace), Ledger(ledger_path),
        budget, emit=_reporter(args.quiet), max_workers=config.max_workers,
    )
    report = orchestrator.run(config.task)

    # Named after the run now that there is a run id, so a directory of these
    # is navigable and two runs never write over each other.
    final = runs / f"{report.run_id}.jsonl"
    ledger_path.replace(final)

    if args.json:
        print(json.dumps(asdict(report), indent=2))
    else:
        _print_report(report, config, final)
        if args.score:
            _print_score(report, workspace)

    # Credibility before verdict. A run that could not complete has no verdict
    # to give, and returning its empty finding list as a pass is the one
    # failure this tool must never have.
    reason = _credibility(report)
    if reason:
        print(f"\n  RUN NOT CREDIBLE: {reason}", file=sys.stderr)
        if config.require_complete:
            return EXIT_FAILED
        # Opted out in the config, out loud. The verdict below is still
        # reported, and it is worth as much as the run behind it.
        print("  [gate] require_complete is off, so this is being reported "
              "anyway", file=sys.stderr)
    if config.fail_on == "survived" and report.survived:
        return EXIT_SURVIVED
    return EXIT_CLEAN


def cmd_verify(args) -> int:
    """Check a run's record without trusting whoever produced it.

    Two questions, and the second is the one nobody else answers. Is the chain
    intact, which the ledger can decide by itself. And do the authority
    decisions in the file reproduce: rebuild the policy the run recorded in its
    own first entry, replay every call it recorded, and confirm that what the
    file says was allowed is what that policy allows.

    A file can be internally consistent and still be a fabrication. Replaying
    the decisions is what closes that gap, and it only closes it as far as the
    policy is trustworthy. By default the policy comes from the file being
    checked, which means a forger who rewrote the whole record could also have
    written down a permissive policy that allows everything they recorded. That
    is stated in the output rather than glossed, and --workspace supplies the
    policy independently, which is what makes the check adversarial rather than
    a consistency test.
    """
    path = Path(args.ledger).resolve()
    if not path.is_file():
        print(f"no such ledger: {path}", file=sys.stderr)
        return EXIT_FAILED

    try:
        ledger = Ledger(path)
        entries = ledger.entries()
    except (ValueError, KeyError, TypeError, OSError,
            UnicodeDecodeError) as exc:
        # A record this tool cannot parse is a record it cannot vouch for, and
        # that is a verdict rather than a crash. Left to escape, the traceback
        # exits 1, which the contract reserves for a finding that survived.
        print(f"{path} is not a readable ledger: "
              f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED
    if not entries:
        print(f"{path} is empty", file=sys.stderr)
        return EXIT_FAILED
    # "null" and "[1,2,3]" are valid JSON lines and not ledger entries. Left
    # alone they reach entry["event"] and leave as a TypeError, which exits 1
    # and reads to a pipeline as a finding that survived.
    stray = next((i for i, e in enumerate(entries) if not isinstance(e, dict)), None)
    if stray is not None:
        print(f"{path}: line {stray + 1} is not a ledger entry",
              file=sys.stderr)
        return EXIT_FAILED

    print(f"  ledger    {path}")
    print(f"  entries   {len(entries)}")

    broken = ledger.verify()
    if broken is not None:
        print(f"\n  CHAIN BROKEN at entry {broken.seq}: {broken.reason}")
        return EXIT_FAILED
    print(f"  chain     intact, head {entries[-1]['hash'][:32]}")

    started = next((e for e in entries if e["event"] == "run_started"), None)
    if started is None:
        print("\n  no run_started entry, so the policy cannot be rebuilt")
        return EXIT_FAILED

    if args.workspace:
        # Built here, from the workspace, owing nothing to the file. This is
        # the only configuration in which the replay is adversarial: the record
        # can no longer choose the rules it will be judged by.
        workspace = Path(args.workspace).resolve()
        if not workspace.is_dir():
            print(f"no such workspace: {workspace}", file=sys.stderr)
            return EXIT_FAILED
        recorded = str(started["payload"].get("workspace") or "")
        if recorded and Path(recorded).resolve() != workspace:
            print(f"\n  the run recorded its workspace as {recorded}, and you "
                  f"named {workspace}. A policy built from a different "
                  f"directory scopes different paths, so the replay would "
                  f"compare decisions nobody made.", file=sys.stderr)
            return EXIT_FAILED
        policy = review_policy(workspace)
        trust = "independent, rebuilt from the workspace you named"
    else:
        policy = Policy.from_dict(started["payload"].get("policy") or {})
        trust = ("SELF-REPORTED by the file, so this replay shows the record "
                 "is internally consistent rather than that the rules were "
                 "real. Pass --workspace to check it independently.")
    print(f"  policy    '{policy.name}' with "
          f"{len(policy.rules)} permitted tool(s)")
    print(f"  source    {trust}")

    allowed = denied = mismatched = redacted = unverifiable = 0
    problems: list[str] = []
    for entry in entries:
        event = entry["event"]
        if event not in ("tool_call", "tool_denied"):
            continue
        payload = entry["payload"]
        args_recorded = payload.get("args") or {}
        # Long values are stored as "<N chars>" so the ledger stays publishable
        # and does not become a second copy of whatever the agent handled. The
        # placeholder is not the argument, so replaying it answers a different
        # question from the one the run answered.
        #
        # Which argument was truncated decides what to do about it. The policy
        # rules on paths, commands and hosts; everything else, `content` above
        # all, only affects a size limit. Skipping the whole call whenever
        # anything was truncated let a forger hide a call from the replay
        # entirely by making one field long, which defeats --workspace. So a
        # truncated decision-bearing argument makes the call unverifiable and
        # fails the record, and a truncated payload is noted and replayed.
        truncated = {k for k, v in args_recorded.items()
                     if str(v).startswith("<") and str(v).endswith("chars>")}
        blocking = truncated & DECISION_ARGS
        if blocking:
            unverifiable += 1
            problems.append(
                f"    entry {entry['seq']}: {', '.join(sorted(blocking))} was "
                f"truncated in the record, so this call cannot be replayed"
            )
            continue
        if truncated:
            redacted += 1
        decision = policy.check(str(payload.get("tool", "")), dict(args_recorded))
        expected_allowed = event == "tool_call"
        if decision.allowed != expected_allowed:
            mismatched += 1
            problems.append(
                f"    entry {entry['seq']}: recorded as "
                f"{'allowed' if expected_allowed else 'denied'}, "
                f"policy says {'allowed' if decision.allowed else 'denied'} "
                f"({decision.reason[:70]})"
            )
        elif expected_allowed:
            allowed += 1
        else:
            denied += 1

    print(f"  replayed  {allowed + denied + mismatched} tool decisions: "
          f"{allowed} allowed, {denied} refused")
    if redacted:
        print(f"  note      {redacted} call(s) carried a truncated payload, so "
              f"their size limits were not rechecked")

    if mismatched or unverifiable:
        if mismatched:
            print(f"\n  {mismatched} DECISION(S) DO NOT REPRODUCE:")
        if unverifiable:
            print(f"\n  {unverifiable} CALL(S) COULD NOT BE REPLAYED:")
        for line in problems[:12]:
            print(line)
        return EXIT_FAILED

    finished = next((e for e in reversed(entries)
                     if e["event"] == "run_finished"), None)
    if finished is None:
        print("\n  the chain is intact but carries no run_finished entry, so "
              "this run was cut short rather than completed")
        return EXIT_FAILED

    payload = finished["payload"]
    raised = payload.get("raised", 0)
    survived = payload.get("survived", 0)
    halted = str(payload.get("halted") or "")
    failures = payload.get("agent_failures") or 0
    print(f"\n  run       {raised} raised, {survived} survived, "
          f"${payload.get('spend_usd', 0):.4f}")

    # The record can be perfect and describe a run that went badly. Verifying
    # the file says nothing about the run unless the run's own outcome is read
    # too, and a green verdict over a halted review is exactly the false
    # reassurance this command exists to prevent.
    problems = []
    if halted:
        problems.append(f"the run halted: {halted}")
    if failures:
        problems.append(f"{failures} agent(s) never returned an answer")
    if payload.get("probe_held") is False:
        problems.append("the enforced boundary did not hold")
    if survived:
        problems.append(f"{survived} finding(s) survived verification")

    print("  verdict   record is intact and every decision in it reproduces")
    if problems:
        print("\n  but the run it describes did not come back clean:")
        for problem in problems:
            print(f"    {problem}")
        # Same order of precedence cmd_run uses. A record showing the boundary
        # gave way describes a run whose conditions were not the ones claimed,
        # which is a failed run rather than a review with a finding in it, and
        # the two commands must not disagree about the same file.
        blocked = halted or failures or payload.get("probe_held") is False
        return EXIT_FAILED if blocked else EXIT_SURVIVED
    return EXIT_CLEAN


# ----------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="crucible",
        description="Adversarial code review with an enforced boundary and a "
                    "verifiable record.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    run = subparsers.add_parser("run", help="review a codebase")
    run.add_argument("path", nargs="?", default=".")
    run.add_argument("--config", help=f"path to {CONFIG_NAME}")
    run.add_argument("--task", help="override the review task")
    run.add_argument("--ceiling", type=float, help="spend ceiling in USD")
    run.add_argument("--fail-on", choices=["survived", "never"],
                     dest="fail_on", help="what makes this exit non-zero")
    run.add_argument("--ledger-dir", help="where to write the run record")
    run.add_argument("--quiet", action="store_true", help="report only")
    run.add_argument("--json", action="store_true", help="report as JSON")
    run.add_argument("--score", action="store_true",
                     help="score against the workspace's answer key if it has one")
    run.set_defaults(func=cmd_run)

    init = subparsers.add_parser("init", help=f"write a starter {CONFIG_NAME}")
    init.add_argument("path", nargs="?", default=".")
    init.add_argument("--force", action="store_true")
    init.set_defaults(func=cmd_init)

    models = subparsers.add_parser("models",
                                   help="show the configured seats and reach them")
    models.add_argument("path", nargs="?", default=".")
    models.add_argument("--config")
    models.set_defaults(func=cmd_models)

    verify = subparsers.add_parser(
        "verify", help="check a run's record and replay its decisions")
    verify.add_argument("ledger")
    verify.add_argument("--workspace",
                        help="rebuild the policy from this directory instead of "
                             "trusting the one recorded in the file")
    verify.set_defaults(func=cmd_verify)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except ConfigError as exc:
        print(f"configuration: {exc}", file=sys.stderr)
        return EXIT_FAILED
    except KeyboardInterrupt:
        print("\ninterrupted", file=sys.stderr)
        return EXIT_FAILED
    except Exception as exc:  # noqa: BLE001
        # Anything unforeseen still has to leave through the documented door.
        # Left to escape, the interpreter exits 1, which this contract reserves
        # for a finding that survived verification, and a pipeline would read a
        # crash as a review result. Failing closed on 2 is the only safe
        # direction, and the traceback goes to stderr so nothing is hidden.
        import traceback

        traceback.print_exc()
        print(f"\ncrucible failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return EXIT_FAILED


if __name__ == "__main__":
    sys.exit(main())
