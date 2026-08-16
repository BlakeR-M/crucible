"""The arena, served.

Standard library only, on purpose. A demonstration whose own dependency tree
needs explaining is a weaker demonstration, and an air-gapped reviewer can read
every line of what serves it without leaving this repository.

Three things worth knowing about the design.

The workspace is fixed. A visitor picks a task, never a path. The policy would
refuse an escape anyway, but a public arena that accepts a directory from a
stranger is a different and much worse thing than one that does not.

The event stream is per run and buffered from the start, so a browser that
connects late, or reconnects after a dropped connection, replays the run from
its beginning rather than joining midway into a story it cannot follow.

Concurrency is capped and so is spend. Both of those are the same argument the
arena is making about agents, applied to the thing hosting them.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import queue
import secrets
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from .ledger import Ledger
from .orchestrator import Orchestrator
from .policy import review_policy
from .providers import Budget, provider_from_env

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"
RUNS = ROOT / "runs"
TARGET = ROOT / "demo_target"

MAX_CONCURRENT_RUNS = 2
RUN_CEILING_USD = float(os.environ.get("CRUCIBLE_RUN_CEILING_USD", "0.60"))
DAILY_CEILING_USD = float(os.environ.get("CRUCIBLE_DAILY_CEILING_USD", "8.00"))
SESSION_HOURS = 12

# Railway's edge closes any HTTP request at 15 minutes, streaming or not, and
# closes one that has been silent for 5. So a stream is retired on our own
# schedule just under the first limit, which turns a proxy killing the
# connection into an ordinary EventSource reconnect that replays the buffer.
# The heartbeat sits far under the second limit, and also under Cloudflare's
# ~100 second idle window in case this ever runs behind an orange cloud.
STREAM_LIFETIME_SECONDS = 13 * 60
HEARTBEAT_SECONDS = 15

DEMO_USER = os.environ.get("CRUCIBLE_USER", "evaluator")
DEMO_PASS = os.environ.get("CRUCIBLE_PASS", "crucible")
SECRET = os.environ.get("CRUCIBLE_SECRET", secrets.token_hex(32))

TASKS = {
    "full": "Review this codebase for correctness defects. Report only real bugs.",
    "money": "Review this codebase for defects in money handling, rounding and totals.",
    "concurrency": "Review this codebase for race conditions, ordering and idempotency defects.",
    "auth": "Review this codebase for access-control and validation defects.",
}


# ------------------------------------------------------------------- state

class RunRegistry:
    """Live runs, their event buffers, and the day's spend."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._runs: dict[str, dict] = {}
        self._spend_day = ""
        self._spend_usd = 0.0

    def day_spend(self) -> float:
        with self._lock:
            today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
            if today != self._spend_day:
                self._spend_day, self._spend_usd = today, 0.0
            return self._spend_usd

    def add_spend(self, usd: float) -> None:
        with self._lock:
            self._spend_usd += usd

    def active(self) -> int:
        with self._lock:
            return sum(1 for r in self._runs.values() if not r["finished"])

    def create(self, run_id: str) -> dict:
        with self._lock:
            state = {
                "id": run_id, "events": [], "finished": False,
                "subscribers": [], "started": time.time(), "report": None,
            }
            self._runs[run_id] = state
            return state

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            return self._runs.get(run_id)

    def publish(self, run_id: str, event: dict) -> None:
        """Append to the buffer and push to anyone listening."""
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return
            event = {**event, "n": len(state["events"])}
            state["events"].append(event)
            if event.get("kind") == "run_finished":
                state["finished"] = True
                state["report"] = event
            dead = []
            for sub in state["subscribers"]:
                try:
                    sub.put_nowait(event)
                except queue.Full:
                    dead.append(sub)
            for sub in dead:
                state["subscribers"].remove(sub)

    def subscribe(self, run_id: str) -> tuple[list, queue.Queue] | None:
        """Everything so far, plus a queue for what comes next.

        Both taken under one lock: releasing between them would drop any event
        published in the gap, which on a fast run is most of the interesting
        part.
        """
        with self._lock:
            state = self._runs.get(run_id)
            if state is None:
                return None
            backlog = list(state["events"])
            channel: queue.Queue = queue.Queue(maxsize=2000)
            state["subscribers"].append(channel)
            return backlog, channel

    def unsubscribe(self, run_id: str, channel: queue.Queue) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if state and channel in state["subscribers"]:
                state["subscribers"].remove(channel)

    def reap(self, older_than_seconds: int = 3600) -> None:
        with self._lock:
            cutoff = time.time() - older_than_seconds
            for run_id in [k for k, v in self._runs.items()
                           if v["finished"] and v["started"] < cutoff]:
                del self._runs[run_id]


REGISTRY = RunRegistry()


# -------------------------------------------------------------------- auth

def issue_session() -> str:
    """A signed, expiring cookie value. No server-side session store, so a
    restart does not log everyone out and there is nothing to grow unbounded."""
    expires = str(int(time.time()) + SESSION_HOURS * 3600)
    signature = hmac.new(SECRET.encode(), expires.encode(), hashlib.sha256).hexdigest()
    return f"{expires}.{signature}"


def valid_session(cookie: str) -> bool:
    try:
        expires, signature = cookie.split(".", 1)
    except ValueError:
        return False
    expected = hmac.new(SECRET.encode(), expires.encode(), hashlib.sha256).hexdigest()
    # compare_digest rather than ==, so the comparison does not leak where the
    # first differing byte is through how long it took to fail.
    if not hmac.compare_digest(signature, expected):
        return False
    try:
        return int(expires) > time.time()
    except ValueError:
        return False


def credentials_ok(user: str, password: str) -> bool:
    return (hmac.compare_digest(user, DEMO_USER)
            and hmac.compare_digest(password, DEMO_PASS))


# ------------------------------------------------------------------ runner

def start_run(task_key: str) -> tuple[str | None, str]:
    """Spawn a run, or say why not."""
    if REGISTRY.active() >= MAX_CONCURRENT_RUNS:
        return None, (f"{MAX_CONCURRENT_RUNS} runs are already in flight. "
                      f"The arena runs a bounded number at once, deliberately. "
                      f"Try again in a minute.")
    if REGISTRY.day_spend() >= DAILY_CEILING_USD:
        return None, (f"the daily ceiling of ${DAILY_CEILING_USD:.2f} is spent. "
                      f"It resets at midnight UTC.")
    if not TARGET.is_dir():
        return None, "the demo target codebase is missing from this deployment"

    task = TASKS.get(task_key, TASKS["full"])
    run_id = uuid.uuid4().hex[:12]
    REGISTRY.create(run_id)

    def work() -> None:
        ledger = Ledger(RUNS / f"{run_id}.jsonl")
        budget = Budget(ceiling_usd=RUN_CEILING_USD)
        try:
            provider = provider_from_env(
                extra_env=Path(os.environ.get("CRUCIBLE_ENV_FILE", ROOT / ".env"))
            )
            orchestrator = Orchestrator(
                provider, TARGET, review_policy(TARGET), ledger, budget,
                emit=lambda event: REGISTRY.publish(run_id, event),
            )
            orchestrator.run(task)
        except Exception as exc:  # noqa: BLE001 - the browser must be told
            REGISTRY.publish(run_id, {
                "kind": "run_failed", "reason": f"{type(exc).__name__}: {exc}"[:300],
            })
            REGISTRY.publish(run_id, {"kind": "run_finished", "run_id": run_id,
                                      "raised": 0, "survived": 0, "findings": [],
                                      "spend_usd": round(budget.spent_usd, 5),
                                      "halted": str(exc)[:200]})
        finally:
            REGISTRY.add_spend(budget.spent_usd)
            REGISTRY.reap()

    threading.Thread(target=work, daemon=True, name=f"run-{run_id}").start()
    return run_id, ""


# ----------------------------------------------------------------- handler

class Handler(BaseHTTPRequestHandler):
    server_version = "crucible"
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):  # quieter, and no query strings logged
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    # ---------------------------------------------------------- helpers

    def _cookie(self, name: str) -> str:
        raw = self.headers.get("Cookie", "")
        for part in raw.split(";"):
            key, _, value = part.strip().partition("=")
            if key == name:
                return value
        return ""

    def _authed(self) -> bool:
        return valid_session(self._cookie("crucible_session"))

    def _send(self, code: int, body: bytes, content_type: str,
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload: dict) -> None:
        self._send(code, json.dumps(payload).encode("utf-8"), "application/json")

    def _file(self, name: str, content_type: str) -> None:
        path = (WEB / name).resolve()
        # Resolved containment, the same rule the agents live under.
        if WEB.resolve() not in path.parents or not path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        self._send(200, path.read_bytes(), content_type,
                   {"Cache-Control": "no-cache"})

    # ------------------------------------------------------------- GET

    def do_GET(self) -> None:
        route = urlparse(self.path)
        path = route.path

        if path == "/healthz":
            self._json(200, {"ok": True, "active": REGISTRY.active(),
                             "day_spend_usd": round(REGISTRY.day_spend(), 4)})
            return

        if path in ("/login", "/login/"):
            self._file("login.html", "text/html; charset=utf-8")
            return

        if path.startswith("/static/"):
            name = path[len("/static/"):]
            kinds = {".css": "text/css; charset=utf-8",
                     ".js": "application/javascript; charset=utf-8",
                     ".svg": "image/svg+xml"}
            # The URL prefix and the directory name are both "static", so the
            # prefix is stripped and then put back rather than assumed away.
            self._file(f"static/{name}",
                       kinds.get(Path(name).suffix, "application/octet-stream"))
            return

        if not self._authed():
            if path.startswith("/api/"):
                self._json(401, {"error": "not signed in"})
            else:
                self._send(302, b"", "text/plain", {"Location": "/login"})
            return

        if path in ("/", "/index.html"):
            self._file("index.html", "text/html; charset=utf-8")
            return

        if path == "/api/tasks":
            self._json(200, {"tasks": [{"key": k, "label": v}
                                       for k, v in TASKS.items()],
                             "ceiling_usd": RUN_CEILING_USD,
                             "active": REGISTRY.active(),
                             "max_concurrent": MAX_CONCURRENT_RUNS})
            return

        if path.startswith("/api/stream/"):
            self._stream(path.rsplit("/", 1)[-1])
            return

        if path.startswith("/api/ledger/"):
            run_id = path.rsplit("/", 1)[-1]
            ledger_path = (RUNS / f"{run_id}.jsonl").resolve()
            if RUNS.resolve() not in ledger_path.parents or not ledger_path.is_file():
                self._json(404, {"error": "no ledger for that run"})
                return
            self._send(200, ledger_path.read_bytes(), "application/x-ndjson",
                       {"Content-Disposition": f'attachment; filename="{run_id}.jsonl"'})
            return

        self._send(404, b"not found", "text/plain")

    # ------------------------------------------------------------ POST

    def do_POST(self) -> None:
        route = urlparse(self.path)
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""

        if route.path == "/api/login":
            form = parse_qs(raw.decode("utf-8", "replace"))
            user = (form.get("user") or [""])[0]
            password = (form.get("password") or [""])[0]
            if credentials_ok(user, password):
                self._send(303, b"", "text/plain", {
                    "Location": "/",
                    "Set-Cookie": (f"crucible_session={issue_session()}; "
                                   f"Path=/; HttpOnly; SameSite=Lax; Secure"),
                })
            else:
                # One deliberate second. Not a real defence on its own, but it
                # takes the sting out of someone walking a password list.
                time.sleep(1.0)
                self._send(303, b"", "text/plain", {"Location": "/login?bad=1"})
            return

        if not self._authed():
            self._json(401, {"error": "not signed in"})
            return

        if route.path == "/api/run":
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                self._json(400, {"error": "bad json"})
                return
            run_id, refusal = start_run(str(payload.get("task", "full")))
            if run_id is None:
                self._json(429, {"error": refusal})
                return
            self._json(200, {"run_id": run_id})
            return

        self._send(404, b"not found", "text/plain")

    # ----------------------------------------------------------- stream

    def _stream(self, run_id: str) -> None:
        subscription = REGISTRY.subscribe(run_id)
        if subscription is None:
            self._json(404, {"error": "no such run"})
            return
        backlog, channel = subscription

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        # Nginx and several proxies in front of a deployment like this buffer a
        # response until it completes, which turns a live stream into one long
        # pause and then everything at once. This is the header that stops it.
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        def write(event: dict) -> bool:
            try:
                # The id line lets a reconnecting browser tell the server how
                # far it got. The buffer is replayed in full regardless, so
                # this is for the client's benefit rather than the server's.
                self.wfile.write(
                    f"id: {event.get('n', 0)}\n"
                    f"data: {json.dumps(event)}\n\n".encode("utf-8")
                )
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        deadline = time.time() + STREAM_LIFETIME_SECONDS
        try:
            for event in backlog:
                if not write(event):
                    return
            finished = any(e.get("kind") == "run_finished" for e in backlog)
            while not finished:
                if time.time() > deadline:
                    # Retire before the edge does. EventSource reconnects by
                    # itself and gets the whole run replayed, so the visitor
                    # sees continuity rather than an error.
                    return
                try:
                    event = channel.get(timeout=HEARTBEAT_SECONDS)
                except queue.Empty:
                    # A comment line keeps the connection warm through proxies
                    # that close an idle one, and costs nothing.
                    try:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    except (BrokenPipeError, ConnectionResetError, OSError):
                        return
                if not write(event):
                    return
                if event.get("kind") == "run_finished":
                    finished = True
        finally:
            REGISTRY.unsubscribe(run_id, channel)


def serve() -> None:
    RUNS.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", "8420"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"crucible listening on :{port}")
    print(f"  target      {TARGET}")
    print(f"  run ceiling ${RUN_CEILING_USD:.2f}   daily ${DAILY_CEILING_USD:.2f}")
    print(f"  sign in as  {DEMO_USER}")
    server.serve_forever()


if __name__ == "__main__":
    serve()
