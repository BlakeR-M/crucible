"""Run the arena from a terminal.

The web interface is the demonstration; this is how you drive the same engine
without one. Useful for a headless run, for scoring against the demo target's
answer key, and for anyone who would rather read the code than trust the page.

    python -m crucible.cli
    python -m crucible.cli --task "Review for money handling defects" --score
    python -m crucible.cli --workspace path/to/repo --ceiling 1.50
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .archive import score_against_key
from .ledger import Ledger
from .orchestrator import Orchestrator
from .policy import review_policy
from .providers import Budget, provider_from_env

ROOT = Path(__file__).resolve().parent.parent


def print_score(report, workspace: Path) -> None:
    """Show a run against the demo target's answer key.

    The scoring itself lives in archive.py and is shared rather than copied, so
    a run scored on its way into the archive and the same run scored here
    cannot disagree about what it caught. This function is only the rendering.

    Recall is printed per difficulty because an aggregate number hides the
    thing worth knowing, which is whether the easy defects were found and the
    hard ones missed or the other way round.
    """
    score = score_against_key(report, workspace)
    if score is None:
        print("\nNo answer key in this workspace, so nothing to score against.")
        return

    print(f"\n  scored against {score['planted']} planted defects")
    for difficulty in ("easy", "medium", "hard"):
        row = score["by_difficulty"].get(difficulty)
        if row:
            print(f"    {difficulty:7} {row['found']}/{row['planted']}")
    print(f"    {'total':7} {len(score['found'])}/{score['planted']}")
    if score["missed"]:
        print("\n  missed:")
        for defect in score["missed"]:
            print(f"    {defect['id']}  {defect['file']}:{defect['line']}  "
                  f"{defect['summary'][:70]}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Crucible arena.")
    parser.add_argument("--task", default="Review this codebase for correctness "
                                          "defects. Report only real bugs.")
    parser.add_argument("--workspace", default=str(ROOT / "demo_target"))
    parser.add_argument("--ceiling", type=float, default=1.00,
                        help="spend ceiling for this run, in US dollars")
    parser.add_argument("--env-file", default=str(ROOT / ".env"))
    parser.add_argument("--quiet", action="store_true",
                        help="only the report, no live event lines")
    parser.add_argument("--score", action="store_true",
                        help="score against the workspace's answer key if it has one")
    parser.add_argument("--json", action="store_true", help="report as JSON")
    args = parser.parse_args()

    workspace = Path(args.workspace).resolve()
    if not workspace.is_dir():
        sys.exit(f"no such workspace: {workspace}")

    def show(event: dict) -> None:
        if args.quiet:
            return
        kind = event.get("kind")
        if kind == "lane":
            print(f"  lane      {event['name']}")
        elif kind == "agent_started":
            print(f"  start     {event['agent']}  {event.get('lane') or event.get('target', '')}")
        elif kind == "tool" and event.get("refused"):
            print(f"  REFUSED   {event['agent']}  {event.get('reason', '')[:90]}")
        elif kind == "finding_raised":
            print(f"  raised    {event['id']}  {event['title'][:70]}")
        elif kind == "finding_settled":
            mark = "SURVIVED" if event["survived"] else "refuted "
            print(f"  {mark}  {event['id']}  "
                  f"{event['survived_by']}/{event['survived_by'] + event['refuted_by']} kept")
        elif kind == "phase":
            print(f"\n== {event['phase']} ==")
        elif kind in ("agent_halted", "agent_error", "run_failed"):
            print(f"  !! {event.get('reason', '')[:120]}")

    ledger_path = ROOT / "runs" / "cli.jsonl"
    ledger_path.unlink(missing_ok=True)
    budget = Budget(ceiling_usd=args.ceiling)
    orchestrator = Orchestrator(
        provider_from_env(extra_env=Path(args.env_file)),
        workspace, review_policy(workspace), Ledger(ledger_path), budget,
        emit=show,
    )
    report = orchestrator.run(args.task)

    if args.json:
        from dataclasses import asdict
        print(json.dumps(asdict(report), indent=2))
        return 0

    print(f"\n{'=' * 62}")
    print(f"  {report.survived} of {report.raised} findings survived "
          f"adversarial verification")
    print(f"  ${report.spend_usd:.4f} over {report.calls} model calls, "
          f"{report.tool_calls} tool calls, {report.refusals} refused")
    print(f"  {report.seconds}s   ledger {report.ledger_head[:16]}")
    if report.halted:
        print(f"  halted: {report.halted}")
    print(f"{'=' * 62}")

    for finding in report.findings:
        print(f"\n  [{finding['severity']}] {finding['title']}")
        print(f"    {finding['file']}:{finding['line']}")
        print(f"    {finding['summary']}")
        if finding.get("failure_scenario"):
            print(f"    -> {finding['failure_scenario'][:200]}")

    if args.score:
        print_score(report, workspace)
    return 0


if __name__ == "__main__":
    sys.exit(main())
