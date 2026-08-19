# Assay: an experiment, killed on its own evidence

Assay was the next idea after Crucible: a small model, made reliable at one
narrow kind of reasoning by making its mistakes unrepresentable, and beating a
frontier model there. Two kill tests were written before any of Assay existed,
each with its pass mark fixed in advance. Both came back negative. The code and
the raw results stay in the tree as the record; nothing here is used by
Crucible itself.

## Test 1: are a small coder's failures mechanical? (`assay/baseline.py`)

Premise: a small model's coding failures are mostly mechanical (hallucinated
API, undefined name, wrong arity) rather than conceptual (wrong algorithm). A
constrained language can remove the first kind and cannot touch the second.
Bar, set before the run: build if the mechanical share is 50% or more; build
only a fixed-stdlib layer and re-measure at 25 to 50%; stop below 25%.

Result, in `results/baseline.json`: Qwen2.5-Coder-7B-Instruct (Q6_K), 30 unseen
tasks, 5 samples each, temperature 0.2. 115 of 150 generations passed (76.7%).
All 35 failures were classified as wrong algorithm. **Mechanical share: 0.0%.**
Every ambiguous failure was deliberately scored as conceptual, so this number
is the least favourable reading, and it lands far under the lowest bar.

## Test 2: is there room to beat a frontier model? (`assay/screen.py`)

Premise: a specialist can only win on a problem class where a frontier model
has room to be beaten. Three classes were built (`assay/problems/`): rostering
(who), seating (where) and jobs (when), each stated in plain prose with the
formal spec kept for the solver and verifier only. Bar: a class is a target
when the model sits between roughly 45% and 90% accuracy; at 90% or above it is
dead.

Results, in `results/`:

| model | difficulty | n per class | rostering | seating | jobs |
|---|---|---|---|---|---|
| local (Qwen2.5-Coder-7B-Instruct Q6_K) | medium | 40 | 100% | 100% | 100% |
| gemini-3.1-pro-preview | hard | 30 | 100% | 100% | 96.7% (one call failed) |

Every class is dead by the bar that was written down first. The problem
classes were too easy for the models that would need beating.

## What is here

- `problems/`: the three generators, their solvers and verifiers.
  `python -m assay.problems` runs the soundness self-check (the solver's own
  answer verifies, a corrupted answer does not, an impossible instance has no
  solution). `python tests/test_assay.py` covers the same ground in the suite.
- `tasks.py`: the thirty coding tasks with reference solutions and tests.
- `baseline.py`, `screen.py`: the two experiments. Both need a model: a local
  OpenAI-compatible server (`CRUCIBLE_LOCAL_URL`, `CRUCIBLE_LOCAL_MODEL`),
  or `OPENAI_API_KEY` / `GEMINI_API_KEY` from the environment or `.env`.
- `results/`: the raw records the numbers above are computed from, every reply
  included. The model field in `baseline.json` was a local file path and is
  recorded here as the model name.
