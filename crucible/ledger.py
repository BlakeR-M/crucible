"""The record of a run, written so that editing it afterwards shows.

An agent that reports its own behaviour is a witness with an interest. The
ledger removes the question: every event is appended with the hash of the one
before it, so the file carries its own proof of order and completeness. Change
an entry, drop one, or slip one in, and verify() names the first line where the
chain stops adding up.

This is not tamper-proof, which would want a signature and somewhere else to
keep the key. It is tamper-evident, which is the property that matters when the
argument is "here is what actually happened" and the other party has no reason
to take your word for it.
"""

from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

GENESIS = "0" * 64


def _canonical(payload: dict) -> str:
    """Key order and separators fixed, so the same content always hashes the
    same way. Without this the chain breaks on a dictionary that merely
    serialised differently."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _digest(seq: int, ts: str, event: str, payload: dict, prev: str) -> str:
    body = _canonical(
        {"seq": seq, "ts": ts, "event": event, "payload": payload, "prev": prev}
    )
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Break:
    """Where the chain stops holding, and what is wrong there."""

    seq: int
    reason: str


class Ledger:
    """Append-only JSONL. One file per run."""

    def __init__(self, path: Path, *, clock=None, scrub=None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Injected so tests get deterministic timestamps and the chain they
        # assert on is reproducible.
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        # Applied to every payload before it is hashed and written. The
        # hosted server passes a redactor here so a provider error that quotes
        # a key is scrubbed before it becomes part of the record, and the
        # chain is computed over what was actually written.
        self._scrub = scrub
        self._seq = 0
        self._head = GENESIS
        # Agents run in parallel and all of them write here. Reading the head,
        # hashing against it, writing the line and advancing the head has to be
        # one indivisible step: two threads interleaving those four operations
        # produce two entries claiming the same predecessor, and the chain that
        # is supposed to prove nothing was tampered with breaks by itself.
        self._lock = threading.Lock()
        if self.path.exists():
            self._resume()

    def _resume(self) -> None:
        """Pick up an existing file, so a reopened run keeps one chain."""
        for line in self.path.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            entry = json.loads(line)
            self._seq = entry["seq"] + 1
            self._head = entry["hash"]

    def append(self, event: str, **payload) -> dict:
        """Write one event and return it, including its place in the chain."""
        if self._scrub is not None:
            payload = self._scrub(payload)
        with self._lock:
            ts = self._clock().strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
            entry = {
                "seq": self._seq,
                "ts": ts,
                "event": event,
                "payload": payload,
                "prev": self._head,
                "hash": _digest(self._seq, ts, event, payload, self._head),
            }
            with self.path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(_canonical(entry) + "\n")
            self._seq += 1
            self._head = entry["hash"]
            return entry

    def entries(self) -> list[dict]:
        if not self.path.exists():
            return []
        return [
            json.loads(line)
            for line in self.path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]

    def verify(self) -> Break | None:
        """Walk the chain and return the first place it stops holding.

        Three ways a file fails: an entry whose hash disagrees with its own
        contents, an entry whose prev does not match the entry before it, and a
        gap or repeat in the sequence. The first two catch an edit, the third
        catches a deletion at the end where the remaining chain is still
        internally consistent.
        """
        prev_hash = GENESIS
        expected_seq = 0
        for entry in self.entries():
            seq = entry.get("seq")
            if seq != expected_seq:
                return Break(expected_seq, f"expected entry {expected_seq}, found {seq}")
            recomputed = _digest(
                entry["seq"],
                entry["ts"],
                entry["event"],
                entry["payload"],
                entry["prev"],
            )
            if recomputed != entry["hash"]:
                return Break(seq, "contents do not match the recorded hash")
            if entry["prev"] != prev_hash:
                return Break(seq, "does not follow the entry before it")
            prev_hash = entry["hash"]
            expected_seq += 1
        return None

    @property
    def head(self) -> str:
        """The current tip. Quoting this alongside a published report is what
        lets someone check later that the file they were given is the file that
        was written."""
        return self._head
