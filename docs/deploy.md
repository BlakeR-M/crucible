# Deploying the hosted demo to Railway

The runbook for `crucible.flow-through.com.au`. Everything here was checked
against `crucible/server.py`, `main.py` and `railway.json` in this repository;
the reasoning behind each choice (Railway's request limits, why the DNS record
is grey-clouded, how the stream survives) is in
[`deploy-research.md`](deploy-research.md).

## What the server needs

`python -u main.py` starts `crucible.server.serve()`, a standard-library
`ThreadingHTTPServer` on `0.0.0.0`. It reads:

| variable | required | notes |
|---|---|---|
| `CRUCIBLE_USER`, `CRUCIBLE_PASS` | yes | Demo sign-in. There is no built-in pair: with either unset the process prints why and exits with code 2, so a deployment that forgot them stays down rather than open. |
| `OPENAI_API_KEY` | yes, for paid runs | Read from the environment when a run or conversation starts. Absent, the server still boots and every run fails with a plain "no OPENAI_API_KEY" event. |
| `PORT` | set by Railway | Defaults to `8420` locally. |
| `CRUCIBLE_SECRET` | recommended | Signs the session cookie. Unset, a random value is drawn per boot and every deploy signs everyone out. Any long random string. |
| `CRUCIBLE_RUN_CEILING_USD` | no | Spend ceiling for one run, default `0.60`. |
| `CRUCIBLE_DAILY_CEILING_USD` | no | Spend ceiling per UTC day, default `8.00`. |
| `CRUCIBLE_CHAT_CEILING_USD` | no | Spend ceiling per visitor conversation, default `0.40`. |
| `CRUCIBLE_OFFLINE` | no | `1` runs the whole thing with the stand-in model and spends nothing. Useful for a first deploy to check the plumbing before the key goes in. |
| `CRUCIBLE_REPO_HOSTS` | no | Comma-separated hosts a visitor may name in the repository field, default `github.com,gitlab.com`. |
| `CRUCIBLE_REPO_MAX_MB` | no | Size cap on a cloned repository, default `50`. |
| `CRUCIBLE_REPO_MAX_FILES` | no | File cap on a cloned repository, default `5000`. |
| `CRUCIBLE_REPO_CLONE_TIMEOUT_S` | no | Seconds a clone may run, default `120`. Railway's 15 minute request limit is nowhere near this; the cap is there so a slow host cannot hold a run slot. |
| `PYTHONUNBUFFERED` | recommended | `1`, so logs stream. `-u` on the start command already covers this; setting both is harmless. |

Health check: `GET /healthz` returns 200 without a session. The `railway.json`
in the repository already sets the start command, the health-check path with a
60 second timeout, and restart on failure.

`git` must be on the container's PATH for the repository field to work.
Railpack's Python image carries it; a run against a URL on an image without
it fails in a sentence ("git is not on the PATH") rather than hanging. Each
URL run clones into a private directory under the container's temp space and
removes it when the run ends, so disk use is bounded by the size cap times
the concurrency cap.

Runs write ledgers to `runs/` inside the container. That directory is
ephemeral on Railway; the demo is fine with that, and the ledger download link
in the interface serves from the live process.

## Steps

Blake runs these. `railway login` opens a browser and is the one step nobody
else can do.

1. **Create the service from GitHub.** Railway dashboard, New Project, Deploy
   from GitHub repo, pick `BlakeR-M/crucible`, branch `main`. Railpack detects
   Python from `main.py`, `pyproject.toml` and `.python-version` (3.11).
   Nothing to install: `requirements.txt` is empty on purpose.

   CLI alternative, from the repository root:
   ```
   railway login
   railway init            # or `railway link` to an existing project
   railway up
   ```

2. **Set the variables** (service, Variables tab, or the RAW editor). The
   minimum for a live demo:
   ```
   CRUCIBLE_USER=<demo user>
   CRUCIBLE_PASS=<demo password>
   CRUCIBLE_SECRET=<long random string>
   OPENAI_API_KEY=<key>
   PYTHONUNBUFFERED=1
   ```
   Seal `OPENAI_API_KEY`, `CRUCIBLE_PASS` and `CRUCIBLE_SECRET` (three-dot
   menu on the variable) so they stay out of the UI. Each variable change
   redeploys, so set them all in one go. Check the CLI's noun with
   `railway variables --help` before scripting this; it has been singular and
   plural across releases.

   For a plumbing check before spending anything, add `CRUCIBLE_OFFLINE=1`
   and remove it once the interface loads and a run completes.

3. **Confirm the boot.** `railway logs` should show
   `crucible listening on :<port>` and the sign-in name. A log line starting
   `crucible refuses to start` means a credential variable is missing; the
   deploy restarts up to ten times (the policy in `railway.json`) and then
   stays down until the variable is set.

4. **Add the domain.** Service, Settings, Public Networking, Custom Domain,
   `crucible.flow-through.com.au`. Railway hands back two records and both are
   needed: a `CNAME` target under `up.railway.app` and a `_railway-verify` TXT
   record.

   CLI: `railway domain crucible.flow-through.com.au`, then
   `railway domain status crucible.flow-through.com.au` for the values.

5. **Cloudflare DNS**, zone `flow-through.com.au`:
   - `CNAME` `crucible` to the exact target Railway showed, proxy status
     **DNS only (grey cloud)**. The interface streams events for up to 13
     minutes at a time and Cloudflare's proxy has been seen buffering
     `text/event-stream`; grey cloud takes it out of the path.
   - `TXT` with the exact name and content Railway showed.

   Back in Railway, wait for the green tick on the domain. Railway issues the
   certificate itself.

6. **Try it end to end.** Open `https://crucible.flow-through.com.au`, sign in
   with the demo pair, start the "full" review of the demo target, watch it
   finish, download the ledger from the interface, and on any machine with
   the repository run:
   ```
   python -m crucible.cli verify <downloaded ledger>
   ```
   `chain intact` and `every decision in it reproduces` is the pass. This is
   the self-reported mode: it checks the chain and replays the decisions
   against the policy the file recorded. The independent `--workspace` mode
   only replays when the directory you name is the one the run recorded,
   which for a ledger made in the container (`/app/demo_target`) will not be
   your checkout; `verify` says so and still checks the chain.

## Redeploying

Push to `main`. Railway rebuilds on any change matching the watch patterns in
`railway.json` (`**/*.py`, `web/**`, `requirements.txt`). A redeploy drops
open streams; the browser reconnects and replays the run from its buffer.

## Rolling back

Railway dashboard, Deployments, pick the previous one, Redeploy. Nothing
persists between deploys apart from the variables, so a rollback is complete.
