"""Checks for the archive: what is kept, what is swept, and what survives a crash.

Plain script, no test framework, so it runs anywhere Python does:
    python tests/test_archive.py

The archive exists so a visitor sees a real finished run in five seconds rather
than two minutes, which means the failures worth testing are the quiet ones. A
half-written file that takes a page load down with it. A retention sweep that
follows a symlink out of its own directory. Concurrent saves where one wins and
the other vanishes. And the misses: a run's score against a planted answer key
has to name the defects that got away, because publishing those is the point.

No network and no money. Reports are built by hand in the shape the
orchestrator produces, so the checks here are about storage rather than about
the run that filled it.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crucible.archive import (  # noqa: E402
    Archive, compact_replay, latest, list_runs, refuted_claims, save_run,
    score_against_key,
)
from crucible.orchestrator import Report  # noqa: E402

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


# ---------------------------------------------------------------- fixtures

def finding(finding_id="f1", file="bookings/api.py", line=51, severity="high",
            survived=True):
    return {
        "id": finding_id, "lane": "money", "title": f"defect {finding_id}",
        "file": file, "line": line, "severity": severity,
        "summary": "a concrete wrong behaviour",
        "failure_scenario": "given 25 rows and page_size 10, has_next is False",
        "evidence": "total_pages = count // page_size",
        "verdicts": [
            {"refuted": not survived, "confidence": "high",
             "reasoning": "checked the code", "concrete_failure": "25 rows"},
        ] * 3,
        "survived": survived, "settled": True, "duplicates": 0,
    }


def report(run_id="a1b2c3d4", raised=3, survived=1, findings=None,
           task="Review this codebase for correctness defects."):
    return Report(
        run_id=run_id, task=task, lanes=["money", "concurrency", "auth"],
        raised=raised, survived=survived,
        findings=[finding()] if findings is None else findings,
        spend_usd=0.4231, calls=34, tool_calls=61, refusals=2,
        ledger_head="ab" * 32, seconds=118.4, halted="",
    )


def stream():
    """An event list shaped like a real run's, traffic included."""
    return [
        {"kind": "run_started", "run_id": "a1b2c3d4", "task": "t",
         "policy": {"tools": {"read_file": {}}}, "ceiling": 0.6},
        {"kind": "phase", "phase": "plan"},
        {"kind": "lane", "name": "money", "brief": "rounding and totals"},
        {"kind": "phase", "phase": "hunt", "lanes": 3},
        {"kind": "agent_started", "agent": "hunter-1", "role": "hunter",
         "lane": "money"},
        {"kind": "agent_thought", "agent": "hunter-1", "step": 0,
         "model": "gpt-5-mini", "cost": 0.0004, "seconds": 3.1},
        {"kind": "tool", "agent": "hunter-1", "tool": "read_file",
         "args": {"path": "bookings/api.py"}, "refused": False, "reason": "",
         "bytes": 4096},
        {"kind": "tool", "agent": "hunter-1", "tool": "read_file",
         "args": {"path": "C:/Windows/win.ini"}, "refused": True,
         "reason": "the path resolves outside the workspace", "bytes": 0},
        {"kind": "finding_raised", "id": "f1", "lane": "money",
         "title": "defect f1", "file": "bookings/api.py", "line": 51,
         "severity": "high", "summary": "floor division loses the last page"},
        {"kind": "finding_raised", "id": "f2", "lane": "money",
         "title": "imagined defect", "file": "bookings/money.py", "line": 12,
         "severity": "low", "summary": "a claim that will not survive"},
        {"kind": "agent_done", "agent": "hunter-1", "step": 3},
        {"kind": "agent_finished", "agent": "hunter-1", "found": 2},
        {"kind": "phase", "phase": "verify", "findings": 2},
        {"kind": "verdict", "finding": "f1", "agent": "verifier-f1-1",
         "refuted": False, "confidence": "high", "reasoning": "reproduced it"},
        {"kind": "verdict", "finding": "f2", "agent": "verifier-f2-1",
         "refuted": True, "confidence": "high", "reasoning": "cannot happen"},
        {"kind": "finding_settled", "id": "f1", "survived": True,
         "refuted_by": 1, "survived_by": 2},
        {"kind": "finding_settled", "id": "f2", "survived": False,
         "refuted_by": 3, "survived_by": 0},
        {"kind": "run_finished", "run_id": "a1b2c3d4", "raised": 2,
         "survived": 1, "findings": [finding()], "spend_usd": 0.4231},
    ]


def ticking_clock(start=None):
    """A clock that advances one minute per call, safely from several threads.

    A bare generator raises if two threads enter it at once, which is exactly
    what the concurrency check does.
    """
    base = start or datetime(2026, 8, 17, 9, 0, tzinfo=timezone.utc)
    state = {"n": 0}
    lock = threading.Lock()

    def now():
        with lock:
            state["n"] += 1
            return base + timedelta(minutes=state["n"])

    return now


def answer_key(workspace: Path) -> Path:
    """A small workspace carrying a known answer key."""
    workspace.mkdir(parents=True, exist_ok=True)
    (workspace / ".answer_key.json").write_text(json.dumps({"defects": [
        {"id": "D1", "file": "bookings/api.py", "line": 51,
         "difficulty": "easy", "summary": "floor division drops the last page"},
        {"id": "D2", "file": "bookings/auth.py", "line": 43,
         "difficulty": "hard", "summary": "a level of 0 is falsy"},
        {"id": "D3", "file": "bookings/service.py", "line": 146,
         "difficulty": "medium", "summary": "no rollback on a billing failure"},
    ]}), encoding="utf-8")
    return workspace


# ------------------------------------------------------------------ checks

def replay_checks() -> None:
    section("the replay keeps the shape and drops the traffic")
    events = stream()
    replay, dropped = compact_replay(events)
    kinds = [e["kind"] for e in replay]

    check("a successful tool call is not stored",
          not any(e["kind"] == "tool" and not e.get("refused") for e in replay))
    check("a refusal is stored, since it is the policy proving itself",
          any(e["kind"] == "tool" and e.get("refused") for e in replay))
    check("model thinking is not stored", "agent_thought" not in kinds)
    check("the tree can still be redrawn",
          all(k in kinds for k in ("run_started", "phase", "lane",
                                   "agent_started", "finding_raised",
                                   "verdict", "finding_settled",
                                   "run_finished")))
    check("the order the run happened in is preserved",
          kinds.index("finding_raised") < kinds.index("verdict")
          < kinds.index("finding_settled"))
    check("the closing event drops its copy of the findings",
          all("findings" not in e for e in replay if e["kind"] == "run_finished"))
    check("nothing was dropped from an ordinary run", dropped == 0)
    check("the stored events speak the live stream's vocabulary",
          all(isinstance(e.get("kind"), str) for e in replay))

    section("the replay is capped")
    noise = [{"kind": "verdict", "finding": f"f{i}", "refuted": True,
              "confidence": "low", "reasoning": "no"} for i in range(400)]
    significant = len(compact_replay(events + noise)[0])
    capped, dropped = compact_replay(events + noise, cap=50)
    check("a run with thousands of events stores at most the cap",
          len(capped) == 50, str(len(capped)))
    check("and reports how many it dropped rather than hiding the trim",
          dropped == significant - 50, f"{dropped} of {significant}")
    check("the events that draw the tree are the ones kept",
          all(k in [e["kind"] for e in capped]
              for k in ("run_started", "lane", "finding_raised", "run_finished")))
    check("a field longer than the ceiling is trimmed",
          len(compact_replay([{"kind": "halted", "reason": "x" * 5000}])[0][0]
              ["reason"]) < 1200)

    section("the claims that died are rebuilt from the stream")
    dead = refuted_claims(events, {"findings": [finding()]})
    check("a refuted claim is kept", len(dead) == 1 and dead[0]["id"] == "f2")
    check("with the verdicts that killed it",
          dead[0]["refuted_by"] == 3 and len(dead[0]["verdicts"]) == 1)
    check("a surviving claim is not listed as refuted",
          all(claim["id"] != "f1" for claim in dead))

    merged = refuted_claims(
        events + [{"kind": "finding_merged", "into": "f1", "dropped": "f2",
                   "title": "imagined defect"}],
        {"findings": [finding()]},
    )
    check("a claim merged into another is not reported as destroyed",
          merged == [])


def round_trip_checks(tmp: Path) -> None:
    section("a saved run comes back")
    home = tmp / "round-trip"
    archive = Archive(home, clock=ticking_clock())
    path = archive.save(report(), stream())

    check("the file is named for the run", path.name == "a1b2c3d4.json")
    check("and sits in the archive directory", path.parent == home)

    stored = archive.latest()
    check("latest returns the run that was just saved",
          stored is not None and stored["run_id"] == "a1b2c3d4")
    check("the full report is kept unedited",
          stored["report"]["ledger_head"] == "ab" * 32
          and stored["report"]["task"].startswith("Review this codebase"))
    check("the findings keep their verdicts",
          len(stored["findings"][0]["verdicts"]) == 3)
    check("the lane names are kept",
          stored["lanes"] == ["money", "concurrency", "auth"])
    check("the spend is kept", abs(stored["spend_usd"] - 0.4231) < 1e-9)
    check("the timing is kept", abs(stored["seconds"] - 118.4) < 1e-9)
    check("the ledger head is kept", stored["ledger_head"] == "ab" * 32)
    check("the replay is kept", len(stored["replay"]) > 5)
    check("the record says which shape it is written in", stored["schema"] == 1)
    check("and when it was saved",
          stored["saved_at"].startswith("2026-08-17T09:01"))

    check("an empty archive returns None rather than raising",
          Archive(tmp / "nothing-here").latest() is None)
    check("and lists nothing", Archive(tmp / "nothing-here").list_runs(10) == [])

    fetched = archive.get("a1b2c3d4")
    check("a run can be fetched by identifier",
          fetched is not None and fetched["run_id"] == "a1b2c3d4")
    check("an identifier that climbs out of the archive is refused",
          archive.get("../../.env") is None)
    check("an unknown identifier is None rather than an error",
          archive.get("deadbeef") is None)

    section("the module level API writes to the same place")
    home2 = tmp / "module-api"
    save_run(report(run_id="mod00001"), stream(), directory=home2)
    check("save_run writes a run", latest(directory=home2)["run_id"] == "mod00001")
    check("list_runs sees it", len(list_runs(10, directory=home2)) == 1)

    section("a report given as a plain dictionary is accepted")
    from dataclasses import asdict
    home3 = tmp / "dict-report"
    Archive(home3).save(asdict(report(run_id="dict0001")), stream())
    check("the server's published event shape round-trips",
          Archive(home3).latest()["run_id"] == "dict0001")

    refused = False
    try:
        Archive(home3).save(report(run_id="../escape"), [])
    except ValueError:
        refused = True
    check("a run identifier that is not a safe filename is refused", refused)


def ordering_checks(tmp: Path) -> None:
    section("list_runs orders newest first")
    home = tmp / "ordering"
    archive = Archive(home, clock=ticking_clock())
    for n in range(5):
        archive.save(report(run_id=f"run{n}", raised=n, survived=n), stream())

    rows = archive.list_runs(10)
    check("every run is listed", len(rows) == 5)
    check("newest first", [r["run_id"] for r in rows]
          == ["run4", "run3", "run2", "run1", "run0"],
          str([r["run_id"] for r in rows]))
    check("latest agrees with the head of the list",
          archive.latest()["run_id"] == rows[0]["run_id"])
    check("timestamps descend",
          [r["timestamp"] for r in rows] == sorted(
              [r["timestamp"] for r in rows], reverse=True))

    row = rows[0]
    check("a summary carries the run identifier", row["run_id"] == "run4")
    check("and the timestamp", bool(row["timestamp"]))
    check("and what was raised and what survived",
          row["raised"] == 4 and row["survived"] == 4)
    check("and the spend", abs(row["spend_usd"] - 0.4231) < 1e-9)
    check("and the task", row["task"].startswith("Review this codebase"))
    check("a summary does not carry the whole replay", "replay" not in row)
    check("limit is honoured", len(archive.list_runs(2)) == 2)


def retention_checks(tmp: Path) -> None:
    section("retention keeps the newest and deletes the rest")
    home = tmp / "retention"
    archive = Archive(home, keep=3, clock=ticking_clock())
    for n in range(8):
        archive.save(report(run_id=f"keep{n}"), stream())

    remaining = sorted(p.name for p in home.glob("*.json"))
    check("only the retention limit is left on disk",
          len(remaining) == 3, str(remaining))
    check("and they are the newest three",
          remaining == ["keep5.json", "keep6.json", "keep7.json"], str(remaining))
    check("the oldest is gone", not (home / "keep0.json").exists())
    check("the listing agrees with the disk",
          [r["run_id"] for r in archive.list_runs(10)]
          == ["keep7", "keep6", "keep5"])

    section("a sweep does not raise when a file has already gone")
    check("deleting a file that is not there counts as done",
          archive._delete(home / "vanished.json") is True)
    check("and nothing was noted about it",
          not any("vanished" in note for note in archive.notes()))

    outside = tmp / "retention-outside.json"
    outside.write_text('{"run_id": "outsider"}', encoding="utf-8")
    check("a path outside the archive directory is left alone",
          archive._delete(outside) is False and outside.exists())

    section("the sweep does not follow a symlink out of the archive")
    treasure = tmp / "treasure.json"
    treasure.write_text('{"run_id": "not yours"}', encoding="utf-8")
    linked = home / "sneaky.json"
    try:
        linked.symlink_to(treasure)
        made_link = True
    except (OSError, NotImplementedError):
        # Windows wants a privilege for this. Nothing to check if the platform
        # refuses to build the attack.
        made_link = False
    if made_link:
        check("a symlink is not listed as an archived run",
              all(r["run_id"] != "not yours" for r in archive.list_runs(10)))
        archive.prune(1)
        check("and the file it points at survives the sweep", treasure.exists())
        check("and the link itself is left where it is", linked.is_symlink())
    else:
        print("  ..  symlink checks skipped, this platform refused to make one")

    section("only real files directly in the archive are ever touched")
    # The same guard the symlink checks above cover, reachable without the
    # privilege Windows wants for making one: the scan filters on being a real
    # file, so anything else in the directory is invisible to both the listing
    # and the sweep.
    guarded = tmp / "guarded"
    guard = Archive(guarded, keep=1, clock=ticking_clock())
    guard.save(report(run_id="realrun1"), stream())
    guard.save(report(run_id="realrun2"), stream())
    impostor = guarded / "adirectory.json"
    impostor.mkdir()
    (impostor / "inside.txt").write_text("still here", encoding="utf-8")
    guard.prune()
    check("a directory wearing a run's name is not listed as a run",
          [r["run_id"] for r in guard.list_runs(10)] == ["realrun2"])
    check("the sweep still removed the surplus run",
          not (guarded / "realrun1.json").exists())
    check("and it did not reach inside the directory",
          (impostor / "inside.txt").exists())

    section("retention never empties the archive")
    small = Archive(tmp / "retention-min", keep=0, clock=ticking_clock())
    small.save(report(run_id="only1"), stream())
    small.save(report(run_id="only2"), stream())
    check("a keep of zero is read as one rather than as delete everything",
          small.latest() is not None)


def corruption_checks(tmp: Path) -> None:
    section("a corrupt file is skipped rather than fatal")
    home = tmp / "corruption"
    archive = Archive(home, clock=ticking_clock())
    archive.save(report(run_id="healthy1"), stream())

    # Every way a stored run can be unreadable, all at once.
    (home / "truncated.json").write_text('{"run_id": "half", "replay": [{"ki',
                                         encoding="utf-8")
    (home / "empty.json").write_text("", encoding="utf-8")
    (home / "notarun.json").write_text("[1, 2, 3]", encoding="utf-8")
    (home / "binary.json").write_bytes(b"\xff\xfe\x00\x01\x02rubbish")

    rows = archive.list_runs(10)
    check("the readable run still lists",
          [r["run_id"] for r in rows] == ["healthy1"], str(rows))
    check("latest still answers", archive.latest()["run_id"] == "healthy1")
    check("every unreadable file was noted",
          len(archive.notes()) >= 4, str(archive.notes()))
    check("the note names the file and says why",
          any("truncated.json" in note and "JSON" in note
              for note in archive.notes()))
    check("a file that is valid JSON but not a run is named too",
          any("notarun.json" in note for note in archive.notes()))
    check("binary rubbish under a json name does not escape as an exception",
          any("binary.json" in note for note in archive.notes()))
    noted_before = len(archive.notes())
    archive.list_runs(10)
    check("a file is only noted once, however often it is read",
          len(archive.notes()) == noted_before)

    check("a corrupt archive can still be added to",
          archive.save(report(run_id="healthy2"), stream()).exists())
    check("and the new run is the one served",
          archive.latest()["run_id"] == "healthy2")

    section("corruption is swept before healthy runs are")
    swept = Archive(home, keep=2, clock=ticking_clock())
    swept.prune()
    left = sorted(p.name for p in home.glob("*.json"))
    check("the sweep removed the unreadable files first",
          left == ["healthy1.json", "healthy2.json"], str(left))


def atomicity_checks(tmp: Path) -> None:
    section("an interrupted write leaves nothing behind")
    home = tmp / "atomic"
    archive = Archive(home, clock=ticking_clock())
    archive.save(report(run_id="survivor"), stream())

    def boom(source, target):
        raise OSError("the disk went away mid-move")

    original = os.replace
    os.replace = boom
    raised = False
    try:
        archive.save(report(run_id="doomed01"), stream())
    except OSError:
        raised = True
    finally:
        os.replace = original

    check("a write that fails says so rather than reporting success", raised)
    check("no partial file was left under the run's name",
          not (home / "doomed01.json").exists())
    check("and the fragment was cleaned up",
          [p.name for p in home.iterdir() if ".part-" in p.name] == [],
          str([p.name for p in home.iterdir()]))
    check("the run saved before it is untouched",
          archive.latest()["run_id"] == "survivor")

    section("a fragment left by a crash is invisible to readers")
    # What a process killed between the write and the move actually leaves.
    (home / "halfway.json.part-deadbeef").write_text(
        '{"run_id": "halfway", "replay": [{"kin', encoding="utf-8")
    check("the fragment is not mistaken for a run",
          [r["run_id"] for r in archive.list_runs(10)] == ["survivor"])
    check("and it is not noted, because it was never a run to skip",
          not any("halfway" in note for note in archive.notes()))
    archive.prune(1)
    check("the sweep leaves fragments alone rather than guessing at them",
          (home / "halfway.json.part-deadbeef").exists())

    section("the file on disk is always complete json")
    stored = json.loads((home / "survivor.json").read_text(encoding="utf-8"))
    check("a saved run parses on its own", stored["run_id"] == "survivor")


def scoring_checks(tmp: Path) -> None:
    section("scoring against a known answer key")
    workspace = answer_key(tmp / "scored-workspace")
    # D1 hit exactly, D2 hit eight lines away which is inside the window, D3
    # missed by forty lines, plus a claim about a file with no planted defect.
    found_report = report(run_id="scored01", raised=4, survived=4, findings=[
        finding("s1", "bookings/api.py", 51),
        finding("s2", "D:/work/demo/bookings/auth.py", 51),
        finding("s3", "bookings/service.py", 106),
        finding("s4", "bookings/models.py", 12),
    ])
    score = score_against_key(found_report, workspace)

    check("the planted total is reported", score["planted"] == 3)
    check("an exact line match counts as found",
          "D1" in [d["id"] for d in score["found"]])
    check("a match inside the window counts as found, path spelling and all",
          "D2" in [d["id"] for d in score["found"]])
    check("a defect forty lines away is a miss",
          [d["id"] for d in score["missed"]] == ["D3"],
          str(score["missed"]))
    check("the miss carries its difficulty",
          score["missed"][0]["difficulty"] == "medium")
    check("and enough to name it",
          score["missed"][0]["file"] == "bookings/service.py"
          and score["missed"][0]["line"] == 146)
    check("difficulty is broken out rather than aggregated",
          score["by_difficulty"]["easy"] == {"found": 1, "planted": 1}
          and score["by_difficulty"]["hard"] == {"found": 1, "planted": 1}
          and score["by_difficulty"]["medium"] == {"found": 0, "planted": 1})
    check("recall is reported", abs(score["recall"] - 0.667) < 0.001)

    empty = score_against_key(report(findings=[]), workspace)
    check("a run that found nothing misses everything",
          len(empty["missed"]) == 3 and empty["found"] == [])
    check("no answer key scores as None rather than as zero",
          score_against_key(found_report, tmp / "unscored-workspace") is None)

    section("the archive records the misses")
    home = tmp / "scored"
    archive = Archive(home, clock=ticking_clock())
    archive.save(found_report, stream(), workspace=workspace)
    stored = archive.latest()
    check("a scored run stores what it missed",
          [d["id"] for d in stored["score"]["missed"]] == ["D3"])
    check("and what it found",
          [d["id"] for d in stored["score"]["found"]] == ["D1", "D2"])
    check("the summary carries the miss count",
          archive.list_runs(1)[0]["missed"] == 1)

    archive.save(report(run_id="unscored"), stream())
    check("a run saved without a workspace scores as None",
          archive.latest()["score"] is None)

    section("the real demo target still scores")
    demo = Path(__file__).resolve().parent.parent / "demo_target"
    if (demo / ".answer_key.json").is_file():
        real = score_against_key(
            report(findings=[finding("r1", "bookings/api.py", 51),
                             finding("r2", "bookings/auth.py", 43)]),
            demo,
        )
        check("the shipped answer key parses and scores",
              real is not None and real["planted"] == 9)
        check("the two defects aimed at are the two found",
              sorted(d["id"] for d in real["found"]) == ["D2", "D8"],
              str([d["id"] for d in real["found"]]))
        check("and the other seven are published as misses",
              len(real["missed"]) == 7)
    else:
        print("  ..  demo target answer key missing, skipped")


def concurrency_checks(tmp: Path) -> None:
    section("concurrent saves all land")
    home = tmp / "concurrent"
    archive = Archive(home, keep=50, clock=ticking_clock())
    errors: list[str] = []
    barrier = threading.Barrier(12)

    def save(n: int) -> None:
        try:
            barrier.wait(timeout=10)
            archive.save(report(run_id=f"thread{n:03d}"), stream())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=save, args=(n,)) for n in range(12)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    check("no save raised", errors == [], str(errors[:3]))
    check("every run reached the disk",
          len(list(home.glob("*.json"))) == 12,
          str(sorted(p.name for p in home.glob("*.json"))))
    check("and every one of them reads back",
          len(archive.list_runs(50)) == 12)
    check("no fragments were left behind",
          [p.name for p in home.iterdir() if ".part-" in p.name] == [])
    check("nothing was skipped as unreadable", archive.notes() == [])

    section("saves racing a retention sweep stay within the limit")
    tight = tmp / "concurrent-tight"
    archive = Archive(tight, keep=4, clock=ticking_clock())
    errors.clear()
    barrier = threading.Barrier(10)

    def save_tight(n: int) -> None:
        try:
            barrier.wait(timeout=10)
            archive.save(report(run_id=f"tight{n:03d}"), stream())
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{type(exc).__name__}: {exc}")

    threads = [threading.Thread(target=save_tight, args=(n,)) for n in range(10)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    check("no save raised while the sweep ran alongside it",
          errors == [], str(errors[:3]))
    check("the archive settled at its retention limit",
          len(list(tight.glob("*.json"))) == 4,
          str(sorted(p.name for p in tight.glob("*.json"))))
    check("and everything left is readable",
          len(archive.list_runs(50)) == 4)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        replay_checks()
        round_trip_checks(tmp)
        ordering_checks(tmp)
        retention_checks(tmp)
        corruption_checks(tmp)
        atomicity_checks(tmp)
        scoring_checks(tmp)
        concurrency_checks(tmp)
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
