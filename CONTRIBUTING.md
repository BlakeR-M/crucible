# Contributing

Crucible is a small, standard-library-only Python project. The bar for a change
is that it keeps the three load-bearing properties in the README enforced by
tests: authority is a document, the record is tamper-evident, the budget holds.

## Setup

```bash
git clone https://github.com/BlakeR-M/crucible
cd crucible
python -m pip install pytest      # the only thing to install; the tool itself needs nothing
python -m pytest -q               # every check, no network, no keys
```

Python 3.11 or newer. The checks under `tests/` are plain scripts, so
`python tests/test_core.py` runs one file with per-check output; `pytest`
runs all of them plus the demo target's own suite.

## Ground rules

- **No third-party dependencies.** This is the point of the project, and a
  pull request that adds one will be asked to do without it.
- **Guards come with a test that fails when the guard is removed.** A policy
  check, a ledger property or a budget rule without that test is unfinished.
- **Line endings are LF** (`.gitattributes` enforces it). Set
  `git config core.autocrlf false` on Windows.
- **Nothing that costs money runs in CI.** Tests use the stand-in provider.
  A change that needs a real model to demonstrate goes in as a saved run under
  `bench/results/` or `assay/results/`, with the raw replies included, so the
  numbers can be recomputed by anyone.
- **Prose in the README states what was measured, with the file that proves
  it.** A claim without a file behind it is a claim to leave out.

## Sending a change

Open a pull request against `main` with a message that says what changed and
why. CI runs on Ubuntu and Windows across Python 3.11 and 3.12. Security
findings go through [SECURITY.md](SECURITY.md) rather than a public issue.
