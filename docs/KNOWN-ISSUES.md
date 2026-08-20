# Known issues

Crucible was reviewed by its own pattern: four reviewers fanned out across the
codebase, and every finding they raised went to independent verifiers
instructed to refute it. Forty-five findings were raised and eighteen survived.
Those counts were read off the runs at the time; the early ledgers stayed on
the build machine, so the figure is reported here rather than checkable, which
is itself the kind of gap this file exists to record.

The fixes from that review and the ones that followed it are in the history
and listed below for the record. Eight findings stay open and are recorded
here rather than quietly dropped, because a project whose argument is that a
system should show what it threw away cannot keep its own list private.

Each entry says what it costs and why it is still open.

---

## Open

### No CSRF defence beyond SameSite=Lax
`POST /api/run` and `/api/login` have no token. SameSite=Lax stops the ordinary
cross-site form post, but it does not separate subdomains, so anything else on
`flow-through.com.au` could drive a run in a signed-in evaluator's browser.

**Cost:** a wasted run, not a data breach. Nothing here reads or returns
anything private.

**Fix:** a signed token in the session cookie, echoed in the request.

### `/static/` is served without a session
Stylesheet and script are readable by anyone who guesses the URL. Deliberate for
the login page, which needs them before a session exists, but it means the
client source is public.

**Cost:** none that matters. There are no secrets in it and the repository is
open anyway.

### The transcript grows quadratically
Every step re-sends the whole conversation including every prior tool result. An
agent that reads six files sends the first one six times.

**Cost:** real money on long runs, and it is the main reason the step limit
exists. Fine at the current scale, wrong at ten times it.

**Fix:** summarise or drop older tool results once the transcript passes a
threshold.

### An exhausted agent discards everything it found
An agent that hits its step limit returns nothing, even if it had already
written three good findings into its reasoning.

**Cost:** silent recall loss, and it looks identical to an agent that found
nothing.

**Fix:** ask for findings-so-far on the final step rather than dropping the
agent.

### A finding can survive on empty non-refutations
A verifier that returns `refuted: false` with no `concrete_failure` still counts
as a survival. The prompt asks for one; nothing enforces it.

**Cost:** weakens the central claim, which is that a survivor is demonstrable.

**Fix:** treat a non-refutation with no concrete failure as a refutation.

### `SURVIVAL_THRESHOLD` is a constant, not a majority
It is set to 2 and `VERIFIERS_PER_FINDING` to 3. Change the second and the first
stops being a majority without saying so.

**Fix:** derive it.

### No Content-Security-Policy
The page renders text written by language models. Everything goes through
`textContent` and there is no `innerHTML` on any model-derived path, so this is
defence in depth rather than a live hole.

### Daily spend can be lost across UTC midnight
`add_spend` books into whichever day is current when a run *finishes*, and the
bucket resets on read. A run spanning midnight can have its spend dropped.

**Cost:** at most one run's worth of ceiling, once a day.

---

## Fixed, for the record

### An agent could write a script into scratch and run it

The policy refuses `python -c` because that flag, in the words of the comment
guarding it, "grants every capability the rest of the policy just refused".
Scratch sat at `workspace/.crucible-scratch`, inside the tree `run_tests` is
scoped to, so writing a `.py` file and asking a permitted interpreter to run
it arrived at exactly the same place: reads outside the workspace, writes past
the size ceiling, edits to the code under review, and a socket.

Two things about this are worth keeping. It is the second defect of this shape
in the same file, after the `-c` hole the arena found in itself, which says
the lesson generalises: an allowlist of binaries is not an allowlist of
behaviours, and every path by which new code reaches a permitted interpreter
has to be closed, not just the obvious one. And the boundary probe reported
`held: true` throughout, including in the published evidence run, because it
only ever tried the `-c` form. A probe is evidence about the doors it tries.

Scratch is now a sibling of the checkout rather than a child, so `run_tests`
can only execute files that were already in it. The probe gained the
write-then-run attempt, so a future run proves the door is shut rather than
leaving it untested, and `tests/test_core.py` fails if scratch moves back
inside the workspace.

### The demo answer key was readable by the agents reviewing beside it

`demo_target/.answer_key.json` lists the nine planted defects and is the basis
of every score the benchmark reports. It sits inside the tree the hunters
read. Listing and search skipped it for being a dotfile, so reaching it needed
the exact filename, and `grep -c answer_key docs/evidence/*.jsonl` returns
zero, so the published run never touched it. The hole was open regardless, and
a hunter that reads the answers has found nothing.

The policy holds the file back by name for `read_file` and `search`, the
refusal is written to the ledger like any other, and the carve-out travels in
the policy the record carries so a replay decides what the live run decided.

### A repository named by URL could run its own tests beside the key

`crucible run <url>` and the interface's repository field reviewed a clone
under the same policy as a local directory, which permits `run_tests`, so a
visitor could point the arena at a repository they control and have its test
files execute inside the process, with `OPENAI_API_KEY` in the environment.
Fixed in two layers that do not depend on each other. A URL run now gets a
policy without `run_tests` (`review-read-only`) unless the operator sets
`CRUCIBLE_URL_RUN_TESTS=1`, and the run's first ledger entry records
`tests_enabled` either way. And every child a tool starts, wherever the
workspace came from, gets a scrubbed environment: an allowlist of what an
interpreter needs to start, with `OPENAI_API_KEY`, every `CRUCIBLE_*` value
and anything ending in `_KEY`, `_TOKEN`, `_SECRET`, `_PASS` or `_PASSWORD`
never passed. `tests/test_repo.py` starts a `run_tests` child with the key set
in the parent and asserts the child cannot see it.

Full detail in the commit history.

- **The command allowlist checked `argv[0]` only**, so `python -c` was arbitrary
  code execution through a permitted binary with no shell metacharacter. A
  verifier reproduced reading a file outside the workspace, writing five
  megabytes past the ceiling, editing the code under review, and opening a
  socket. This one invalidated every other guarantee in the policy.
- An unhandled worker exception discarded every finding its siblings had
  produced.
- `_extract_json` miscounted brace depth inside string literals, which findings
  quote constantly.
- A line number written as `"42-45"` raised out of the hunt.
- A JSON array reply reached `.get` and crashed the agent loop.
- Lanes or findings returned as bare strings crashed the run.
- `lanes` was unbound when the planner hit the ceiling, crashing the report that
  was meant to explain the failure.
- A response with no usage block was booked as free, disabling the ceiling.
- The SSE response promised keep-alive with no length and no chunked encoding.
- A run whose thread failed to start held its slot forever and left browsers
  waiting on a stream that would never end.
- Search took an untrusted pattern into a backtracking engine. Nested
  quantifiers are now refused; the line-length cap that was tried first is not a
  fix, since `(a+)+b` does not finish at any length worth allowing.
- The public `/static/` prefix honoured `..`, so the signed-in app shell was
  readable without a session. Names that climb are refused before serving;
  resolved containment already kept everything outside `web/` unreachable.
- Admission control was check-then-act: the active-run count, the daily spend
  and the run's creation were three separate acquisitions of the registry
  lock, so a burst of simultaneous requests could all read zero and all pass.
  Fixed the day the interface opened to the public: `RunRegistry.admit`
  decides and claims the slot in one acquisition, and a twenty-thread burst
  test holds it to exactly the configured slots.

---

## Not defects, decided deliberately

**`npm test` runs whatever the reviewed repository's `package.json` says.** That
is what "run the project's own tests" means. A hostile repository could put
anything there. Inherent to the task rather than a flaw in the guard, and the
reason the demo target is a repository we wrote.

**There are no default credentials.** `CRUCIBLE_USER` and `CRUCIBLE_PASS`
come from the environment, and `serve()` exits with code 2 and a plain message
when either is unset, so a deployment that forgot them stays closed rather than
opening a paid model to whoever finds the URL. The pair for the hosted demo is
handed out in conversation, never written into the repository.
