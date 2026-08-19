# Crucible

AI finds bugs. Most of them are wrong. This proves which ones aren't.

Agents review a codebase, and every finding they raise is handed to three
independent verifiers whose only job is to destroy it. Only survivors are
reported. The number this makes largest is not how many defects were found, it
is **how few survived**. A recorded run against the demo target raised sixteen
findings and reported nine; the seven it threw away are the point.

```
09 / 16  SURVIVED
```

Every agent action is checked against a written policy before it runs, and every
call and result is written to a hash-chained ledger that someone who does not
trust you can verify themselves.

---

## Three things it has actually done

**It found a critical sandbox escape in its own policy engine.** Pointed at its
own source, it reported that the command allowlist checked only the first word
of a command, so `python -c "..."` handed the process arbitrary code and left
the sandbox entirely. No shell metacharacter was involved, so the guard standing
in front of that path never fired. Reproduced, fixed, and the regression test is
in [`tests/test_core.py`](tests/test_core.py) (the checks headed "an
allowlisted binary is not an allowlisted behaviour"). Nine further defects it
found in itself are written up unfixed in
[`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md).

**Constrained decoding lifted a 9B local model's defect discovery by 78%.**
Measured properly: nine planted defects with an answer key, five runs per arm,
pass marks written down before any number existed. At the hunt stage, recovery
went from 1.8 to 3.2 defects per run and from 3 to 7 across the union of runs,
while malformed replies fell from 12.9% to 0.8%, and the reasoning degradation
that everyone warns about did not appear there. One pre-registered bar was
missed: the gate scores defects that survive verification, that number was
zero in every run of both arms, and `python -m bench.run_bench --compare`
prints the FAIL. Method, raw data and the full outcome in
[`bench/README.md`](bench/README.md), with the runs in
[`bench/results/`](bench/results/).

**Ten runs of that model, thinking off, returned zero survivors, including
correct findings.** The hunters correctly identified real defects and the
verifiers destroyed every one, because "default to refuted, uncertainty is a
refutation" becomes "refute everything" for a model uncertain about everything.
A confident zero that looks exactly like a clean bill of health and means the
opposite. This is why the verifier seat is configured separately from the
rest, and why it is worth knowing before putting a small model in it. (With
thinking on, three findings survived across ten runs, and most replies were
starved of output tokens; that pair is kept under `bench/results/starved/`
and described in the bench README.)

**And the tool reviewing this README was reviewed the same way.** Four agents
over the new command line code, independent verification on each finding, six
verified defects, all six real. The worst: a run whose agents all failed exited
0 and printed a clean bill of health.

---

## Why this exists, plainly

**This is a demonstration.** It was built to show a working control plane for
autonomous agents, not to be bought, deployed, or trusted with your production
codebase. The target it reviews is a small synthetic project written for the
purpose, with defects deliberately planted in it and an answer key checked in
beside them, so the arena's precision and recall are measurable rather than
asserted.

Saying that up front matters, because the interesting claims here are the ones
that are easy to fake. Anything can print "3 agents working". The parts worth
looking at are the ones that fail closed when something goes wrong, and those
are only visible if you know what to look for.

Read this section, then go and try to break it.

---

## The argument

Generating findings is nearly worthless. A language model asked to find bugs
will always find bugs, and a great many of them will be confident, articulate,
and wrong. Anyone who has pointed a model at a codebase knows this. The output
looks like work and costs the reader more time than it saves.

So the entire design puts its weight on the second half. Every finding a hunter
raises is handed to **three independent verifiers**, each told to refute it,
each blind to the others' verdicts, each instructed that uncertainty counts as a
refutation. A finding needs a clear majority to survive. A verifier that crashes
or fails to answer counts against the finding it was judging, because the wrong
direction to fail in is the one where an unverified claim gets reported because
a process died.

That is why the kill count has permanent space on screen and never collapses.
A system that shows how much of its own output it threw away is making the
argument structurally instead of in copy.

---

## What is actually enforced

Three properties are load-bearing. Each has tests that fail if the guard is
removed.

### Authority is a document, not an assumption

An agent's power is usually whatever its tools happen to allow, discovered by
reading the tool implementations and hoping. Here it is declared in one object
that is printed on screen during the run, travels in the run record, and is
checked before any tool code executes.

```python
review_policy(workspace) = {
  read_file      within the workspace
  list_dir       within the workspace
  search         within the workspace
  write_scratch  within .crucible-scratch/ only, 200 KB ceiling
  run_tests      python, pytest, node, npm only
  network        refused entirely
}
```

Containment is decided on the **resolved** path, so `work/../../etc/passwd` is
judged as the file it actually opens rather than the string it looks like. A
sibling directory whose name shares the workspace's prefix is refused, which is
the case a naive `startswith` check silently admits. Commands carrying shell
metacharacters are refused even though nothing here uses a shell, because an
allowlist means nothing if a permitted binary can smuggle another inside its
arguments. **A policy check that raises is a refusal, never an allow.**

Refusals are first-class events. They appear on screen with the reason, they are
counted, and they go into the record. A refused agent gets a sentence it can
reason about and carries on, which is behaviour worth watching.

### The record is tamper-evident

Every run writes an append-only ledger. Each entry carries the SHA-256 of the
entry before it, so the file proves its own order and completeness. Editing a
payload, deleting an entry, inserting one, or swapping two all produce a break,
and `verify()` names the first sequence number where the chain stops adding up.

You do not have to take the server's word for this. The interface offers the
ledger for download, and `crucible verify <ledger>` recomputes the whole chain
and replays every policy decision on your machine, without trusting anything
the server says. Add `--workspace <dir>` when the reviewed directory is at
hand and the replay is rebuilt from it rather than from the policy the file
recorded. The claim is checkable by the person who doubts it.

It is tamper-**evident**, not tamper-proof. Making it the latter needs a
signature and a key kept somewhere else, which is a real design and not one
that has been done here. A chain truncated at the end still verifies, which is
why the root hash is published with the report.

### The budget holds against work in flight

Checking "have I spent too much" and then spending is two steps, and a dozen
agents run them concurrently. Every one can pass the check against the same low
total a moment before any of them pays, and the run sails past its ceiling with
every individual check having been correct.

So a call **reserves** its worst case up front, priced as though the model
returns its full output allowance, and settles to real usage afterwards. Calls
that fail release their hold rather than leaking the ceiling away. A run stops
slightly early instead of at a number nobody chose.

---

## Architecture

```
                    ┌─────────────┐
   task ──────────► │   planner   │  one call, expensive seat
                    └──────┬──────┘  divides the ground into 4-6 lanes
                           │
        ┌──────────┬───────┴───┬──────────┐
        ▼          ▼           ▼          ▼
     hunter     hunter      hunter     hunter    cheap, parallel, blind
        │          │           │          │      to each other
        └──────────┴─────┬─────┴──────────┘
                         │  findings
        ┌────────────────┼────────────────┐
        ▼                ▼                ▼
    verifier         verifier         verifier   expensive seat, ×3 per
    "refute it"      "refute it"      "refute it"  finding, independent
        └────────────────┼────────────────┘
                         ▼
                 majority survives
```

Hunters are blind to each other on purpose. Agents that can see each other's
work converge on the same easy finding and a review ends up with four copies of
one bug and nothing else.

Three roles, three price points. The planner runs once. Hunters fan out cheap
and wide. Verifiers are expensive because that is the seat where being right
matters, and there are three of them per finding.

| Role | Model | Why |
|---|---|---|
| Planner | `gpt-5` | Runs once or twice; quality here shapes the whole run |
| Hunter | `gpt-5-mini` | Many calls in parallel; volume work |
| Verifier | `gpt-5` | Three per finding; the seat where being wrong is expensive |

A run on those seats has cost between 21 and 39 US cents across thirteen
recorded runs, median 29.

---

## Sovereign deployment

The single biggest blocker to AI adoption in Australian government is not
capability. It is that the data cannot leave the boundary.

Crucible speaks the OpenAI chat completions API through a provider interface, so
pointing it at a local server is a configuration change rather than a rewrite.
`docs/SOVEREIGN.md` sets out what a fully local deployment costs in quality and
in hardware, with real numbers: what fits in 16 GB, measured KL-divergence per
quantisation level, throughput on consumer and datacentre cards, and where the
honest gap to a frontier model is small (judging) versus large (generating a
correct hard fix).

The short version. On one 16 GB card you reach roughly `gpt-5-mini` class for
worker tasks and most of the way for planning. You do not reach `gpt-5` class.
Matching it sovereignly starts around USD 30–45k of hardware. That document
exists because "yes, it can run locally" is the kind of claim that should come
with the bill attached.

One design note that survives the move and is worth more than the model choice:
the verifier should come from a **different model family** than the hunter. A
critic drawn from the same training distribution systematically misses the
errors its sibling makes.

---

## Running it

Python 3.11 or newer and nothing else. From a fresh clone:

```bash
git clone https://github.com/BlakeR-M/crucible
cd crucible
python -m pip install pytest         # only for the one-command test run
python -m pytest -q                  # 46 passed: every check file plus the demo target's suite
python -m crucible.cli --help        # the tool, straight from the checkout
pip install .                        # or install it: gives you `crucible` and `crucible-server`
```

The one path that needs no API key is the web interface in offline mode. It
runs the real orchestrator, policy and ledger against the demo target with a
stand-in model that answers from the prompt, so nothing is spent:

```bash
CRUCIBLE_OFFLINE=1 CRUCIBLE_USER=demo CRUCIBLE_PASS=demo python main.py
# then open http://localhost:8420 and sign in as demo / demo
```

Every other path (`crucible run`, `crucible models`, the interface with real
models, the bench) talks to a model, so it needs `OPENAI_API_KEY` in the
environment or a local OpenAI-compatible server in `crucible.toml`.
`crucible verify` needs neither.

### As a check before something ships

```bash
crucible init                     # write a starter crucible.toml
crucible models                   # show the seats and reach the endpoint
crucible run .                    # review; blocks the pipeline if anything survives
crucible verify runs/abc.jsonl --workspace .
```

### A repository by URL

```bash
crucible run https://github.com/org/repo            # default branch
crucible run https://github.com/org/repo@v1.4.2     # a tag, a branch, or a 40-hex commit
crucible run git@github.com:org/repo.git --ref main # scp form is read as https
crucible run https://github.com/org/repo --keep     # keep the checkout and print its path
```

The repository is cloned once, at depth 1, with no submodules and no tags,
into a temporary workspace that is removed when the run ends. The commit it
stood at is recorded (`git rev-parse HEAD`) and then the `.git` directory is
stripped, so the agents read the tree at that commit and never its history.
The report header prints `source: <url> @ <sha>`, the run record carries
`repo_url`, `repo_ref` and `commit_sha` in its first entry, and `crucible
verify` shows them. Public repositories only: no credential is ever passed,
and git is told not to ask for one, so a private repository fails in a
sentence rather than waiting on a prompt. The config still comes from your
current directory, never from inside the clone. The CLI takes any host and
caps the checkout at 200 MB and 20,000 files; a URL that cannot be parsed, or
a repository past the caps, exits 2 with the numbers.

Exit codes, because the point of a gate is that something downstream reads it:

| code | meaning |
|---|---|
| `0` | the run completed and nothing survived verification |
| `1` | a finding survived every verifier |
| `2` | the run could not complete, or the configuration is wrong |

The difference between `0` and `2` is the one that matters, and it is the one
this got wrong first. A run whose agents all died reported success, because
"found nothing" and "never ran" both produce an empty finding list. A run that
could not complete has no verdict to give, so every way a run can be hollow now
fails it: agents that never answered, a planner that produced no lanes, a budget
that ran out, or a boundary that did not hold.

### Configuration

`crucible.toml`, committed to the repository it reviews, so the same run happens
on a laptop and in a pipeline. No secrets in it: the key is named, never written.

```toml
[provider]
base_url    = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
metered     = true

[models]
planner  = "gpt-5"
worker   = "gpt-5-mini"
verifier = "gpt-5"

[gate]
fail_on = "survived"
```

Models are chosen **per seat** rather than once for the run. That is what lets
the wide fan-out run on hardware you own while the seat where being right
matters runs on something strong.

### Against your own model

Anything speaking the OpenAI chat API works, which is llama.cpp, Ollama, vLLM
and LM Studio. It is a base URL, not a second code path.

```toml
[provider]
base_url = "http://localhost:8080/v1"
metered  = false          # priced at nothing, so the ceiling stops refusing a free run
```

Before trusting a local configuration, point it at the target that has an answer
key and see what it actually catches:

```bash
crucible run demo_target --score
```

### The web interface

```bash
crucible-server          # or: python main.py
```

Then open `http://localhost:8420`. It needs `OPENAI_API_KEY` in the environment
or in a file named by `CRUCIBLE_ENV_FILE`. `CRUCIBLE_OFFLINE=1` runs the whole
thing, orchestrator and policy and ledger included, without spending anything.

| Variable | Default | What it does |
|---|---|---|
| `CRUCIBLE_USER` / `CRUCIBLE_PASS` | none | Demo credentials. Both required: the server exits with code 2 when either is unset |
| `OPENAI_API_KEY` | none | Required for paid runs; read when a run starts |
| `CRUCIBLE_ENV_FILE` | `.env` at the repo root | File the key is read from when it is absent from the environment |
| `CRUCIBLE_OFFLINE` | unset | `1` runs everything with a local stand-in model and spends nothing |
| `PORT` | `8420` | Listen port |
| `CRUCIBLE_SECRET` | random per boot | Session cookie signing key |
| `CRUCIBLE_RUN_CEILING_USD` | `0.60` | Spend ceiling for one run |
| `CRUCIBLE_DAILY_CEILING_USD` | `8.00` | Spend ceiling per UTC day |
| `CRUCIBLE_CHAT_CEILING_USD` | `0.40` | Spend ceiling for one visitor's conversation |
| `CRUCIBLE_REPO_HOSTS` | `github.com,gitlab.com` | Hosts a visitor may name in the "Review a public repository" field |
| `CRUCIBLE_REPO_MAX_MB` | `50` | Size cap on a cloned repository, `.git` excluded |
| `CRUCIBLE_REPO_MAX_FILES` | `5000` | File cap on a cloned repository |
| `CRUCIBLE_REPO_CLONE_TIMEOUT_S` | `120` | Seconds a clone may take before it is stopped |

The interface takes a public repository URL as well as the demo target:
`POST /api/run` with `{"repo_url": "...", "ref": "...", "task": "full"}`, or
the field on the page. The same guardrails as the CLI apply, plus a host
allowlist and the smaller caps above; the clone is depth 1 with no
submodules, lives in a private temporary directory for exactly the length of
the run, and the review policy is scoped to it. The clone shows on the stream
as `clone_started`, `clone_finished` (with the commit, file count and bytes)
and `clone_failed`, and `GET /api/tasks` lists each run with its source. A URL
run counts against the run and daily ceilings like any other.

Deployment to Railway behind a Cloudflare subdomain is the runbook in
[`docs/deploy.md`](docs/deploy.md); the research behind it, including the two
limits that shape the streaming design, is in
[`docs/deploy-research.md`](docs/deploy-research.md): Railway closes any
request at 15 minutes and any silent one at 5, and Cloudflare's proxy has been
observed buffering `text/event-stream`.

### Dependencies

None. Standard library only.

That is deliberate rather than minimalist for its own sake. This project argues
that an autonomous system should be auditable, and a reviewer who has to trust a
dependency tree in order to audit the thing auditing their code has been handed
a weaker argument. Every line that serves this can be read.

---

## Tests

```bash
python -m pytest -q                # everything below, plus the demo target's own suite

python tests/test_core.py          # 71
python tests/test_orchestrator.py  # 120
python tests/test_chat.py          # 134
python tests/test_archive.py       # 102
python tests/test_cli.py           # 125
python tests/test_assay.py         # 20
python tests/test_repo.py          # 81
```

**653 checks, no network, no spend.** The check files are plain scripts;
`tests/test_suite.py` runs each one under pytest so a pipeline needs one
command. The orchestrator suite replaces the model
with a stand-in that answers from the prompt it is given, because a queue of
canned replies handed out to concurrent agents would pass or fail by luck.

What they actually cover, beyond the happy path:

- a forgery whose hash chain has been **correctly rebuilt**, which the chain
  alone cannot catch and only the policy replay can
- a forgery that also rewrites the recorded policy, which passes the
  self-reported replay and fails against `--workspace`. That limitation is
  asserted as a test rather than hidden
- a run whose planner succeeds and whose every other agent dies, which must
  exit 2 rather than 0
- a malformed config exiting 2 rather than 1, because 1 means a finding
  survived and a typo must not look like a review result

- path traversal, prefix-sibling directories, chained shell commands, lookalike
  hostnames, and a checker that throws
- ledger edits, deletions, insertions and reordering
- a verifier that dies counting against its finding
- the majority rule at every split
- a run that hits its ceiling still producing a report
- an agent refused mid-run carrying on afterwards
- 18 findings and 54 verifiers writing one hash chain concurrently, with no
  sequence number repeated or skipped

---

## Honest limitations

- **The demo target is synthetic.** Real codebases are larger, messier, and have
  history. Recall on this target says little about recall on yours.
- **Runs vary.** Three consecutive runs against the same nine planted defects
  found seven, seven and six, and not the same seven. Different runs catch
  different hard defects: one found the reschedule cache-invalidation miss and
  missed the falsy-role auth bypass, the next did the reverse. Quote a range,
  never a single number, and treat any single run as a sample.
- **Three verifiers is a small panel**, chosen so a run finishes while someone
  is watching it. Reducing false positives further wants more of them, or a
  verifier from a different model family, or both.
- **Tamper-evident, not tamper-proof.** See above.
- **The survival threshold is a policy choice, not a discovered constant.** Two
  of three is set in `orchestrator.py` and moving it trades false positives
  against false negatives with no free lunch in either direction.
- **Findings are not fixes.** The arena reports what it can defend, and stops
  there. Generating a correct fix for a hard defect is a materially harder task
  than recognising one.
- **`verify` trusts the file's own policy unless told otherwise.** The record
  carries the policy the run declared, so a forger who rewrites the record can
  rewrite the rules it will be judged against. The replay then shows internal
  consistency rather than that the limits were real. `--workspace` rebuilds the
  policy independently and is the only configuration in which the check is
  adversarial. The default output says so in as many words.
- **The local-model measurements are one model on one target.** Five runs per
  arm with pass marks fixed in advance is enough to act on and not enough to
  generalise. The defensible sentence is "on this target, with this model".

---

## Measuring it

The claim that any of this works is checkable rather than asserted, because the
target carries nine defects with a known answer key.

```bash
python -m bench.run_bench --arm A --runs 5   # prompted only (needs a local llama.cpp server)
python -m bench.run_bench --arm B --runs 5   # constrained decoding
python -m bench.run_bench --compare          # reads the saved results; no model needed
python -m bench.analyse
```

Every raw reply is saved, so any measure added later is applied to both arms by
the same code on the same day, and the saved runs stay checkable by anyone who
wants to disagree with the arithmetic. Method, pass marks, the outcome against
each and the reasons for each in [`bench/README.md`](bench/README.md).

`assay/` holds a second, separate experiment that was killed on its own
evidence: two pre-registered kill tests for a small-specialist-model idea, both
negative, with the raw results kept. [`assay/README.md`](assay/README.md) says
what was measured.

---

Built by Blake Rowlands-Mowle. Canberra.
