# Security

## Reporting

Email service@flow-through.com.au with "crucible" in the subject. Include what
you found, how to reproduce it, and what it lets an attacker do. You will get a
reply within seven days saying whether it is confirmed and what happens next.
Please keep the report private until a fix is out; findings that are confirmed
and fixed are credited in `docs/KNOWN-ISSUES.md` unless you ask otherwise.

Findings that are already recorded as open in
[`docs/KNOWN-ISSUES.md`](docs/KNOWN-ISSUES.md) are known; a report that adds a
working exploit or a higher impact than recorded is still welcome.

## What this tool does, and the assumptions it makes

Crucible runs language-model agents that read a codebase and, within a policy,
execute commands. Every proposed action is checked by `crucible/policy.py`
before any tool code runs: file reads, listings and searches are contained to
the resolved workspace path; scratch writes go only under `.crucible-scratch/`
with a size ceiling; the only commands allowed are `python`, `pytest`, `node`
and `npm`, with shell metacharacters refused and no shell involved; there is no
network tool. A policy check that raises is a refusal.

Those are the guarantees, and here is what they rest on:

- **The policy is a boundary inside one operating-system user, not a sandbox.**
  `run_tests` executes `python`, `pytest`, `node` or `npm` from the reviewed
  repository as the user running Crucible. A repository whose `package.json`
  test script or `conftest.py` is hostile can do anything that user can do.
  Review code you would run the tests of. For anything else, run Crucible
  inside a container or VM with the workspace mounted and nothing you care
  about reachable, and treat the policy as defence in depth rather than the
  only wall.
- **The allowlist is on the binary and its arguments, not on what the binary
  does.** The regression for `python -c "..."` (an allowlisted binary handed
  arbitrary code) is in `tests/test_core.py`; the same shape of problem in a
  binary or flag not yet thought of is the class of finding this file exists
  to receive.
- **The ledger is tamper-evident, not tamper-proof.** Anyone who can rewrite
  the whole file can rewrite the chain. `crucible verify --workspace` rebuilds
  the policy independently and is the only mode in which the check is
  adversarial; the default mode trusts the policy recorded in the file and
  says so.
- **The hosted demo is a demo.** It reviews a fixed synthetic target, needs
  the demo credentials to start at all, and caps spend per run, per
  conversation and per day. Its open items are listed in
  `docs/KNOWN-ISSUES.md` (admission control, CSRF, CSP among them). Do not
  point it, or a deployment of it, at code you have not read.
- **Keys stay in the environment.** `OPENAI_API_KEY` is read from the
  environment or a named env file and never written to a ledger, a result
  file or the repository. `.env` is ignored by git.
- **Visitor keys live in memory for one session.** A key a visitor attaches
  through `POST /api/key` is validated with one request to the vendor, then
  held in the server process keyed by the session cookie and dropped on
  sign-out or expiry. It is written to no file, no ledger, no event and no log
  line, it is never echoed back, and any provider error text that quotes it is
  scrubbed before it reaches the record or the stream (`crucible/byok.py`,
  checked in `tests/test_byok.py`). The provider is one of two named vendors
  and the models come from a fixed list; a visitor cannot point the hosted
  box at an arbitrary URL.

## Supported versions

The `main` branch. There are no release branches yet.
