"""The arena, served.

Standard library only, on purpose. A demonstration whose own dependency tree
needs explaining is a weaker demonstration, and an air-gapped reviewer can read
every line of what serves it without leaving this repository.

Three things worth knowing about the design.

The workspace is fixed, or fetched. A visitor picks a task, and may name a
public repository on an allowlisted host, which is cloned into a private
temporary directory for the run and removed after it. A visitor never names a
path. The policy would refuse an escape anyway, but a public arena that
accepts a directory from a stranger is a different and much worse thing than
one that does not.

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
import sys
import threading
import time
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import repo
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
# A conversation is cheap per turn but unbounded in turns, so it carries its
# own ceiling separate from the run's. A visitor who talks all afternoon costs
# this much and then is told so plainly.
CHAT_CEILING_USD = float(os.environ.get("CRUCIBLE_CHAT_CEILING_USD", "0.40"))
SESSION_HOURS = 12

# A visitor may also name a public repository. It is fetched under the same
# rules a stranger's input always gets here: an allowlist of hosts, one commit
# at depth one, no submodules, a size cap and a file cap, a clone timeout, and
# a workspace that exists for the run and is removed after it. The review
# policy is then scoped to that workspace and nothing else on the box.
REPO_HOSTS = tuple(h.strip() for h in os.environ.get(
    "CRUCIBLE_REPO_HOSTS", ",".join(repo.DEFAULT_HOSTS)).split(",") if h.strip())
REPO_MAX_BYTES = int(float(os.environ.get("CRUCIBLE_REPO_MAX_MB", "50")) * 1_000_000)
REPO_MAX_FILES = int(os.environ.get("CRUCIBLE_REPO_MAX_FILES", "5000"))
REPO_CLONE_TIMEOUT_S = float(os.environ.get("CRUCIBLE_REPO_CLONE_TIMEOUT_S", "120"))

# Railway's edge closes any HTTP request at 15 minutes, streaming or not, and
# closes one that has been silent for 5. So a stream is retired on our own
# schedule just under the first limit, which turns a proxy killing the
# connection into an ordinary EventSource reconnect that replays the buffer.
# The heartbeat sits far under the second limit, and also under Cloudflare's
# ~100 second idle window in case this ever runs behind an orange cloud.
STREAM_LIFETIME_SECONDS = 13 * 60
HEARTBEAT_SECONDS = 15

# The demo credentials come from the environment and nowhere else. There is
# no built-in pair: a deployment that forgot to set them refuses to start
# (see serve()) rather than opening a paid model to whoever finds the URL.
DEMO_USER = os.environ.get("CRUCIBLE_USER", "")
DEMO_PASS = os.environ.get("CRUCIBLE_PASS", "")
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

    def newest(self) -> str | None:
        """The most recently started run, finished or not. A browser attaching
        after the fact still gets the whole thing, because the buffer is
        replayed from the beginning."""
        with self._lock:
            if not self._runs:
                return None
            return max(self._runs.values(), key=lambda r: r["started"])["id"]

    def create(self, run_id: str, source: dict | None = None) -> dict:
        with self._lock:
            state = {
                "id": run_id, "events": [], "finished": False,
                "subscribers": [], "started": time.time(), "report": None,
                # Where the code came from: {} for the demo target, otherwise
                # repo_url and repo_ref at creation, commit_sha once cloned.
                "source": dict(source or {}),
            }
            self._runs[run_id] = state
            return state

    def get(self, run_id: str) -> dict | None:
        with self._lock:
            return self._runs.get(run_id)

    def set_source(self, run_id: str, **fields) -> None:
        with self._lock:
            state = self._runs.get(run_id)
            if state is not None:
                state["source"].update(fields)

    def listing(self) -> list[dict]:
        """Every run the registry still holds, newest first, with its source."""
        with self._lock:
            rows = sorted(self._runs.values(), key=lambda r: -r["started"])
            return [{"run_id": r["id"], "finished": r["finished"],
                     "started": r["started"], "source": dict(r["source"])}
                    for r in rows]

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


def pick_provider():
    """The paid model, or the free stand-in.

    One place decides, so a deployment cannot end up running its reviews for
    free and its conversations for money. Offline mode is not a mock: the real
    orchestrator, policy, toolbox and ledger all run, and only the completions
    are written locally instead of bought.
    """
    from .offline import OfflineProvider, enabled

    if enabled():
        return OfflineProvider()
    return provider_from_env(
        extra_env=Path(os.environ.get("CRUCIBLE_ENV_FILE", ROOT / ".env"))
    )


REGISTRY = RunRegistry()

# The one function that reaches the network. Held here by name so a test can
# stand a local bare repository in for a host, and the rest of the run path
# is exercised as deployed.
clone = repo.clone


class ChatSessions:
    """One conversational agent per visitor, kept for the life of their visit.

    The session cookie proves someone signed in but does not say who: it is a
    signed expiry, so two visitors who log in during the same second carry the
    same value. Conversations therefore key off a separate random id, because a
    visitor must never be handed another visitor's transcript, and the agent
    will only discuss a run its own session started.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._agents: dict[str, dict] = {}

    def get(self, sid: str):
        from .chat import ChatAgent, ServerRuns

        with self._lock:
            held = self._agents.get(sid)
            if held is not None:
                held["seen"] = time.time()
                return held["agent"]

        # Built outside the lock: constructing an agent reads the task list and
        # there is no reason to hold every other visitor up for it.
        agent = ChatAgent(
            pick_provider(),
            ServerRuns(REGISTRY, runs_dir=RUNS, target=TARGET),
            Budget(ceiling_usd=CHAT_CEILING_USD),
            session_id=sid,
        )
        with self._lock:
            # Another thread may have built one for this visitor while we were
            # outside the lock. Theirs wins, so a session keeps one transcript.
            existing = self._agents.get(sid)
            if existing is not None:
                existing["seen"] = time.time()
                return existing["agent"]
            self._agents[sid] = {"agent": agent, "seen": time.time()}
            return agent

    def reap(self, idle_seconds: int = 3600) -> None:
        with self._lock:
            cutoff = time.time() - idle_seconds
            for sid in [k for k, v in self._agents.items() if v["seen"] < cutoff]:
                del self._agents[sid]

    def count(self) -> int:
        with self._lock:
            return len(self._agents)


CHATS = ChatSessions()


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
    if not DEMO_USER or not DEMO_PASS:
        return False
    return (hmac.compare_digest(user, DEMO_USER)
            and hmac.compare_digest(password, DEMO_PASS))


def missing_credentials() -> list[str]:
    """The credential variables a deployment has left unset."""
    return [name for name, value in (("CRUCIBLE_USER", DEMO_USER),
                                     ("CRUCIBLE_PASS", DEMO_PASS)) if not value]


# ------------------------------------------------------------------ runner

def plan_repo(repo_url: str, ref: str | None) -> tuple[str, str | None]:
    """Parse and admit a visitor's repository reference, or raise RepoError
    with the sentence to show them. No clone happens here."""
    url, inline_ref = repo.parse_repo_ref(repo_url)
    ref = (ref or "").strip() or None
    if inline_ref and ref and inline_ref != ref:
        raise repo.RepoUrlError("the URL names one ref and the field another; "
                                "pick one")
    ref = ref or inline_ref
    repo.check_host(url, REPO_HOSTS)
    return url, ref


def start_run(task_key: str, repo_url: str | None = None,
              ref: str | None = None) -> tuple[str | None, str]:
    """Spawn a run, or say why not.

    With a repository URL, the run clones it first, in the run's own thread,
    so the visitor sees the clone as stream events rather than as a request
    that hangs. Admission (concurrency, daily ceiling, host allowlist) is
    decided here, before anything is spent or fetched.
    """
    source: dict = {}
    if repo_url:
        try:
            url, ref = plan_repo(repo_url, ref)
        except repo.RepoError as exc:
            return None, str(exc)
        source = {"repo_url": url, "repo_ref": ref}
    if REGISTRY.active() >= MAX_CONCURRENT_RUNS:
        return None, (f"{MAX_CONCURRENT_RUNS} runs are already in flight. "
                      f"The arena runs a bounded number at once, deliberately. "
                      f"Try again in a minute.")
    if REGISTRY.day_spend() >= DAILY_CEILING_USD:
        return None, (f"the daily ceiling of ${DAILY_CEILING_USD:.2f} is spent. "
                      f"It resets at midnight UTC.")
    if not source and not TARGET.is_dir():
        return None, "the demo target codebase is missing from this deployment"

    task = TASKS.get(task_key, TASKS["full"])
    run_id = uuid.uuid4().hex[:12]
    REGISTRY.create(run_id, source)

    def finish(reason: str, spent: float = 0.0) -> None:
        """Close a run out so nothing is left hanging.

        A run that dies without publishing run_finished holds its slot in the
        concurrency count for the life of the process and leaves every attached
        browser waiting on a stream that will never end. Both failures are
        silent, which is the worst property a failure can have here.
        """
        REGISTRY.publish(run_id, {"kind": "run_failed", "reason": reason[:300]})
        REGISTRY.publish(run_id, {
            "kind": "run_finished", "run_id": run_id, "raised": 0, "survived": 0,
            "findings": [], "spend_usd": round(spent, 5), "calls": 0,
            "tool_calls": 0, "refusals": 0, "seconds": 0,
            "ledger_head": "", "lanes": [], "task": task,
            "halted": reason[:200],
        })

    def fetch(scratch: Path) -> repo.CloneResult:
        """Clone into the run's private directory, narrating to the stream."""
        REGISTRY.publish(run_id, {"kind": "clone_started",
                                  "repo_url": source["repo_url"],
                                  "repo_ref": source["repo_ref"]})
        try:
            result = clone(
                source["repo_url"], source["repo_ref"], scratch / "checkout",
                timeout_s=REPO_CLONE_TIMEOUT_S, max_bytes=REPO_MAX_BYTES,
                max_files=REPO_MAX_FILES, allowed_hosts=REPO_HOSTS,
            )
        except repo.RepoError as exc:
            REGISTRY.publish(run_id, {"kind": "clone_failed",
                                      "reason": str(exc)[:300]})
            raise
        REGISTRY.set_source(run_id, commit_sha=result.commit_sha)
        REGISTRY.publish(run_id, {
            "kind": "clone_finished", "commit_sha": result.commit_sha,
            "files": result.files, "bytes": result.bytes,
        })
        return result

    def work() -> None:
        ledger = Ledger(RUNS / f"{run_id}.jsonl")
        budget = Budget(ceiling_usd=RUN_CEILING_USD)
        scratch: Path | None = None
        try:
            if source:
                scratch = repo.workspace_dir()
                fetched = fetch(scratch)
                # The record names what the visitor asked for, canonicalised
                # at admission, and the commit the clone actually stood at.
                workspace = fetched.path
                tests_enabled = repo.url_tests_enabled()
                header = {**source, "commit_sha": fetched.commit_sha,
                          "tests_enabled": tests_enabled}
            else:
                workspace, header, tests_enabled = TARGET, {}, True
            provider = pick_provider()
            orchestrator = Orchestrator(
                provider, workspace,
                review_policy(workspace, run_tests=tests_enabled), ledger, budget,
                emit=lambda event: REGISTRY.publish(run_id, event),
                source=header,
            )
            orchestrator.run(task)
        except BaseException as exc:  # noqa: BLE001 - the browser must be told
            finish(f"{type(exc).__name__}: {exc}", budget.spent_usd)
        finally:
            if scratch is not None:
                # The clone lives exactly as long as the run. Removed here on
                # every path out, so a failed review leaves no stranger's tree
                # on the box.
                repo.remove_tree(scratch)
            REGISTRY.add_spend(budget.spent_usd)
            REGISTRY.reap()

    try:
        threading.Thread(target=work, daemon=True, name=f"run-{run_id}").start()
    except RuntimeError as exc:
        # The thread never started, so nothing else will ever close this run.
        finish(f"could not start the run: {exc}")
        return None, "the server could not start another run just now"
    return run_id, ""


def admit_run(payload: dict) -> tuple[int, dict]:
    """The POST /api/run body, decided. Returns (status, json body).

    A refused repository reference is a 400 with the sentence to show; a
    full arena or a spent day is a 429, as before. Kept out of the handler so
    a check can call it without a socket.
    """
    task = str(payload.get("task", "full") or "full")
    repo_url = str(payload.get("repo_url") or "").strip()
    ref = str(payload.get("ref") or "").strip() or None
    if repo_url:
        # Bounded before it is parsed. A URL is a stranger's string too.
        if len(repo_url) > 500 or (ref and len(ref) > 200):
            return 400, {"error": "that address is longer than any repository "
                                  "URL needs to be"}
        try:
            plan_repo(repo_url, ref)
        except repo.RepoError as exc:
            return 400, {"error": str(exc)}
    run_id, refusal = start_run(task, repo_url or None, ref)
    if run_id is None:
        return 429, {"error": refusal}
    return 200, {"run_id": run_id}


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

        if path == "/api/runs/current":
            # The chat agent can start a review mid-sentence, and the browser
            # needs to know which run to attach to without the agent having to
            # remember to announce it.
            self._json(200, {"run_id": REGISTRY.newest()})
            return

        if path == "/api/chat/opening":
            from .chat import ChatAgent  # noqa: F401  (imported for its module)

            agent = CHATS.get(self._sid())
            self._json(200, {
                "opening": agent.opening(),
                "ceiling_usd": CHAT_CEILING_USD,
                "spent_usd": round(agent.budget.spent_usd, 5),
            })
            return

        if path == "/api/tasks":
            self._json(200, {"tasks": [{"key": k, "label": v}
                                       for k, v in TASKS.items()],
                             "ceiling_usd": RUN_CEILING_USD,
                             "active": REGISTRY.active(),
                             "max_concurrent": MAX_CONCURRENT_RUNS,
                             "repo_hosts": list(REPO_HOSTS),
                             "repo_max_mb": REPO_MAX_BYTES / 1_000_000,
                             "repo_max_files": REPO_MAX_FILES,
                             "runs": REGISTRY.listing()})
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
                # Two cookies with different jobs. The signed one proves the
                # visitor got past the gate; the random one says which visitor
                # they are, so conversations do not bleed between them.
                self.send_response(303)
                self.send_header("Location", "/")
                self.send_header("Content-Length", "0")
                self.send_header("Set-Cookie",
                                 f"crucible_session={issue_session()}; "
                                 f"Path=/; HttpOnly; SameSite=Lax; Secure")
                self.send_header("Set-Cookie",
                                 f"crucible_sid={secrets.token_urlsafe(18)}; "
                                 f"Path=/; HttpOnly; SameSite=Lax; Secure")
                self.end_headers()
            else:
                # One deliberate second. Not a real defence on its own, but it
                # takes the sting out of someone walking a password list.
                time.sleep(1.0)
                self._send(303, b"", "text/plain", {"Location": "/login?bad=1"})
            return

        if not self._authed():
            self._json(401, {"error": "not signed in"})
            return

        if route.path == "/api/chat":
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                self._json(400, {"error": "bad json"})
                return
            message = str(payload.get("message", "")).strip()
            if not message:
                self._json(400, {"error": "say something"})
                return
            # Bounded before it reaches a model. A visitor pasting a novel is
            # an unbounded prompt on somebody else's key.
            if len(message) > 2000:
                self._json(413, {"error": "that message is too long"})
                return
            self._chat(self._sid(), message)
            return

        if route.path == "/api/run":
            try:
                payload = json.loads(raw.decode("utf-8")) if raw else {}
            except json.JSONDecodeError:
                self._json(400, {"error": "bad json"})
                return
            status, body = admit_run(payload)
            self._json(status, body)
            return

        self._send(404, b"not found", "text/plain")

    # ----------------------------------------------------------- stream

    def _sid(self) -> str:
        """Which visitor this is. Falls back to the signed cookie so a request
        that somehow arrives without the random one still gets a conversation
        rather than an error, and a fresh string rather than a shared one."""
        return self._cookie("crucible_sid") or f"anon-{secrets.token_urlsafe(12)}"

    def _chat(self, sid: str, message: str) -> None:
        """One conversational turn, streamed as it happens.

        The agent's tool calls and refusals reach the browser while it is still
        thinking, which is most of the point: a visitor watching it decide to
        go and read the run is watching the thing work, not waiting on a
        spinner.
        """
        agent = CHATS.get(sid)
        CHATS.reap()

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()

        def push(event: dict) -> bool:
            try:
                self.wfile.write(f"data: {json.dumps(event)}\n\n".encode("utf-8"))
                self.wfile.flush()
                return True
            except (BrokenPipeError, ConnectionResetError, OSError):
                return False

        try:
            for event in agent.stream(message):
                if not push(event):
                    return
        except Exception as exc:  # noqa: BLE001 - the visitor must be told
            push({"kind": "chat_error",
                  "text": f"{type(exc).__name__}: {exc}"[:300]})
        finally:
            push({"kind": "chat_done",
                  "spent_usd": round(agent.budget.spent_usd, 5),
                  "ceiling_usd": CHAT_CEILING_USD})

    def _stream(self, run_id: str) -> None:
        subscription = REGISTRY.subscribe(run_id)
        if subscription is None:
            self._json(404, {"error": "no such run"})
            return
        backlog, channel = subscription

        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        # HTTP/1.1 with neither Content-Length nor chunked encoding leaves the
        # message length undefined, and a proxy in front of this has to guess
        # where the response ends. Closing the connection is what actually
        # delimits a stream of unknown length, so say so rather than promising
        # keep-alive on a body that never finishes.
        self.send_header("Connection", "close")
        self.close_connection = True
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
    unset = missing_credentials()
    if unset:
        print("crucible refuses to start: set " + " and ".join(unset)
              + " in the environment. There are no built-in credentials, "
              "so a deployment without them would be open to anyone.",
              file=sys.stderr)
        raise SystemExit(2)
    RUNS.mkdir(parents=True, exist_ok=True)
    port = int(os.environ.get("PORT", "8420"))
    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    server.daemon_threads = True
    print(f"crucible listening on :{port}")
    print(f"  target      {TARGET}")
    print(f"  repos from  {', '.join(REPO_HOSTS)}  "
          f"(cap {REPO_MAX_BYTES / 1_000_000:.0f} MB, {REPO_MAX_FILES} files)")
    print(f"  run ceiling ${RUN_CEILING_USD:.2f}   daily ${DAILY_CEILING_USD:.2f}")
    print(f"  sign in as  {DEMO_USER}")
    server.serve_forever()


if __name__ == "__main__":
    serve()
