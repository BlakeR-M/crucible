"""Findings against the answer key, matched on more than a line number.

Nine defects are planted in demo_target and their exact positions are known, so
recovery can be counted rather than judged. The counting is less obvious than
it sounds. Three files carry two defects each and the pairs sit thirteen,
eighteen and twenty lines apart, so a window generous enough to credit a
finding that points at a function's signature rather than at the guilty line is
also wide enough to let one finding claim either defect.

So two signals. Line proximity decides candidacy, and a short list of terms
drawn from each defect's own description decides between candidates. A finding
about mutable default arguments and a finding about float rounding both land in
invoicing.py within a dozen lines of each other, and only one of them mentions
adjustments.

Each planted defect can be claimed once. A run that reports the same defect
three times has found one defect, and counting it three times would be the
flattering arithmetic rather than the true one.

Findings that match nothing are reported as unmatched rather than as wrong.
demo_target was written to carry nine defects, not to be free of every other
one, so an unmatched survivor may be a genuine defect nobody planted. Calling
it a false positive here would be an accusation the answer key cannot support.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

# The window inside which a finding may claim a defect. Set from the widest
# honest gap between a defect and the line a reviewer would reasonably cite for
# it: D1's mutation is on line 83 and its cause, the mutable default, is in the
# signature about fifteen lines above.
LINE_WINDOW = 16

# Terms that distinguish each planted defect from its neighbour in the same
# file. Lowercased substring tests, chosen to be specific to the defect rather
# than generic review vocabulary.
KEYWORDS: dict[str, tuple[str, ...]] = {
    "D1": ("mutable default", "default argument", "adjustments", "shared list",
           "default=[]", "adjustments=[]"),
    "D2": ("floor division", "total_pages", "has_next", "partial", "//",
           "page count"),
    "D3": ("float", "banker", "round_half_up", "rounding", "decimal",
           "percentage_of"),
    "D4": ("rollback", "reservation", "release", "compensat", "billingerror",
           "leak", "orphan"),
    "D5": ("half-open", "<=", "touching", "adjacent", "overlap", "boundary"),
    "D6": ("race", "atomic", "lock", "check-then-act", "toctou",
           "double-book", "concurren"),
    "D7": ("cache", "invalidat", "stale", "old date", "reschedul"),
    "D8": ("falsy", " or ", "level 0", "suspended", "zero", "default_level"),
    "D9": ("idempotenc", "hash", "sha", "same day", "collision", "digest"),
}


@dataclass
class Match:
    defect: str
    finding_id: str
    finding_title: str
    line_claimed: int
    line_actual: int
    distance: int
    keyword_hits: list = field(default_factory=list)


def _text_of(finding: dict) -> str:
    return " ".join(str(finding.get(k, "")) for k in
                    ("title", "summary", "failure_scenario", "evidence")).lower()


def _keyword_hits(defect_id: str, finding: dict) -> list:
    text = _text_of(finding)
    return [k for k in KEYWORDS.get(defect_id, ()) if k in text]


def load_key(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["defects"]


def score(findings: list[dict], key: list[dict]) -> dict:
    """Match findings to planted defects. One claim per defect."""
    candidates = []
    for finding in findings:
        claimed_file = Path(str(finding.get("file", ""))).name.lower()
        claimed_line = int(finding.get("line") or 0)
        for defect in key:
            if Path(defect["file"]).name.lower() != claimed_file:
                continue
            distance = abs(claimed_line - defect["line"])
            if distance > LINE_WINDOW:
                continue
            hits = _keyword_hits(defect["id"], finding)
            candidates.append({
                "defect": defect, "finding": finding,
                "distance": distance, "hits": hits,
            })

    # Best evidence first: more keyword agreement beats mere proximity, because
    # proximity is what makes the two-defects-in-one-file cases ambiguous in the
    # first place.
    candidates.sort(key=lambda c: (-len(c["hits"]), c["distance"]))

    matched: dict[str, Match] = {}
    claimed_findings: set[str] = set()
    for c in candidates:
        defect_id = c["defect"]["id"]
        finding_id = str(c["finding"].get("id", "")) or c["finding"].get("title", "")
        if defect_id in matched or finding_id in claimed_findings:
            continue
        matched[defect_id] = Match(
            defect=defect_id,
            finding_id=finding_id,
            finding_title=str(c["finding"].get("title", ""))[:90],
            line_claimed=int(c["finding"].get("line") or 0),
            line_actual=c["defect"]["line"],
            distance=c["distance"],
            keyword_hits=c["hits"],
        )
        claimed_findings.add(finding_id)

    unmatched = [
        {"title": str(f.get("title", ""))[:90],
         "file": str(f.get("file", "")), "line": f.get("line"),
         "severity": f.get("severity", "")}
        for f in findings
        if (str(f.get("id", "")) or f.get("title", "")) not in claimed_findings
    ]

    # A match resting on line proximity with no keyword agreement is the one a
    # human should look at, so it is counted separately rather than folded in.
    weak = [m.defect for m in matched.values() if not m.keyword_hits]

    return {
        "recovered": sorted(matched),
        "recovered_count": len(matched),
        "of": len(key),
        "missed": sorted(d["id"] for d in key if d["id"] not in matched),
        "weak_matches": sorted(weak),
        "unmatched_findings": unmatched,
        "detail": {k: vars(v) for k, v in sorted(matched.items())},
        "by_difficulty": _by_difficulty(matched, key),
    }


def _by_difficulty(matched: dict, key: list[dict]) -> dict:
    out: dict[str, dict] = {}
    for defect in key:
        slot = out.setdefault(defect["difficulty"], {"found": 0, "of": 0})
        slot["of"] += 1
        if defect["id"] in matched:
            slot["found"] += 1
    return out
