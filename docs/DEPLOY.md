<!-- Research document. Compiled 2026-08-16 against live provider docs. -->

> **What this is.** The deployment path for this application: Railway from a
> GitHub repository, behind a Cloudflare subdomain, serving a Server-Sent
> Events stream.
>
> Two limits shape the design and are worth knowing before reading the code.
> Railway closes any HTTP request at **15 minutes**, and any request that has
> been silent for **5**. Cloudflare's proxy adds an idle timeout around 100
> seconds and has been observed buffering `text/event-stream` despite not
> compressing it. `crucible/server.py` answers both: a heartbeat every 15
> seconds, and streams retired at 13 minutes so the browser reconnects on our
> schedule rather than on an error.

---

# Deploying a stdlib Python SSE app to Railway behind `crucible.flow-through.com.au`

Researched 2026-08-16 against current Railway and Cloudflare docs. Headline: this works, but two limits shape the design — **Railway kills any HTTP request at 15 minutes** (and at 5 minutes of silence), and **Cloudflare's proxy adds a ~100–125 s idle timeout plus a buffering risk on `text/event-stream`**. The recommended configuration is a **DNS-only (grey cloud)** record for this subdomain, which removes the Cloudflare layer from the SSE path entirely, plus a heartbeat and client reconnect to live inside Railway's 15-minute ceiling.

---

## 1. Railway deployment from a GitHub repo

### How Railway detects a Python app

Railway's default builder is **Railpack** (successor to Nixpacks; Nixpacks is in maintenance mode). New services default to Railpack. Railpack detects Python when any of these exist in the root directory:

- one of `main.py`, `app.py`, `start.py`, `bot.py`, `hello.py`, `server.py`
- `requirements.txt`
- `pyproject.toml`
- `Pipfile`

Python version resolution priority:

1. `RAILPACK_PYTHON_VERSION` environment variable
2. mise-compatible version files: `.python-version`, `.tool-versions`, `mise.toml`
3. `runtime.txt`
4. `Pipfile`
5. default `3.13.2`

Default start command: framework-specific detection first (FastAPI, Django, Flask), then the first main file found in the order `main.py`, `app.py`, `start.py`, `bot.py`, `hello.py`, `server.py`.

**Practical consequence for a stdlib app:** a bare `main.py` at the repo root is technically enough. Do not rely on that — pin the version and the start command explicitly.

### Which files you need

| File | Needed? | Why |
|---|---|---|
| `railway.json` | **Yes** (recommended) | Pins builder, start command, healthcheck, restart policy. Config in code overrides dashboard settings. |
| `.python-version` | **Yes** | Pins the interpreter. Highest-priority file source. |
| `requirements.txt` | Optional but recommended | Empty file is fine; makes detection unambiguous and documents zero-dependency intent. |
| `runtime.txt` | No | Legacy Heroku-style; `.python-version` wins and is the modern path. |
| `Procfile` | No | Supported but explicitly deprecated: *"Railpack automatically detects commands defined in Procfiles. Although this is not recommended and specifying the start command directly in your service settings is preferred."* |
| `nixpacks.toml` | No | Only relevant if you deliberately switch the builder back to Nixpacks. |
| `railpack.json` | Only if you need custom build steps | Not needed here — no dependencies to install. |
| `Dockerfile` | No | Only if you set `"builder": "DOCKERFILE"`. |

### `railway.json`

```json
{
  "$schema": "https://railway.com/railway.schema.json",
  "build": {
    "builder": "RAILPACK",
    "watchPatterns": ["**/*.py", "static/**", "requirements.txt"]
  },
  "deploy": {
    "startCommand": "python -u main.py",
    "healthcheckPath": "/healthz",
    "healthcheckTimeout": 60,
    "restartPolicyType": "ON_FAILURE",
    "restartPolicyMaxRetries": 10
  }
}
```

Full key set available (from the config-as-code reference):

- `build`: `builder` (`RAILPACK` default, or `DOCKERFILE`), `buildCommand`, `watchPatterns`, `dockerfilePath`, `railpackVersion`
- `deploy`: `startCommand`, `preDeployCommand` (array), `healthcheckPath`, `healthcheckTimeout` (seconds), `restartPolicyType` (`ON_FAILURE` | `ALWAYS` | `NEVER`), `restartPolicyMaxRetries`, `cronSchedule`, `overlapSeconds`, `drainingSeconds`, `multiRegionConfig`
- `environments`: per-environment overrides, e.g. `"environments": { "staging": { "deploy": { "startCommand": "..." } } }`

`-u` in the start command forces unbuffered stdio so your `print()` lines reach Railway's log stream immediately. Setting `PYTHONUNBUFFERED=1` as a service variable does the same thing.

### `.python-version`

```
3.13
```

(Use `3.11` if you have a reason to. Anything ≥3.11 satisfies the requirement; 3.13 is Railpack's default lane and gets the best cache hits.)

### `requirements.txt`

```
# Standard library only — no third-party dependencies.
```

### `.gitignore`

```
__pycache__/
*.pyc
.venv/
.env
```

### The app — `main.py`

This is a complete, working stdlib server: static files, an SSE endpoint with explicit chunked framing, heartbeats, a self-imposed stream lifetime under Railway's cap, clean SIGTERM handling, and correct broken-pipe behaviour.

```python
#!/usr/bin/env python3
"""Crucible — stdlib-only static file + Server-Sent Events server.

Design constraints (Railway edge proxy):
  * HTTP requests are closed after 15 minutes even while streaming.
  * HTTP requests are closed after 5 minutes with no data transferred.
So: heartbeat well under 5 min, and retire each stream before 15 min so the
browser's EventSource reconnects on our schedule rather than on an error.
"""

from __future__ import annotations

import json
import os
import queue
import signal
import threading
import time
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HOST = "0.0.0.0"                       # Railway requires 0.0.0.0, not 127.0.0.1
PORT = int(os.environ.get("PORT", "8080"))
STATIC_DIR = Path(__file__).resolve().parent / "static"

HEARTBEAT_SECONDS = 15                 # comfortably under every proxy idle timeout
MAX_STREAM_SECONDS = 13 * 60           # retire before Railway's 15-minute ceiling
SUBSCRIBER_QUEUE_DEPTH = 256


class Hub:
    """Fan-out of events to every connected SSE subscriber."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()
        self._next_id = 0

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=SUBSCRIBER_QUEUE_DEPTH)
        with self._lock:
            self._subscribers.add(q)
        return q

    def unsubscribe(self, q: queue.Queue) -> None:
        with self._lock:
            self._subscribers.discard(q)

    def publish(self, event: str, data: object) -> None:
        with self._lock:
            self._next_id += 1
            frame = (self._next_id, event, data)
            stalled = []
            for q in self._subscribers:
                try:
                    q.put_nowait(frame)
                except queue.Full:
                    stalled.append(q)          # slow client: drop it, it will reconnect
            for q in stalled:
                self._subscribers.discard(q)


HUB = Hub()


class Handler(SimpleHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "crucible"
    sys_version = ""

    # ---- routing -----------------------------------------------------------

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/healthz":
            self.send_json({"ok": True})
        elif path == "/events":
            self.serve_sse()
        else:
            super().do_GET()

    # ---- helpers -----------------------------------------------------------

    def send_json(self, payload: object) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def write_chunk(self, data: bytes) -> None:
        """One HTTP/1.1 chunked-transfer chunk, flushed immediately."""
        self.wfile.write(b"%X\r\n" % len(data) + data + b"\r\n")
        self.wfile.flush()

    # ---- SSE ---------------------------------------------------------------

    def serve_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        # no-cache stops proxy/browser caching; no-transform stops Cloudflare
        # compressing or otherwise rewriting the body.
        self.send_header("Cache-Control", "no-cache, no-store, no-transform")
        self.send_header("Connection", "keep-alive")
        # nginx and most nginx-derived proxies honour this to disable buffering.
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Transfer-Encoding", "chunked")
        self.end_headers()

        q = HUB.subscribe()
        deadline = time.monotonic() + MAX_STREAM_SECONDS
        try:
            # Priming bytes: forces the response head plus a body byte out of
            # every intermediary before any real event exists.
            self.write_chunk(b": connected\n\n")
            self.write_chunk(b"retry: 3000\n\n")

            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    self.write_chunk(b": stream-retire\n\n")
                    break
                try:
                    event_id, event, data = q.get(
                        timeout=min(HEARTBEAT_SECONDS, remaining)
                    )
                except queue.Empty:
                    self.write_chunk(b": keepalive\n\n")
                    continue

                payload = json.dumps(data)          # single line, safe for `data:`
                frame = f"id: {event_id}\nevent: {event}\ndata: {payload}\n\n"
                self.write_chunk(frame.encode("utf-8"))
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass                                     # client went away
        finally:
            HUB.unsubscribe(q)
            try:
                self.wfile.write(b"0\r\n\r\n")       # terminal chunk
                self.wfile.flush()
            except OSError:
                pass
            self.close_connection = True

    # ---- logging -----------------------------------------------------------

    def log_message(self, fmt: str, *args) -> None:
        print("%s - %s" % (self.address_string(), fmt % args), flush=True)


class Server(ThreadingHTTPServer):
    daemon_threads = True          # one thread per open SSE stream
    allow_reuse_address = True


def ticker(stop: threading.Event) -> None:
    """Replace with the real event source. Present so the stream is never idle."""
    while not stop.wait(5.0):
        HUB.publish("tick", {"t": time.time()})


def main() -> None:
    STATIC_DIR.mkdir(parents=True, exist_ok=True)
    handler = partial(Handler, directory=str(STATIC_DIR))
    httpd = Server((HOST, PORT), handler)

    stop = threading.Event()

    def shutdown(_signum, _frame) -> None:
        stop.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)   # Railway sends SIGTERM on redeploy
    signal.signal(signal.SIGINT, shutdown)

    threading.Thread(target=ticker, args=(stop,), daemon=True).start()

    print(f"crucible listening on {HOST}:{PORT}", flush=True)
    try:
        httpd.serve_forever(poll_interval=0.5)
    finally:
        httpd.server_close()


if __name__ == "__main__":
    main()
```

### Client side — `static/index.html`

```html
<!doctype html>
<meta charset="utf-8">
<title>Crucible</title>
<pre id="out"></pre>
<script>
  const out = document.getElementById('out');
  let es;
  function connect() {
    es = new EventSource('/events');
    es.addEventListener('tick', (e) => {
      out.textContent += e.data + '\n';
    });
    es.onerror = () => {
      // EventSource retries on its own using the server's `retry:` value.
      // This handler exists only for visibility.
      out.textContent += '[reconnecting]\n';
    };
  }
  connect();
</script>
```

`EventSource` reconnects automatically when the stream closes, and replays the last `id:` it saw in a `Last-Event-ID` request header. That is what makes the 13-minute retirement invisible to the user. If you want resume-from-offset, read `self.headers.get("Last-Event-ID")` at the top of `serve_sse` and replay from there.

### Deploy steps

1. Push the repo to GitHub.
2. Railway dashboard → **New Project** → **Deploy from GitHub repo** → pick the repo. (Or `railway link` then `railway up` from the CLI.)
3. Service **Settings → Source**: confirm the branch and, for a monorepo, set **Root Directory**. The default root directory is `/`.
4. Railway builds with Railpack and starts `python -u main.py`.
5. Service **Settings → Networking → Generate Domain** to get a `*.up.railway.app` URL and confirm it serves before touching DNS.
6. Watch logs with `railway logs` (build logs: `railway logs --build`).

---

## 2. PORT — how Railway assigns it, what your app must do

Two rules, both from Railway's own troubleshooting doc:

> "Your web server should bind to the host `0.0.0.0` and listen on the port specified by the `PORT` environment variable, which Railway automatically injects into your application."

And on generation:

> As long as you have not defined a `PORT` variable, Railway will provide and expose one for you.

So:

- **Bind `0.0.0.0`, never `127.0.0.1`/`localhost`.** Binding to loopback is the number-one cause of `Application failed to respond` (a 502 from Railway's edge proxy — it connected to the container but nothing answered).
- **Read `PORT` from the environment**, with a local-dev fallback: `int(os.environ.get("PORT", "8080"))`.
- **Do not hardcode a port** and do not set `PORT` yourself unless you also set the domain's **target port** to match. If you set `PORT` manually, the domain's target port in service settings must equal it, or you get a 502.
- Caveat if you later add a second service: `${{ api.PORT }}` reference variables resolve only to a *manually set* `PORT` service variable. They do not resolve to the auto-injected runtime `PORT`.

---

## 3. Custom domain in Railway + the Cloudflare DNS record

### In Railway

1. Service → **Settings** → **Public Networking** → **+ Custom Domain**.
2. Enter `crucible.flow-through.com.au`.
3. Set the **target port** if prompted — leave it on the port your app listens on. With auto-injected `PORT` you normally leave this alone.
4. Railway returns **two records**, and both are mandatory:
   - a **CNAME** whose value looks like `g05ns7.up.railway.app` (a random per-domain subdomain of `up.railway.app`, not your service's generated domain)
   - a **TXT** record for ownership verification, name in the `_railway-verify.*` shape, content starting `railway-verify=`

   Railway's docs are explicit: *"Both records are required - the domain will not verify with only the `CNAME` in place."* With the CNAME alone you get a 404 from Railway's edge even after DNS resolves.

Domain limits: Hobby plan is 2 custom domains per service, Pro is 20 by default.

CLI equivalent: `railway domain crucible.flow-through.com.au`, then `railway domain status crucible.flow-through.com.au` to see the DNS values.

### In Cloudflare (`flow-through.com.au` zone → DNS → Records)

**Record 1 — CNAME**

| Field | Value |
|---|---|
| Type | `CNAME` |
| Name | `crucible` |
| Target | the exact value Railway showed, e.g. `g05ns7.up.railway.app` |
| Proxy status | **DNS only (grey cloud)** — recommended, see below |
| TTL | Auto |

**Record 2 — TXT**

| Field | Value |
|---|---|
| Type | `TXT` |
| Name | exactly what Railway showed (a `_railway-verify…` name scoped to `crucible`) |
| Content | exactly what Railway showed, e.g. `railway-verify=…` |
| TTL | Auto |

TXT records are never proxied, so there is no cloud toggle to worry about there.

Then return to Railway and wait for the green checkmark. DNS changes can take up to 72 hours to propagate worldwide, though Cloudflare is usually seconds.

### Orange cloud: off or on?

**Recommendation for this subdomain: OFF (grey cloud / DNS only.)**

Reasons, in order of weight:

1. **Cloudflare's own 524 guidance** says to move long-running processes *"behind a subdomain not proxied (DNS-only, grey clouded) in the Cloudflare DNS app."* An SSE endpoint is exactly that shape.
2. It removes Cloudflare's idle timeout from the equation entirely, leaving only Railway's limits to design around.
3. It removes the SSE buffering risk described in §4, which has recurring, unresolved community reports and no supported fix on Free/Pro.
4. Certificate issuance just works — Railway provisions Let's Encrypt directly against the origin with no ACME challenge going through Cloudflare's edge.

Cost of grey cloud: no Cloudflare WAF, caching, analytics, or IP masking on this hostname, and the Railway origin hostname is visible in DNS. For an internal/tool subdomain that is normally a fine trade.

**If you do want the orange cloud on**, these are hard requirements:

- **SSL/TLS encryption mode must be `Full`.** Railway states this outright: *"If you have proxying enabled on Cloudflare (the orange cloud), you MUST set your SSL/TLS settings to Full."*
  - `Flexible` breaks it: Cloudflare sends plain HTTP to Railway, Railway redirects HTTP→HTTPS, and you get an infinite redirect loop (`ERR_TOO_MANY_REDIRECTS`).
  - `Full (Strict)` breaks it intermittently: during certificate renewal Railway briefly serves a `*.up.railway.app` certificate, which Strict rejects as a hostname mismatch. `Full` tolerates that while still encrypting the hop.
  - Note this mode is zone-wide unless you scope it with a Configuration Rule — check it does not break other proxied hostnames on `flow-through.com.au`.
- **If the certificate hangs on "Validating Challenges"**, toggle the proxy to grey cloud, wait for Railway's green checkmark, then flip it back to orange. This takes Cloudflare out of the ACME path.
- **CAA records**: if the zone has any, Let's Encrypt must be permitted — `flow-through.com.au. CAA 0 issue "letsencrypt.org"`.

---

## 4. Does SSE survive both proxies?

### Railway's proxy: yes, with documented limits

Railway publishes explicit SSE guidance and edge-proxy specs.

- Protocol support: HTTP/1.1 and HTTP/2; websockets over HTTP/1.1.
- **"HTTP requests can run for up to 15 minutes if data keeps transferring"** and are **"otherwise closed after 5 minutes with no data transferred."**
- Websockets are exempt from those limits and can stay open indefinitely. **SSE is not exempt** — Railway's SSE guide states SSE runs "up to 15 minutes with keep-alive heartbeats, closed after 5 minutes with no data transferred."
- Railway's own instruction: **"Send a heartbeat (such as an SSE comment line) at least every 5 minutes, and reconnect when a stream outlives the 15-minute cap."**
- Idle HTTP/1.1 connections are closed after 60 seconds between requests (does not apply to HTTP/2 or websockets).
- Max combined header size 32 KB.
- Railway adds `X-Real-IP`, `X-Forwarded-Proto` (always `https`), `X-Forwarded-Host`, `X-Railway-Edge`, `X-Request-Start`, `X-Railway-Request-Id`. Railway staff have stated the only response header Railway modifies is `Server`.

Railway's recommended headers match what the code above sends: `Content-Type: text/event-stream`, `Cache-Control: no-cache`, `Connection: keep-alive`. Railway community guidance adds: explicitly flush headers, use `text/event-stream; charset=UTF-8` (not `text/plain`), and check for a framework buffering layer — with raw `http.server` there is no framework layer, but you must call `self.wfile.flush()` yourself after every write, which the code does.

**Design implication:** the 15-minute cap is not avoidable. Either retire the stream yourself just under it (what `MAX_STREAM_SECONDS = 13 * 60` does) and let `EventSource` reconnect, or accept an error-flavoured drop at 15 minutes. Retiring deliberately is cleaner: you control the `retry:` interval and can replay from `Last-Event-ID`.

### Cloudflare's proxy: works, with two real risks

**Risk A — idle timeout (524).** Cloudflare's proxy read timeout is the trigger for error 524: *"Cloudflare successfully connected to the origin web server, but the origin did not provide an HTTP response before the default 125 seconds."* Older docs and most third-party writeups say 100 seconds; Cloudflare's current page says 125 s read / 30 s write. Treat the effective budget as **100 seconds, idle-based** — it is measured between data events, not as a total connection cap, so a stream that keeps emitting bytes survives indefinitely. Only Enterprise can raise it (up to 6,000 s via Cache Rules or the zone-settings API). A heartbeat every 15 s puts you six times inside the smallest reported figure.

Corollary: **send bytes before you have data.** The classic failure is an endpoint that does work first and only then emits the SSE head — the silent gap trips the timeout. The code above writes `: connected` immediately after `end_headers()`.

**Risk B — buffering.** This is the one to actually test rather than assume.

- Cloudflare's stated default is to stream: each packet is sent as it becomes available. The **Response Buffering** toggle (Network tab) is **Enterprise-only and off by default**, so on Free/Pro there is nothing to switch.
- **`text/event-stream` is not in Cloudflare's list of compressible content types**, so Brotli/gzip should not be applied to your stream — one common buffering cause is off the table.
- Despite that, there are repeated, current community reports of Cloudflare holding `text/event-stream` responses until roughly 100 KB accumulates, with SSE working correctly the moment the orange cloud is turned off. `X-Accel-Buffering: no` is the standard mitigation and *"is typically enough to fix issues in practice, even for third party proxies outside of your control"* — but Cloudflare does not document honouring it, and reports on whether it helps are mixed.

Headers that maximise your odds through both proxies (all present in the code above):

```
Content-Type: text/event-stream; charset=utf-8
Cache-Control: no-cache, no-store, no-transform
Connection: keep-alive
X-Accel-Buffering: no
Transfer-Encoding: chunked
```

`no-transform` is the documented Cloudflare lever: *"If you do not want a particular response from your origin to be encoded with Brotli/Gzip when delivered to website visitors, you can disable this by including a `cache-control: no-transform` HTTP header in the response from your origin web server."* It also disables Polish and JavaScript Detections injection. It must come from the origin; it cannot be set client-side.

**Verification command** — run this against both the Railway domain and the custom domain and compare. If events arrive one-per-interval, streaming is intact; if they arrive in a burst, something is buffering.

```bash
curl -N -sS -D - https://crucible.flow-through.com.au/events \
  -H 'Accept: text/event-stream' \
  | ts '[%H:%M:%.S]'      # or: | while IFS= read -r l; do echo "$(date +%T) $l"; done
```

`-N` disables curl's own buffering. Check the response headers for an unexpected `Content-Encoding` or a `cf-cache-status` you did not expect.

---

## 5. Environment variables and secrets in Railway

**Dashboard (primary path):** service → **Variables** tab → **New Variable**, or use the **RAW Editor** to paste the contents of a `.env` or JSON file in one go. Railway also scans the repo for `.env`, `.env.example`, `.env.local`, `.env.production`, and any `.env.<suffix>` file and suggests those variables at deploy time.

**Sealed variables** for real secrets: 3-dot menu on a variable → seal. *"When a variable is sealed, its value is provided to builds and deployments but is never visible in the UI."* One-way — *"Sealed variables cannot be un-sealed."*

**Shared variables:** Project Settings → Shared Variables → pick the environment → Add. Reference with `${{ shared.VARIABLE_KEY }}`.

**Reference syntax:**
- shared: `${{ shared.VARIABLE_KEY }}`
- another service: `${{ ServiceName.VAR }}` — e.g. `DATABASE_URL=${{ Clickhouse.DATABASE_URL }}`
- same service: `${{ VARIABLE_NAME }}`

**Railway-provided variables** you can read: `PORT`, `RAILWAY_PUBLIC_DOMAIN`, `RAILWAY_PRIVATE_DOMAIN`, `RAILWAY_ENVIRONMENT`, `RAILWAY_TCP_PROXY_PORT`, and others.

**CLI:** the docs show `railway variable list`, `railway variable set KEY=value`, `railway variable delete KEY`. Older builds used `railway variables --set "KEY=VALUE"`. Run `railway variables --help` against your installed version before scripting it — the noun has been both singular and plural across releases. `railway run <cmd>` executes locally with the Railway environment injected. For CI, authenticate with `RAILWAY_TOKEN` (project-scoped) or `RAILWAY_API_TOKEN` (account-scoped) rather than interactive login.

Worth setting for this app:

```
PYTHONUNBUFFERED=1
```

Changing a variable triggers a redeploy, which drops every open SSE stream. Batch your variable edits.

---

## 6. Gotchas, ranked by how likely they are to bite

1. **The 15-minute wall is Railway's, not Cloudflare's, and it applies even to a perfectly healthy stream.** Grey-clouding does not help. Retire streams at ~13 minutes and lean on `EventSource` auto-reconnect plus `id:` / `Last-Event-ID`.
2. **Silence over ~100 s trips Cloudflare (524); silence over 5 min trips Railway.** One heartbeat design satisfies both — a `: keepalive\n\n` comment line every 15 s. Emit the first bytes immediately, before any work.
3. **Threading is mandatory.** A plain `HTTPServer` serialises requests, so one open SSE stream blocks every other request including Railway's healthcheck, which then fails the deploy. Use `ThreadingHTTPServer` with `daemon_threads = True`. Each stream costs a thread — budget accordingly and cap concurrent subscribers if this is public-facing.
4. **Framing.** With `protocol_version = "HTTP/1.1"` and no `Content-Length`, the response is ambiguous unless you supply chunked encoding yourself. `http.server` does not add it. The code writes real chunks and a terminal `0\r\n\r\n`. The alternative — `Connection: close` with no length, letting connection-close delimit the body — is also RFC-valid but throws away keep-alive and confuses some proxies. Chunked is the safer default.
5. **Client disconnects raise.** `BrokenPipeError` / `ConnectionResetError` on `wfile.write` is normal, not an error condition. Catch it, unsubscribe, return. Uncaught, it floods your logs and can leak subscriber queues.
6. **Healthcheck path must be instant.** If `healthcheckPath` points at anything that blocks, the deploy fails. `/healthz` returns a fixed JSON body with a `Content-Length` and never touches the hub.
7. **Replicas break fan-out.** If you scale past one replica, a client connected to replica A never sees an event published on replica B. Either stay at one replica or move fan-out to shared state (Redis, Postgres LISTEN/NOTIFY).
8. **Check the service's sleep/serverless setting.** A service configured to sleep on inactivity is a poor fit for long-lived streams — verify it is off in service settings.
9. **Cloudflare SSL mode is zone-wide.** Flipping `flow-through.com.au` to `Full` for Railway's sake affects every other proxied hostname on the zone. Confirm nothing else depends on `Flexible` or `Full (Strict)` before changing it. Grey-clouding `crucible` sidesteps this entirely.
10. **The TXT record is not optional.** CNAME-only gives you a Railway edge 404 that looks exactly like a broken deploy. If the domain shows "waiting for DNS" for hours, re-check the TXT name and value character-for-character.
11. **SIGTERM.** Railway sends SIGTERM on redeploy. Without a handler, streams are severed hard. The signal handler plus `drainingSeconds` in `railway.json` gives clients a clean close and a prompt reconnect.
12. **Header budget.** Railway caps combined headers at 32 KB. Irrelevant here, but relevant if you ever pass large auth tokens on the SSE request.

---

## Sources

- [Railway — Config as Code](https://docs.railway.com/reference/config-as-code) and [Config as Code reference](https://docs.railway.com/config-as-code/reference)
- [Railway — Build Configuration](https://docs.railway.com/builds/build-configuration)
- [Railway — Railpack](https://docs.railway.com/reference/nixpacks) / [Why We're Moving on From Nix](https://blog.railway.com/p/introducing-railpack)
- [Railpack — Python provider](https://railpack.com/languages/python), [config file](https://railpack.com/config/file), [Procfile](https://railpack.com/config/procfile/)
- [Railway — Application Failed to Respond](https://docs.railway.com/networking/troubleshooting/application-failed-to-respond)
- [Railway — Public Networking](https://docs.railway.com/public-networking)
- [Railway — Specs & Limits](https://docs.railway.com/networking/public-networking/specs-and-limits)
- [Railway — Choose Between SSE and WebSockets](https://docs.railway.com/guides/sse-vs-websockets)
- [Railway — Working with Domains](https://docs.railway.com/networking/domains/working-with-domains)
- [Railway — Troubleshooting SSL](https://docs.railway.com/networking/troubleshooting/ssl)
- [Railway — Using Variables](https://docs.railway.com/variables)
- [Railway — CLI](https://docs.railway.com/cli)
- [Cloudflare — Error 524](https://developers.cloudflare.com/support/troubleshooting/http-status-codes/cloudflare-5xx-errors/error-524/)
- [Cloudflare — Content compression](https://developers.cloudflare.com/speed/optimization/content/compression/)
- [Cloudflare — Response Buffering](https://developers.cloudflare.com/network/response-buffering/)
- [Cloudflare Community — Using Server Sent Events (SSE) with Cloudflare Proxy](https://community.cloudflare.com/t/using-server-sent-events-sse-with-cloudflare-proxy/656279)
- [Cloudflare Community — SSE endpoint breaks, Cloudflare buffers text/event-stream](https://community.cloudflare.com/t/sse-endpoint-breaks-after-recent-update-cloudflare-buffers-text-event-stream-desp/810790)
- [Railway Station — Streaming responses from a Node server](https://station.railway.com/questions/streaming-responses-from-a-node-server-d485c0b8)
- [Python docs — http.server](https://docs.python.org/3/library/http.server.html)
- [SSE Timeout Mitigation Guide (Cloudflare/ALB)](https://smartscope.blog/en/Infrastructure/sse-timeout-mitigation-cloudflare-alb/)