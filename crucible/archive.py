"""Finished runs, kept, so the arena can be understood before it is trusted.

A live run is the proof and it takes two minutes. That is a fair price for
someone who has already decided to care and much too high for someone deciding
whether to. So a completed run is written down and the landing page shows a
real one immediately: the lanes that were drawn, the claims that were raised,
the ones the verifiers destroyed, what it cost, and the ledger head that lets a
reader check the record for themselves. Running it live afterwards then answers
a better question than "what is this", which is "is that archived run real".

What is kept, and why each part is here.

The report exactly as the run produced it, unedited, because an archive that
paraphrases is a screenshot with extra steps. The replay, which is the event
stream with the tool traffic removed: enough to redraw the agent tree and the
order things happened in, without the hundreds of read_file lines that make a
stored run large and tell a reader nothing. Refused tool calls stay in, since a
refusal is the policy layer proving itself and is the opposite of noise. The
replay speaks the same event vocabulary as the live stream on purpose, so one
renderer draws a finished run and a running one and an archived run cannot
drift into looking like a different product.

The refuted claims are rebuilt and kept alongside the surviving ones. A report
carries survivors only, which is right for a report and wrong here: the
refutations are the product, and a page that says three of twelve survived
needs the other nine to be more than a number.

And the misses. A run scored against the demo target's answer key records the
planted defects it failed to find, by identifier and difficulty, next to the
ones it caught. Publishing that is the same move as publishing the kills: a
review system that shows only its successes is asking to be taken on trust,
which is the thing this project exists to refuse.

Two failure modes shape the mechanics. A process can die mid-write, so a record
goes to a temporary name and is moved into place in one step, and a reader that
meets a malformed file skips it with a note rather than failing a page load.
And runs finish on worker threads while the web layer reads, so the write and
the retention sweep that follows it are one serialised step, while reads take
no lock at all: a visitor loading the landing page should never wait behind a
run saving itself, and every read here is already written to survive a file
vanishing underneath it.
"""

from __future__ import annotations

import json
import os
import re
import threading
import uuid
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import NamedTuple

ROOT = Path(__file__).resolve().parent.parent
ARCHIVE_DIR = ROOT / "runs" / "archive"

# Bumped when the shape of a record changes in a way a reader cannot infer.
# Stored runs outlive the code that wrote them, and a page that guesses at an
# older shape renders something nobody ever measured.
SCHEMA_VERSION = 1

RETENTION_DEFAULT = 30

# Enough to redraw a large run and nowhere near enough to store its traffic. A
# six lane run with a dozen findings settles near 150 events once the tool
# calls are out, so the cap is loose in ordinary use and only bites on
# something pathological.
REPLAY_CAP = 600

# No single field in a replay event needs more than this. The orchestrator
# already truncates the long ones, so this is a backstop against an event kind
# added later that does not.
MAX_FIELD_CHARS = 800

# How far a reported line may sit from a planted one and still count as the
# same defect. Deliberately generous: a defect reported at the call site rather
# than at the declaration is still that defect found.
MATCH_WINDOW_LINES = 12

# Deliberately not ".json". A process that dies between the write and the move
# leaves a file the readers here never look at, rather than a truncated run
# they would have to detect and explain.
TEMP_MARKER = ".part-"

# A run identifier becomes a filename, so it is checked rather than trusted.
# The orchestrator writes twelve hex characters; anything that could climb out
# of the archive directory is refused before it reaches the filesystem.
_RUN_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


# ------------------------------------------------------------------- replay

# The events that carry the shape of a run. Losing one of these loses a branch
# of the tree, so they are kept ahead of everything else when the cap bites.
ESSENTIAL_KINDS = frozenset({
    "run_started", "run_finished", "run_failed", "phase", "phase_failed",
    "lane", "finding_raised", "finding_settled", "halted",
})

# Worth keeping, and droppable. These fill the tree in rather than draw it.
SECONDARY_KINDS = frozenset({
    "agent_started", "agent_done", "agent_finished", "agent_error",
    "agent_halted", "agent_exhausted", "verdict", "finding_merged",
    "deduped", "worker_failed",
})


def _trim(value, limit: int = MAX_FIELD_CHARS):
    if isinstance(value, str) and len(value) > limit:
        return value[:limit] + " ..."
    return value


def _slim(event) -> dict | None:
    """One stored event, or None for traffic.

    A hunter reading eleven files is the largest part of a run by volume and
    the smallest part of it by meaning. A refusal is the reverse: one line that
    is the entire argument for the policy layer, so refused calls survive the
    filter while successful ones do not.
    """
    if not isinstance(event, dict):
        return None
    kind = event.get("kind")
    if kind == "tool":
        if not event.get("refused"):
            return None
    elif kind not in ESSENTIAL_KINDS and kind not in SECONDARY_KINDS:
        return None

    slim = {}
    for key, value in event.items():
        # The closing event repeats every surviving finding, which the record
        # already stores in full. Two copies of a dozen findings is most of the
        # file for no added meaning.
        if kind == "run_finished" and key == "findings":
            continue
        slim[key] = _trim(value)
    return slim


def compact_replay(events, cap: int = REPLAY_CAP) -> tuple[list[dict], int]:
    """The stream without its traffic, in the order it happened.

    Returns the kept events and the number the cap discarded, because a replay
    that has been trimmed should say so rather than quietly present itself as
    complete. Traffic filtered out above is not counted in that number: losing
    the tool calls is the design here, and reporting it as a trim would put a
    warning on every ordinary run.
    """
    essential: list[tuple[int, dict]] = []
    secondary: list[tuple[int, dict]] = []
    for position, event in enumerate(events or []):
        slim = _slim(event)
        if slim is None:
            continue
        bucket = essential if slim.get("kind") in ESSENTIAL_KINDS else secondary
        bucket.append((position, slim))

    dropped = 0
    if len(essential) > cap:
        # Pathological and still bounded: an enormous run keeps its opening and
        # loses its tail rather than being stored whole.
        dropped += len(essential) - cap
        essential = essential[:cap]
    room = max(0, cap - len(essential))
    if len(secondary) > room:
        dropped += len(secondary) - room
        secondary = secondary[:room]

    kept = sorted(essential + secondary, key=lambda pair: pair[0])
    return [slim for _, slim in kept], dropped


def refuted_claims(events, report_data: dict) -> list[dict]:
    """The claims that died, rebuilt from the stream.

    Findings that were merged into another are excluded. A duplicate is not a
    refuted claim, and listing it as one would report the arena as having
    destroyed work it actually consolidated.
    """
    kept = {str(f.get("id", "")) for f in (report_data.get("findings") or [])
            if isinstance(f, dict)}
    merged: set[str] = set()
    claims: dict[str, dict] = {}

    for event in events or []:
        if not isinstance(event, dict):
            continue
        kind = event.get("kind")
        if kind == "finding_raised":
            finding_id = str(event.get("id", ""))
            if finding_id and finding_id not in kept:
                claims[finding_id] = {
                    "id": finding_id,
                    "lane": str(event.get("lane", "")),
                    "title": _trim(str(event.get("title", ""))),
                    "file": str(event.get("file", "")),
                    "line": _as_int(event.get("line")),
                    "severity": str(event.get("severity", "")),
                    "summary": _trim(str(event.get("summary", ""))),
                    "verdicts": [],
                    "survived": False,
                    "refuted_by": 0,
                    "survived_by": 0,
                }
        elif kind == "finding_merged":
            merged.add(str(event.get("dropped", "")))
        elif kind == "verdict":
            claim = claims.get(str(event.get("finding", "")))
            if claim is not None:
                claim["verdicts"].append({
                    "refuted": bool(event.get("refuted", True)),
                    "confidence": str(event.get("confidence", "")),
                    "reasoning": _trim(str(event.get("reasoning", ""))),
                })
        elif kind == "finding_settled":
            claim = claims.get(str(event.get("id", "")))
            if claim is not None:
                claim["survived"] = bool(event.get("survived"))
                claim["refuted_by"] = _as_int(event.get("refuted_by"))
                claim["survived_by"] = _as_int(event.get("survived_by"))

    return [claim for finding_id, claim in claims.items()
            if finding_id not in merged and not claim["survived"]]


# ------------------------------------------------------------------ scoring

def score_against_key(report, workspace) -> dict | None:
    """What a run found of what was planted, and what it missed.

    Shared with the command line runner rather than copied, so a run scored on
    its way into the archive and the same run scored at a terminal cannot
    disagree about what it caught.

    A workspace with no answer key scores as None rather than as zero. No key
    and no defects found are very different statements and must not render the
    same.

    Difficulty is carried through because the aggregate hides the only
    interesting question, which is whether the defects that got away were the
    hard ones.
    """
    key_path = Path(workspace) / ".answer_key.json"
    try:
        key = json.loads(key_path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        # Missing, unreadable, or not JSON. Scoring is a bonus on top of a run
        # that has already happened, so it declines rather than raises.
        return None
    planted = key.get("defects") if isinstance(key, dict) else None
    if not isinstance(planted, list):
        return None

    findings = _findings_of(report)
    found: list[dict] = []
    missed: list[dict] = []
    for defect in planted:
        if not isinstance(defect, dict):
            continue
        entry = {
            "id": str(defect.get("id", "")),
            "difficulty": str(defect.get("difficulty", "unknown")),
            "file": str(defect.get("file", "")),
            "line": _as_int(defect.get("line")),
            "summary": _trim(str(defect.get("summary", "")), 400),
        }
        hit = any(_same_defect(defect, finding) for finding in findings)
        (found if hit else missed).append(entry)

    by_difficulty: dict[str, dict] = {}
    for entry in found:
        row = by_difficulty.setdefault(entry["difficulty"], {"found": 0, "planted": 0})
        row["found"] += 1
        row["planted"] += 1
    for entry in missed:
        row = by_difficulty.setdefault(entry["difficulty"], {"found": 0, "planted": 0})
        row["planted"] += 1

    total = len(found) + len(missed)
    return {
        "planted": total,
        "found": found,
        "missed": missed,
        "by_difficulty": by_difficulty,
        "recall": round(len(found) / total, 3) if total else 0.0,
    }


def _same_defect(defect: dict, finding: dict) -> bool:
    """Same file, and close enough on the line.

    Compared on the file name rather than the whole path because a planted
    defect is recorded relative to the workspace and a hunter reports whatever
    path it was handed, and neither spelling is wrong.
    """
    planted_file = Path(str(defect.get("file", ""))).name.lower()
    found_file = Path(str(finding.get("file", ""))).name.lower()
    if not planted_file or planted_file != found_file:
        return False
    gap = abs(_as_int(defect.get("line")) - _as_int(finding.get("line")))
    return gap <= MATCH_WINDOW_LINES


# ------------------------------------------------------------------ helpers

def _as_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_float(value, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _report_dict(report) -> dict:
    """A Report, or something already shaped like one.

    The orchestrator returns the dataclass and the server publishes the same
    thing as a dictionary in its closing event, so both are accepted. Anything
    else is a programming error and says so immediately rather than being
    stored as an empty run.
    """
    if is_dataclass(report) and not isinstance(report, type):
        return asdict(report)
    if isinstance(report, dict):
        return dict(report)
    raise TypeError(f"cannot archive a {type(report).__name__} as a run report")


def _findings_of(report) -> list[dict]:
    data = report if isinstance(report, dict) else _report_dict(report)
    return [f for f in (data.get("findings") or []) if isinstance(f, dict)]


class _Row(NamedTuple):
    """One file in the archive, with what is needed to order and read it."""

    saved_at: str
    mtime_ns: int
    name: str
    path: Path
    record: dict | None


# ------------------------------------------------------------------ archive

class Archive:
    """A directory of finished runs, newest first, with a retention limit."""

    def __init__(self, directory=ARCHIVE_DIR, *, keep: int = RETENTION_DEFAULT,
                 clock=None):
        self.directory = Path(directory)
        self.keep = max(1, _as_int(keep, RETENTION_DEFAULT))
        # Injected so tests get deterministic ordering, the same arrangement
        # the ledger uses for the same reason.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Re-entrant because a save takes the lock and then sweeps under it.
        # Writing a run and deciding which runs are now surplus has to be one
        # step: two runs finishing together would otherwise both read the same
        # listing, both decide the same file is the oldest, and act on answers
        # that stopped being true the moment the other one wrote.
        self._lock = threading.RLock()
        self._notes: list[str] = []
        self._noted: set[str] = set()
        self._notes_lock = threading.Lock()

    # ------------------------------------------------------------- notes

    def _note(self, message: str) -> None:
        """Record a skipped file once.

        Once per file per process, because the alternative is a corrupt file
        printing a line on every page load for the life of the deployment,
        which trains whoever is reading the logs to ignore them.
        """
        with self._notes_lock:
            if message in self._noted:
                return
            self._noted.add(message)
            self._notes.append(message)
        print(f"[archive] {message}")

    def notes(self) -> list[str]:
        with self._notes_lock:
            return list(self._notes)

    # ------------------------------------------------------------ writing

    def build_record(self, report, events, *, workspace=None) -> dict:
        """The stored shape, assembled but not yet written."""
        data = _report_dict(report)
        run_id = str(data.get("run_id") or "").strip()
        if not _RUN_ID.match(run_id):
            raise ValueError(f"refusing to archive a run with the identifier {run_id!r}")

        replay, dropped = compact_replay(events)
        score = score_against_key(report, workspace) if workspace else None
        return {
            "schema": SCHEMA_VERSION,
            "run_id": run_id,
            "saved_at": self._timestamp(),
            "task": str(data.get("task") or ""),
            "lanes": [str(lane) for lane in (data.get("lanes") or [])],
            "raised": _as_int(data.get("raised")),
            "survived": _as_int(data.get("survived")),
            "spend_usd": _as_float(data.get("spend_usd")),
            "calls": _as_int(data.get("calls")),
            "tool_calls": _as_int(data.get("tool_calls")),
            "refusals": _as_int(data.get("refusals")),
            "seconds": _as_float(data.get("seconds")),
            "ledger_head": str(data.get("ledger_head") or ""),
            "halted": str(data.get("halted") or ""),
            # The report as it was produced, kept whole. Everything above is a
            # convenience for a listing and is derived from this.
            "report": data,
            "findings": _findings_of(data),
            "refuted": refuted_claims(events, data),
            "score": score,
            "replay": replay,
            "replay_dropped": dropped,
        }

    def save(self, report, events, *, workspace=None, keep=None) -> Path:
        """Write one finished run and sweep the surplus. Returns the path."""
        record = self.build_record(report, events, workspace=workspace)
        path = self.directory / f"{record['run_id']}.json"
        with self._lock:
            self._write(path, record)
            self.prune(keep)
        return path

    def _write(self, path: Path, record: dict) -> None:
        """Temporary file, then one move, so a reader never sees half a run.

        The move is atomic on both platforms this runs on. The contents
        reaching the disk are not, which is what the fsync is for: without it a
        power loss can leave the final name in place over an empty file, and an
        empty file under a real run's name is the one outcome the temporary
        file existed to prevent.
        """
        self.directory.mkdir(parents=True, exist_ok=True)
        temp = path.with_name(f"{path.name}{TEMP_MARKER}{uuid.uuid4().hex[:8]}")
        try:
            with temp.open("w", encoding="utf-8", newline="\n") as handle:
                # default=str so an exotic value in an event cannot fail the
                # save of a run that has already been paid for.
                json.dump(record, handle, ensure_ascii=False, default=str)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp, path)
        except BaseException:
            # Whatever went wrong, the fragment does not stay. Already gone is
            # the ordinary case when the move itself succeeded and something
            # after it raised.
            try:
                temp.unlink()
            except OSError:
                pass
            raise

    def _timestamp(self) -> str:
        return self._clock().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"

    # ------------------------------------------------------------ reading

    def _files(self) -> list[Path]:
        """The real .json files sitting directly in the archive directory.

        Not recursive, and symlinks are excluded rather than resolved. That
        second part is the one that matters: the retention sweep deletes what
        this returns, and a symlink planted here and followed would delete
        something outside the archive entirely. This module did not write it,
        so it is left exactly where it is.
        """
        try:
            with os.scandir(self.directory) as entries:
                return [Path(entry.path) for entry in entries
                        if entry.name.endswith(".json")
                        and entry.is_file(follow_symlinks=False)]
        except FileNotFoundError:
            return []
        except OSError as exc:
            self._note(f"the archive directory could not be listed "
                       f"({type(exc).__name__})")
            return []

    def _load(self, path: Path) -> dict | None:
        """One archived run, or None with a note.

        Every way this returns None is ordinary rather than exceptional: the
        retention sweep removed the file between the listing and the read, the
        process died mid-write, or the contents are not the shape this module
        writes. A page load fails for none of them.

        ValueError is caught around the read as well as the parse, because
        binary rubbish under a .json name raises UnicodeDecodeError out of
        read_text, which is a ValueError and not an OSError.
        """
        try:
            raw = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except (OSError, ValueError) as exc:
            self._note(f"{path.name}: skipped, could not be read "
                       f"({type(exc).__name__})")
            return None
        try:
            record = json.loads(raw)
        except ValueError:
            self._note(f"{path.name}: skipped, not valid JSON, most likely a "
                       f"run whose process died mid-write")
            return None
        if not isinstance(record, dict) or not record.get("run_id"):
            self._note(f"{path.name}: skipped, valid JSON but not an archived run")
            return None
        return record

    def _scan(self) -> list[_Row]:
        """Every file in the archive, newest first, readable or not.

        Ordered on the timestamp inside the record rather than on the file's
        modification time, because copying or restoring a directory rewrites
        mtimes and the archive would silently reorder itself. mtime breaks
        ties, since two runs can finish in the same millisecond and an unstable
        order would have the retention sweep choose differently on each pass.

        Unreadable files sort last, so a corrupt file is the first thing the
        sweep removes rather than something that lingers forever.
        """
        rows: list[_Row] = []
        for path in self._files():
            record = self._load(path)
            try:
                mtime = path.stat().st_mtime_ns
            except OSError:
                mtime = 0
            saved_at = str(record.get("saved_at") or "") if record else ""
            rows.append(_Row(saved_at, mtime, path.name, path, record))
        rows.sort(key=lambda row: (row.saved_at, row.mtime_ns, row.name),
                  reverse=True)
        return rows

    def latest(self) -> dict | None:
        """The most recent readable run, or None if the archive is empty."""
        for row in self._scan():
            if row.record is not None:
                return row.record
        return None

    def get(self, run_id: str) -> dict | None:
        """One run by identifier.

        The identifier is checked before it is joined to a path. This is
        reached from the web layer, where the value is whatever a stranger put
        in the URL, and a containment check that happens after the filesystem
        call has already happened too late.
        """
        if not _RUN_ID.match(str(run_id or "")):
            return None
        path = self.directory / f"{run_id}.json"
        if not path.is_file() or path.is_symlink():
            return None
        return self._load(path)

    def list_runs(self, limit: int = 20) -> list[dict]:
        """Summaries, newest first."""
        limit = max(0, _as_int(limit, 20))
        summaries = []
        for row in self._scan():
            if row.record is None:
                continue
            summaries.append(_summary(row.record))
            if len(summaries) >= limit:
                break
        return summaries

    # ---------------------------------------------------------- retention

    def prune(self, keep=None) -> list[str]:
        """Keep the newest runs and delete the rest. Returns what was removed."""
        limit = self.keep if keep is None else max(1, _as_int(keep, self.keep))
        with self._lock:
            removed = []
            for row in self._scan()[limit:]:
                if self._delete(row.path):
                    removed.append(row.name)
            return removed

    def _delete(self, path: Path) -> bool:
        """Remove one archived file, safely, or leave it and say why.

        The path came from a non-recursive scan that already excluded symlinks,
        so there is nothing here to resolve and nothing outside the archive
        that can be reached. The containment check is repeated anyway, because
        this is the only line in the module that destroys anything and it
        should be readable as safe without tracing where its argument came
        from.

        Two outcomes are expected rather than exceptional. The file is already
        gone, which counts as success since the state the caller wanted is the
        state that holds. And on Windows a file open in a reader refuses to be
        deleted at all, so the sweep leaves it and takes it on the next pass
        rather than failing the run that was saving itself.
        """
        if path.parent != self.directory or not path.name.endswith(".json"):
            self._note(f"{path.name}: left alone, it is not a file this archive owns")
            return False
        if path.is_symlink():
            self._note(f"{path.name}: left alone, it is a symlink")
            return False
        try:
            os.unlink(path)
            return True
        except FileNotFoundError:
            return True
        except OSError as exc:
            self._note(f"{path.name}: could not be removed ({type(exc).__name__}), "
                       f"leaving it for the next sweep")
            return False


def _summary(record: dict) -> dict:
    """The listing row. Small on purpose: a page listing thirty runs should not
    load thirty full replays to render thirty lines."""
    score = record.get("score") or {}
    return {
        "run_id": str(record.get("run_id", "")),
        "timestamp": str(record.get("saved_at", "")),
        "task": str(record.get("task", "")),
        "raised": _as_int(record.get("raised")),
        "survived": _as_int(record.get("survived")),
        "spend_usd": _as_float(record.get("spend_usd")),
        "seconds": _as_float(record.get("seconds")),
        "lanes": len(record.get("lanes") or []),
        "halted": str(record.get("halted", "")),
        "ledger_head": str(record.get("ledger_head", "")),
        "missed": len(score.get("missed") or []) if score else 0,
    }


# ------------------------------------------------------------- module API

_DEFAULT = Archive()
_INSTANCES: dict[str, Archive] = {}
_INSTANCES_LOCK = threading.Lock()


def _archive(directory=None) -> Archive:
    """One Archive per directory, shared.

    A lock only excludes the threads holding the same lock. Building a fresh
    Archive on every call would hand each thread a private one and provide no
    mutual exclusion whatever, which is the kind of thread safety that passes
    every test and then interleaves a sweep with a write in production.
    """
    if directory is None:
        return _DEFAULT
    key = os.path.normcase(str(Path(directory)))
    with _INSTANCES_LOCK:
        instance = _INSTANCES.get(key)
        if instance is None:
            instance = Archive(directory)
            _INSTANCES[key] = instance
        return instance


def save_run(report, events, *, workspace=None, directory=None, keep=None) -> Path:
    """Archive a completed run. Returns the path written."""
    return _archive(directory).save(report, events, workspace=workspace, keep=keep)


def latest(*, directory=None) -> dict | None:
    """The most recent archived run, or None."""
    return _archive(directory).latest()


def get_run(run_id: str, *, directory=None) -> dict | None:
    return _archive(directory).get(run_id)


def list_runs(limit: int = 20, *, directory=None) -> list[dict]:
    """Summaries of archived runs, newest first."""
    return _archive(directory).list_runs(limit)


def prune(keep=None, *, directory=None) -> list[str]:
    return _archive(directory).prune(keep)


def notes(*, directory=None) -> list[str]:
    """Files this process skipped, and why."""
    return _archive(directory).notes()
