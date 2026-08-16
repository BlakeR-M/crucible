# Crucible

An arena where a fleet of AI agents reviews code in the open, and every finding
they raise is then attacked by independent verifiers whose job is to destroy it.
Only findings that survive are reported.

The number the interface makes largest is not how many defects were found. It is
how many of them **survived**, next to how many were raised. A run that raises
forty-one findings and reports nine is doing the thing correctly.

```
09 / 41  SURVIVED
```

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

You do not have to take the server's word for this. Open the ledger drawer and
press **Verify chain**: the browser recomputes the whole chain client-side with
SubtleCrypto and prints the result and the elapsed milliseconds. The claim is
checkable by the person who doubts it, on their machine, without trusting
anything this server says.

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

A measured run costs roughly twenty cents.

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

```bash
python -m crucible.server
```

Then open `http://localhost:8420`. It needs `OPENAI_API_KEY` in the environment
or in a file named by `CRUCIBLE_ENV_FILE`.

| Variable | Default | What it does |
|---|---|---|
| `OPENAI_API_KEY` | — | Required |
| `PORT` | `8420` | Listen port |
| `CRUCIBLE_USER` / `CRUCIBLE_PASS` | `evaluator` / `crucible` | Demo credentials |
| `CRUCIBLE_SECRET` | random per boot | Session cookie signing key |
| `CRUCIBLE_RUN_CEILING_USD` | `0.60` | Spend ceiling for one run |
| `CRUCIBLE_DAILY_CEILING_USD` | `8.00` | Spend ceiling per UTC day |

Deployment to Railway behind a Cloudflare subdomain is covered in
`docs/DEPLOY.md`, including the two limits that shape the streaming design:
Railway closes any request at 15 minutes and any silent one at 5, and
Cloudflare's proxy has been observed buffering `text/event-stream`.

### Dependencies

None. Standard library only.

That is deliberate rather than minimalist for its own sake. This project argues
that an autonomous system should be auditable, and a reviewer who has to trust a
dependency tree in order to audit the thing auditing their code has been handed
a weaker argument. Every line that serves this can be read.

---

## Tests

```bash
python tests/test_core.py
python tests/test_orchestrator.py
```

87 checks, no network, no spend. The orchestrator suite replaces the model with
a stand-in that answers from the prompt it is given, because a queue of canned
replies handed out to concurrent agents would pass or fail by luck.

What they actually cover, beyond the happy path:

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

---

Built by Blake Rowlands-Mowle. Canberra.
