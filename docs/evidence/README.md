# Evidence

The run behind the README's headline number, committed whole so the claim can
be checked rather than believed.

## The 9 / 16 run

[`2026-08-17-demo-target-16-raised-9-survived.jsonl`](2026-08-17-demo-target-16-raised-9-survived.jsonl)
is the full hash-chained ledger of a run recorded on 2026-08-17 against
[`demo_target/`](../../demo_target), the synthetic booking service that ships
in this repository with nine planted defects and a committed answer key.

**Method, as the ledger records it.** The task was "Review this codebase for
access-control and validation defects." The planner divided the ground into
six lanes: authentication and session handling, HTTP input validation,
service-layer rules, tenant scoping, money and invoicing, and temporal
validation. One hunter fanned out per lane, blind to the others. Every finding
the hunters raised went to three independent verifiers instructed to refute
it, with uncertainty counting as a refutation, and a finding needed two of the
three to survive. Sixteen findings were raised and nine survived; each
`finding_judged` entry carries its split (`survived_by` and `refuted_by`).
The boundary probe ran alongside the hunt, so the ledger holds 24 tool
decisions, 16 allowed and 8 refused. The run cost US$0.2746 against a $1.20
ceiling. The seats were the defaults of the day, gpt-5 planning and verifying
with gpt-5-mini hunting; ledgers of this vintage record spend, policy and
every decision, and began naming the model per seat in the header with the
bring-your-own-key work that followed.

**Check it yourself:**

```bash
python -m crucible.cli verify docs/evidence/2026-08-17-demo-target-16-raised-9-survived.jsonl
```

The verifier recomputes the whole hash chain and replays every policy
decision. Expected: `chain intact, head 3e4c243b53b9b660aa198eccdfca8f21`,
24 decisions reproduced, `16 raised, 9 survived, $0.2746`, and exit code 1.
The 1 is the tool being consistent with itself: the exit code reports the
review gate, and nine findings surviving means the code under review failed
its review. An intact record of a failed gate is exactly what this file is.

One honesty note. This is the self-reported mode: the record carries the
policy the run declared, so the replay proves internal consistency. The
adversarial `--workspace` mode rebuilds the policy from a directory you name,
and it applies when that directory is the one the run recorded, which for
this file was the build machine's checkout (the ledger stores its absolute
paths, `D:\projects\crucible\demo_target`). The chain check stands on its own
either way, and both modes are covered by tests, including the forgery that
only `--workspace` catches.

The README's cost sentence (twelve recorded runs between 21 and 39 US cents,
median 29) was measured across this ledger and eleven siblings on the same
seats. This is the one published because it is the run the README quotes.
