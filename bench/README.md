# Does constrained decoding make a small local model reliable at agentic work?

A gate, not a demo. The question was asked before any code was written for the
answer, and the bars were written down before any number existed, because a bar
set afterwards is not a bar.

## The claim under test

A 9B model running on one desktop GPU is cheap, private and nobody's dependency.
It is also widely held to be unusable for agentic work, because agent loops
demand exactly-shaped tool calls every step and small models produce nearly-right
JSON. If that is really the binding constraint, then constrained decoding removes
it outright: llama.cpp compiles a JSON schema to a grammar and consults it while
sampling, so a token that would take the output outside the shape is never drawn.
Malformed output stops being unlikely and becomes unrepresentable.

If that is *not* the binding constraint, the whole idea is worth dropping, and
finding that out in an afternoon is the cheapest possible outcome.

## The arms

| arm | decoding | thinking | otherwise |
|-----|----------|----------|-----------|
| A | asked in the prompt for one JSON object | off | identical |
| B | reply constrained during sampling by a per-role JSON schema | off | identical |
| C | asked in the prompt for one JSON object | on | identical |
| D | reply constrained during sampling by a per-role JSON schema | on | identical |

Two factors, four arms. Constraint was the question; thinking turned out to
matter as much and had to be separated from it rather than fixed at one level.
The A/B pair is the measurement the numbers below come from. The C/D pair sits
under `results/starved/` and has its own section at the end, because its
replies were starved of output tokens and it measures the allowance more than
the constraint.

Held still across A and B: the same weights and quantisation, the same task,
the same workspace, the same temperature, the same seed sequence, the same
serial execution so agents never interleave differently, and the same disabled
thinking. The constraint is the only difference.

Each arm runs five times. The first version of this ran each arm once at
temperature zero and was wrong twice over. One run is a single observation of a
noisy process. And greedy decoding made the three independent verifiers on a
finding return character-identical verdicts, so the adversarial panel was one
opinion counted three times and the two-of-three survival threshold was really
one-of-one. Both are fixed: the arms sample, and they repeat.

## Setup

- **Model**: Qwen3.5-9B, Q5_K_M GGUF from `unsloth/Qwen3.5-9B-GGUF` (6.13 GB)
- **Runtime**: llama.cpp `b10453`, CUDA 13.3 build, all layers on GPU
- **Hardware**: RTX 5080, 16 GB VRAM (8.8 GB resident), 32 GB system RAM
- **Target**: `demo_target`, which carries nine planted defects with a known
  answer key, three of them graded hard

The constraint is delivered as JSON Schema through `response_format` rather than
as hand-authored GBNF. Two reasons, and the second is the honest one: schema is
what a practitioner would actually reach for, so the result transfers; and this
build's GBNF parser rejects the canonical negated character class from
llama.cpp's own `json.gbnf`, which cost an hour. The mechanism measured is
identical either way — the schema is compiled to a grammar and sampling is
masked against it.

## How replies are graded

The arena's parser is deliberately generous: it reads a tool call in whichever
shape the model chose, pulls JSON out of prose and code fences, and rescues line
numbers written as `"42-45"`. That generosity was worth building — it once took a
real run from nothing surviving to nine findings out of ten. It also makes the
arena the wrong instrument to measure with, because every rescue is invisible and
the unconstrained arm would look healthy on work the parser was doing for it.

So every reply is graded three ways, before anything forgives it:

- **strict** — the documented protocol exactly. `json.loads` on the raw text
  returns a tool call with a known name and an object of arguments, or a done
  object. Nothing was forgiven.
- **salvaged** — not strict, but the tolerant path recovered a usable action. The
  step worked, and it only worked because of code written to absorb this.
- **dead** — neither. The step is burned.

Only the third costs a run directly. The second is the interesting number,
because it measures how much the tolerant parser is carrying.

## How defects are scored

Nine defects, positions known. The counting is less obvious than it looks: three
files carry two defects each, thirteen, eighteen and twenty lines apart, so a
window generous enough to credit a finding pointing at a function signature is
also wide enough to let one finding claim either defect.

Two signals therefore. Line proximity decides candidacy; a short list of terms
drawn from each defect's own description decides between candidates. Each planted
defect can be claimed once — a run reporting the same defect three times has
found one defect.

Findings matching nothing are reported as **unmatched**, not as wrong. The target
was written to carry nine defects, not to be free of every other one, so an
unmatched survivor may be a genuine defect nobody planted, and the answer key
cannot support calling it a false positive.

The project's own shipped scorer is reported alongside. It credits a defect if
*any* finding lands within twelve lines, with no one-claim-per-defect rule, so one
finding between two planted defects can credit both. Fine for a rough recall
figure, wrong for comparing arms, and the gap between the two numbers is worth
seeing rather than hiding.

## The bars

Set in advance:

| measure | bar |
|---|---|
| malformed replies (salvaged + dead) | fall 80% or more from A to B |
| defects recovered in B | 6 of 9 or better |
| B's reasoning vs A's | no more than about 15% below |

The third is the one expected to fail. It is the Format Tax: constrain the
sampler hard enough and the model stops thinking well, trading broken JSON for
confidently-shaped nonsense. **A high recovery number bought with degraded
reasoning is a failure, not a win**, and writing that down in advance is what
stops it being argued into a win afterwards.

## What came out

Five runs per arm, in `results/arm-A.json` and `results/arm-B.json`, with
`results/analysis.json` recomputed from the saved replies. Read straight off
`python -m bench.run_bench --compare` and `python -m bench.analyse`:

| measure | A (prompted) | B (constrained) |
|---|---|---|
| replies not strict (salvaged + dead) | 31 of 239, 12.9% | 2 of 237, 0.8% |
| dead steps per run | 3.8 | 0.4 |
| findings raised per run | 3.4 | 5.6 |
| planted defects found at the hunt stage, per run | 1.8 | 3.2 |
| union of defects found across the five runs | 3 of 9 | 7 of 9 |
| findings that survived verification, all runs | 0 | 0 |
| output tokens, all runs | 47,709 | 40,431 |

Against the bars:

- **Malformed replies fall 80% or more: cleared.** 6.2 to 0.4 per run, a 94%
  fall. The constraint does exactly what it says about shape.
- **6 of 9 defects recovered in B: missed, on the metric the gate uses.** The
  gate scores defects that survive verification, and that number is zero in
  every run of both arms, so `--compare` prints `FAIL constrained recovers 6
  of 9+ 0/9 mean`. At the hunt stage B reaches 3.2 per run and 7 of 9 across
  the union, a 78% lift over A. That lift is real and it is not the bar that
  was written down.
- **Reasoning within about 15%: cleared, and only vacuously.** 0 versus 0 on
  survivors; at the hunt stage B is ahead, so the Format Tax did not show up
  at this operating point.

The zero survivors are the third finding in the top-level README: this model's
verifiers, told that uncertainty is a refutation, refuted everything, including
findings that matched planted defects. Constrained decoding fixed the shape of
what the model said and left what it decided alone.

## The thinking-on arms, C and D

`results/starved/arm-C.json` and `arm-D.json` are the same design with the
model's thinking switched on, five runs each. They are kept because they were
run, and kept apart because they do not measure what they were meant to. The
output allowance per reply was 2,200 tokens, thinking counts against it, and on
most steps the model was still thinking when the allowance ran out: every dead
reply in both arms finished with `finish: length` and an empty answer. Dead
replies were 39% of C and 43% of D, against 7.9% and 0.8% with thinking off.

What did come through: C found 1.4 planted defects per run at the hunt stage
and D found 1.0, each with a union of 4 of 9, and between them three findings
survived verification across ten runs (one in C, two in D), the only survivors
this model produced anywhere. Runs took 20 to 30 minutes against about a minute
and a half. Rerunning this pair with an allowance that lets the thinking finish
is the open next step, and until it is run the numbers above are the numbers.

## Running it

```bash
python -m bench.run_bench --arm A --runs 5
python -m bench.run_bench --arm B --runs 5
python -m bench.run_bench --arm C --runs 5   # thinking on; see the section above
python -m bench.run_bench --arm D --runs 5
python -m bench.run_bench --compare
python -m bench.analyse
```

The runs need a llama.cpp server on `http://127.0.0.1:8080` with the model
above loaded; `--compare` and `analyse` need only the saved results and run
anywhere. Every raw reply is saved. `analyse.py` recomputes every number from the saved
text, so any metric added later is applied to both arms by the same code on the
same day, and the saved runs stay checkable by anyone who wants to disagree with
the arithmetic.
