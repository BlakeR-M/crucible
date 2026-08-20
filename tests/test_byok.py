"""Checks for bring-your-own-key on the hosted server.

Plain script, no test framework:
    python tests/test_byok.py

The claims under check: a visitor's key is held in memory for the session
and nowhere else; it is validated before it is kept and a wrong one is
refused with a sentence; a run on it records provider_kind "visitor" and the
models, never the key; its ceiling is capped; its spend is the session's and
not the operator's day; a provider error that quotes the key reaches neither
the ledger, the stream nor the process's output; and logout forgets it.

The provider is a stand-in wearing OpenAIProvider's constructor, so no
network is touched and nothing is spent. The validation probe is replaced
the same way.
"""

from __future__ import annotations

import contextlib
import hashlib
import hmac
import http.client
import io
import json
import os
import socket
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlencode

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

os.environ["CRUCIBLE_OFFLINE"] = "1"
os.environ["CRUCIBLE_USER"] = "t"
os.environ["CRUCIBLE_PASS"] = "t"
os.environ["CRUCIBLE_BYO_RUN_CEILING_USD"] = "1.00"

from crucible import byok, server  # noqa: E402
from crucible.ledger import Ledger  # noqa: E402
from crucible.offline import OfflineProvider  # noqa: E402
from crucible.providers import Tier  # noqa: E402

PASSED = 0
FAILED: list[str] = []

GOOD_KEY = "sk-test-goodkey-0123456789abcdefghijklmnop"
BAD_KEY = "sk-test-badkey-zzzzzzzzzzzzzzzzzzzzzzzzzzzz"


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


# ------------------------------------------------------------- stand-ins

PROBED: list[tuple[str, str]] = []


def fake_probe(base_url: str, api_key: str, **_) -> None:
    PROBED.append((base_url, api_key))
    if api_key != GOOD_KEY:
        raise ValueError("the provider rejected that key")


class FakeVisitorProvider(OfflineProvider):
    """OpenAIProvider's shape, the stand-in's answers, a real-looking cost.

    Books a small charge per call so session spend is observable, and can
    be told to fail once with a message that quotes the key, the way a 4xx
    body echoing an Authorization header would.
    """

    name = "openai"
    fail_with: str | None = None

    def __init__(self, api_key: str, models=None, *, base_url=None, **_):
        super().__init__(pace=0.0)
        self._key = api_key
        self.models = dict(models or {})
        self.base_url = base_url or ""

    def complete(self, system, user, tier, budget, *, max_output=2000, reasoning="low"):
        if FakeVisitorProvider.fail_with is not None:
            message, FakeVisitorProvider.fail_with = FakeVisitorProvider.fail_with, None
            raise RuntimeError(message)
        out = super().complete(system, user, tier, budget, max_output=max_output)
        # One cent-ish per call, priced through the table like a real one.
        budget.record(self.models.get(tier, "gpt-5-mini"), 4000, 400)
        return out


def wait_for(run_id: str, seconds: float = 120) -> dict:
    state = server.REGISTRY.get(run_id)
    deadline = time.time() + seconds
    while not state["finished"] and time.time() < deadline:
        time.sleep(0.1)
    return state


def all_text_under(root: Path) -> str:
    chunks = []
    for path in root.rglob("*"):
        if path.is_file():
            chunks.append(path.read_text(encoding="utf-8", errors="replace"))
    return "\n".join(chunks)


# ---------------------------------------------------------------- redactor

def redactor_checks() -> None:
    section("redactor")
    r = byok.Redactor([GOOD_KEY])
    check("the whole key is scrubbed",
          GOOD_KEY not in r.scrub(f"openai 401: bad Bearer {GOOD_KEY} token"))
    clipped = f"openai 401: {GOOD_KEY}"[:24]
    check("a clipped prefix of the key is scrubbed too",
          GOOD_KEY[:8] not in r.scrub(clipped), r.scrub(clipped))
    tail = f"...er {GOOD_KEY[-16:]} was rejected"
    check("a clipped suffix of the key is scrubbed too",
          GOOD_KEY[-8:] not in r.scrub(tail), r.scrub(tail))
    check("text without the key is untouched", r.scrub("hello there") == "hello there")
    nested = r.scrub_value({"a": [GOOD_KEY, {"b": f"x {GOOD_KEY} y"}], "n": 3})
    check("nested values are walked", GOOD_KEY not in json.dumps(nested) and nested["n"] == 3)
    check("a short secret is never registered (nothing to match on)",
          not byok.Redactor(["abc"]))


# ------------------------------------------------------------- validation

def validation_checks() -> None:
    section("attach: validation, allowlists, and what comes back")
    sid = "sid-validate"
    cookie = server.issue_session()
    status, body = server.admit_key({"provider": "openai", "api_key": BAD_KEY}, sid, cookie)
    check("a wrong key is a 400 with a sentence", status == 400 and "rejected" in body["error"], str(body))
    check("and nothing is held", server.KEYS.get(sid) is None)
    check("the probe was asked, at the openai base", PROBED[-1] == (byok.PROVIDERS["openai"]["base_url"], BAD_KEY))

    status, body = server.admit_key({"provider": "anthropic", "api_key": GOOD_KEY}, sid, cookie)
    check("an unknown provider is a 400", status == 400 and "openai or gemini" in body["error"])
    status, body = server.admit_key({"provider": "openai", "api_key": "short"}, sid, cookie)
    check("a key that does not read as one is a 400", status == 400)
    status, body = server.admit_key({"provider": "openai", "api_key": "sk-with-newline\nHeader: x" + "a" * 20}, sid, cookie)
    check("a key with a newline is a 400 (header injection shape)", status == 400)
    status, body = server.admit_key({"provider": "gemini", "api_key": GOOD_KEY,
                                     "models": {"planner": "gpt-5"}}, sid, cookie)
    check("a model off the provider's allowlist is a 400 naming it",
          status == 400 and "gpt-5" in body["error"], str(body))
    check("still nothing held", server.KEYS.get(sid) is None)

    probes = len(PROBED)
    status, body = server.admit_key({"provider": "gemini", "api_key": GOOD_KEY,
                                     "models": {"worker": "gemini-2.5-flash"}}, sid, cookie)
    check("a good key is a 200", status == 200, str(body))
    check("the probe went to the gemini base",
          len(PROBED) == probes + 1 and PROBED[-1][0] == byok.PROVIDERS["gemini"]["base_url"])
    check("the answer says attached, provider and models and never the key",
          body.get("attached") is True and body.get("provider") == "gemini"
          and body["models"]["worker"] == "gemini-2.5-flash"
          and body["models"]["planner"] == "gemini-3.1-pro-preview"
          and GOOD_KEY not in json.dumps(body), str(body))
    check("the ceiling defaults to the deployment's BYO ceiling", body.get("ceiling_usd") == 1.0)
    check("and spent starts at zero", body.get("spent_usd") == 0.0)
    status2 = server.byo_status(sid)
    check("GET status has the same shape, plus the offer, and no key",
          status2["attached"] and "providers" in status2 and GOOD_KEY not in json.dumps(status2))
    held = server.KEYS.get(sid)
    check("the table holds the key in memory with the session's expiry",
          held is not None and held["key"] == GOOD_KEY
          and held["expires"] == server.session_expiry(cookie))
    server.KEYS.detach(sid)
    check("detach forgets it", server.KEYS.get(sid) is None)

    section("attach: an expired session is a forgotten key")
    expired = f"{int(time.time()) - 5}.sig"
    server.KEYS.attach("sid-expired", {"provider": "openai", "key": GOOD_KEY,
                                        "base_url": "", "models": {}},
                       ceiling_usd=1.0, expires=server.session_expiry(expired))
    check("a read after expiry finds nothing", server.KEYS.get("sid-expired") is None)

    section("attach: ceiling clamp")
    check("a wild ceiling is clamped to the max", server.byo_ceiling(50) == server.BYO_RUN_CEILING_MAX_USD)
    check("a small ceiling is honoured", server.byo_ceiling(0.25) == 0.25)
    check("garbage falls back to the default", server.byo_ceiling("lots") == server.BYO_RUN_CEILING_USD)


# ------------------------------------------------------------------ runs

def run_checks(tmp: Path) -> None:
    server.RUNS = tmp / "runs"
    server.OpenAIProvider = FakeVisitorProvider
    sid = "sid-runner"
    cookie = server.issue_session()

    section("run: without a key, the deployment's own provider (offline here)")
    day_before = server.REGISTRY.day_spend()
    status, body = server.admit_run({"task": "full"}, sid)
    check("admitted", status == 200, str(body))
    state = wait_for(body["run_id"])
    started = next(e for e in state["events"] if e["kind"] == "run_started")
    check("provider_kind is offline in the stream", started.get("provider_kind") == "offline", str(started.get("provider_kind")))
    provider_event = next((e for e in state["events"] if e["kind"] == "provider"), None)
    check("a provider event names the kind before the run starts",
          provider_event is not None and provider_event["provider_kind"] == "offline")

    section("run: with a key, the visitor's provider")
    status, body = server.admit_key({"provider": "gemini", "api_key": GOOD_KEY}, sid, cookie)
    check("attached", status == 200, str(body))
    day_before = server.REGISTRY.day_spend()
    status, body = server.admit_run({"task": "full", "ceiling_usd": 50}, sid)
    check("admitted", status == 200, str(body))
    run_id = body["run_id"]
    state = wait_for(run_id)
    check("the run finished", state["finished"])
    started = next(e for e in state["events"] if e["kind"] == "run_started")
    check("run_started says provider_kind visitor", started.get("provider_kind") == "visitor", str(started))
    check("and the gemini default models",
          started.get("models") == {"planner": "gemini-3.1-pro-preview",
                                    "worker": "gemini-3.5-flash",
                                    "verifier": "gemini-3.1-pro-preview"}, str(started.get("models")))
    check("the ceiling asked for (50) was capped at the max",
          started.get("ceiling") == server.BYO_RUN_CEILING_MAX_USD, str(started.get("ceiling")))
    ledger = Ledger(server.RUNS / f"{run_id}.jsonl")
    head = ledger.entries()[0]["payload"]
    check("the ledger header records provider_kind visitor and the models",
          head.get("provider_kind") == "visitor" and head.get("models", {}).get("worker") == "gemini-3.5-flash")
    check("and the budget ceiling it ran under", head.get("budget_ceiling_usd") == server.BYO_RUN_CEILING_MAX_USD)
    check("the chain verifies with the scrub in place", ledger.verify() is None)
    finished = state["report"]
    check("the run spent something on the visitor's key", finished["spend_usd"] > 0, str(finished["spend_usd"]))
    check("the operator's day_spend did not move", server.REGISTRY.day_spend() == day_before,
          f"{day_before} -> {server.REGISTRY.day_spend()}")
    status_now = server.KEYS.status(sid)
    check("the session's spend is tracked", abs(status_now["spent_usd"] - finished["spend_usd"]) < 1e-6,
          str(status_now))

    section("run: the key is on disk nowhere")
    on_disk = all_text_under(tmp)
    check("the key is not under the runs directory or anywhere in the temp tree",
          GOOD_KEY not in on_disk and GOOD_KEY[:12] not in on_disk)
    check("nor in any event on the stream",
          GOOD_KEY[:12] not in json.dumps(state["events"]))

    section("run: a provider error that quotes the key is scrubbed everywhere")
    # Padded so the orchestrator's 200-character clip lands inside the key,
    # which is the case a whole-string replace would miss.
    lead = " openai 401: invalid Authorization: Bearer "
    FakeVisitorProvider.fail_with = "x" * (188 - len(lead)) + lead + GOOD_KEY + " rejected"
    out, err = io.StringIO(), io.StringIO()
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        status, body = server.admit_run({"task": "full"}, sid)
        state = wait_for(body["run_id"])
    check("the run still finished", state["finished"])
    events_text = json.dumps(state["events"])
    check("the failure reached the stream", "phase_failed" in events_text or "run_failed" in events_text)
    check("no 8-character prefix of the key is in any event",
          GOOD_KEY[:8] not in events_text, events_text[:400])
    check("the marker is where the key would have been", byok.REDACTED in events_text)
    ledger_text = (server.RUNS / f"{body['run_id']}.jsonl").read_text(encoding="utf-8")
    check("nor in the ledger file", GOOD_KEY[:8] not in ledger_text)
    check("and the ledger still verifies", Ledger(server.RUNS / f"{body['run_id']}.jsonl").verify() is None)
    check("nor in captured stdout or stderr", GOOD_KEY[:8] not in out.getvalue() + err.getvalue())

    section("run: with visitor keys switched off, the key is ignored")
    server.BYO_ENABLED = False
    status, body = server.admit_run({"task": "full"}, sid)
    state = wait_for(body["run_id"])
    started = next(e for e in state["events"] if e["kind"] == "run_started")
    check("the run fell back to the deployment's provider", started.get("provider_kind") == "offline")
    server.BYO_ENABLED = True
    server.KEYS.detach(sid)


# ---------------------------------------------------------------- routes

class Client:
    """Just enough of a browser: keeps the two cookies between requests."""

    def __init__(self, port: int):
        self.port = port
        self.cookies: dict[str, str] = {}

    def request(self, method: str, path: str, body=None, form=False):
        conn = http.client.HTTPConnection("127.0.0.1", self.port, timeout=30)
        headers = {}
        if self.cookies:
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        data = None
        if body is not None:
            if form:
                data = urlencode(body).encode()
                headers["Content-Type"] = "application/x-www-form-urlencoded"
            else:
                data = json.dumps(body).encode()
                headers["Content-Type"] = "application/json"
        conn.request(method, path, body=data, headers=headers)
        resp = conn.getresponse()
        raw = resp.read()
        for value in resp.msg.get_all("Set-Cookie") or []:
            name, _, rest = value.partition("=")
            val = rest.split(";", 1)[0]
            if "Max-Age=0" in value:
                self.cookies.pop(name, None)
            else:
                self.cookies[name] = val
        conn.close()
        try:
            parsed = json.loads(raw) if raw else {}
        except json.JSONDecodeError:
            parsed = {}
        return resp.status, parsed


def route_checks() -> None:
    section("routes: attach, status, forget and logout over HTTP")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    try:
        c = Client(httpd.server_address[1])
        status, body = c.request("GET", "/api/key")
        check("unauthenticated GET /api/key is 401", status == 401)
        status, _ = c.request("POST", "/api/login", {"user": "t", "password": "t"}, form=True)
        check("login sets both cookies", "crucible_session" in c.cookies and "crucible_sid" in c.cookies)
        sid = c.cookies["crucible_sid"]

        status, body = c.request("GET", "/api/key")
        check("fresh session: nothing attached, offer present",
              status == 200 and body["attached"] is False and body["enabled"] is True
              and "gemini" in body["providers"], str(body))
        status, body = c.request("POST", "/api/key", {"provider": "openai", "api_key": BAD_KEY})
        check("wrong key over HTTP is 400", status == 400 and "rejected" in body["error"])
        status, body = c.request("POST", "/api/key", {"provider": "openai", "api_key": GOOD_KEY,
                                                    "ceiling_usd": 0.5})
        check("good key over HTTP is 200 with status and no key",
              status == 200 and body["attached"] and body["provider"] == "openai"
              and body["ceiling_usd"] == 0.5 and GOOD_KEY not in json.dumps(body), str(body))
        check("the table holds it under the browser's sid", server.KEYS.get(sid) is not None)
        status, body = c.request("GET", "/api/tasks")
        check("/api/tasks carries the byo status", body.get("byo", {}).get("attached") is True
              and GOOD_KEY not in json.dumps(body))
        status, body = c.request("POST", "/api/key", "not an object")
        check("a non-object body is 400", status == 400)
        status, body = c.request("DELETE", "/api/key")
        check("DELETE forgets it", status == 200 and body["attached"] is False
              and server.KEYS.get(sid) is None)

        status, body = c.request("POST", "/api/key", {"provider": "gemini", "api_key": GOOD_KEY})
        check("re-attached", status == 200 and server.KEYS.get(sid) is not None)
        status, body = c.request("POST", "/api/logout")
        check("logout is a redirect to /login", status == 303)
        check("logout drops the key", server.KEYS.get(sid) is None)
        check("and expires the cookies", not c.cookies, str(c.cookies))
        status, body = c.request("GET", "/api/key")
        check("after logout the API is closed again", status == 401)
    finally:
        httpd.shutdown()
        httpd.server_close()


def hardening_checks() -> None:
    section("routes: the public face holds against a hostile client")
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.Handler)
    httpd.daemon_threads = True
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    port = httpd.server_address[1]
    try:
        c = Client(port)

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("HEAD", "/login")
        resp = conn.getresponse()
        head_body = resp.read()
        check("HEAD answers instead of 501", resp.status == 200)
        check("with the headers GET would send and no body",
              int(resp.getheader("Content-Length") or 0) > 0
              and head_body == b"")
        check("the server header keeps the Python version to itself",
              "python" not in (resp.getheader("Server") or "").lower(),
              str(resp.getheader("Server")))
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("HEAD", "/")
        resp = conn.getresponse()
        resp.read()
        check("HEAD on the root takes the same route as GET",
              resp.status in (200, 302), str(resp.status))
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("GET", "/")
        resp = conn.getresponse()
        page = resp.read().decode("utf-8", "replace")
        check("a stranger at the root gets the public page, status 200",
              resp.status == 200, str(resp.status))
        check("and it carries the evidence and the description tag",
              "docs/evidence" in page and 'name="description"' in page)
        check("and the sign-in stays one link away", 'href="/login"' in page)
        conn.close()

        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("GET", "/somewhere-else")
        resp = conn.getresponse()
        resp.read()
        check("every other unauthenticated path still walks to the gate",
              resp.status == 302
              and resp.getheader("Location") == "/login", str(resp.status))
        conn.close()

        c2 = Client(port)
        c2.request("POST", "/api/login", {"user": "t", "password": "t"},
                   form=True)
        conn = http.client.HTTPConnection("127.0.0.1", port, timeout=30)
        conn.request("GET", "/", headers={"Cookie": "; ".join(
            f"{k}={v}" for k, v in c2.cookies.items())})
        resp = conn.getresponse()
        page = resp.read().decode("utf-8", "replace")
        check("signed in, the root is the live app rather than the cover",
              resp.status == 200 and "docs/evidence" not in page
              and 'id="stage"' in page, str(resp.status))
        conn.close()

        status, _ = c.request("GET", "/static/../index.html")
        check("a static name that climbs out of static/ is refused",
              status == 404, str(status))
        status, _ = c.request("GET", "/static/app.css")
        check("an honest static name still serves", status == 200, str(status))

        expires = str(int(time.time()) + 3600)
        c.cookies["crucible_session"] = f"{expires}." + "0" * 64
        status, _ = c.request("GET", "/api/key")
        check("a forged session signature is refused", status == 401)

        past = str(int(time.time()) - 10)
        stale = hmac.new(server.SECRET.encode(), past.encode(),
                         hashlib.sha256).hexdigest()
        c.cookies["crucible_session"] = f"{past}.{stale}"
        status, _ = c.request("GET", "/api/key")
        check("an expired cookie with a true signature is refused",
              status == 401)
        c.cookies.clear()

        status, _ = c.request("POST", "/api/login",
                              {"user": "ünïcode", "password": "x"},
                              form=True)
        check("a non-ASCII sign-in is refused rather than crashed",
              status == 303 and "crucible_session" not in c.cookies,
              str(status))
        status, _ = c.request("POST", "/api/login",
                              {"user": "t", "password": "t"}, form=True)
        check("the true pair still signs in after the bytes comparison",
              status == 303 and "crucible_session" in c.cookies)
        c.request("POST", "/api/logout")

        with socket.create_connection(("127.0.0.1", port), timeout=30) as raw:
            raw.sendall(b"POST /api/login HTTP/1.1\r\n"
                        b"Host: x\r\nContent-Length: abc\r\n\r\n")
            reply = raw.recv(1000).decode("latin-1", "replace")
        check("a malformed Content-Length is a 400, not a dropped connection",
              " 400 " in reply.split("\r\n")[0], reply.split("\r\n")[0])
    finally:
        httpd.shutdown()
        httpd.server_close()


def main() -> None:
    byok.probe_models = fake_probe
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        redactor_checks()
        validation_checks()
        run_checks(tmp)
        route_checks()
        hardening_checks()
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
