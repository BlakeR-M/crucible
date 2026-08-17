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

| arm | decoding | otherwise |
|-----|----------|-----------|
| A | asked in the prompt for one JSON object | identical |
| B | reply constrained during sampling by a per-role JSON schema | identical |

Held still across both: the same weights and quantisation, the same task, the
same workspace, the same temperature, the same seed sequence, the same serial
execution so agents never interleave differently, and the same disabled
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

## Running it

```bash
python -m bench.run_bench --arm A --runs 5
python -m bench.run_bench --arm B --runs 5
python -m bench.run_bench --compare
python -m bench.analyse
```

Every raw reply is saved. `analyse.py` recomputes every number from the saved
text, so any metric added later is applied to both arms by the same code on the
same day, and the saved runs stay checkable by anyone who wants to disagree with
the arithmetic.
