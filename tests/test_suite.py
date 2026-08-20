"""Runs every plain-script check file under pytest, one test per script.

The check files are plain scripts on purpose (they run anywhere Python does).
This wrapper exists so that a single `python -m pytest -q` in a pipeline runs
all of them, and a failing script shows up as a failing test with its output.
Run a script directly when you want the per-check detail.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = sorted(p for p in HERE.glob("test_*.py") if p.name != Path(__file__).name)


def _clean_env() -> dict[str, str]:
    """The environment minus every CRUCIBLE_* value.

    Collection imports test_byok, which sets CRUCIBLE_OFFLINE for its own
    process; without this scrub that value leaks into every script launched
    here and a check written for a keyless refusal meets a run that succeeds.
    Each script owns its own environment.
    """
    return {k: v for k, v in os.environ.items()
            if not k.startswith("CRUCIBLE_")}


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.stem)
def test_script_passes(script: Path) -> None:
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, cwd=str(HERE.parent), timeout=600,
                            env=_clean_env())
    assert result.returncode == 0, (
        f"{script.name} exited {result.returncode}\n"
        f"--- stdout (tail) ---\n{result.stdout[-3000:]}\n"
        f"--- stderr (tail) ---\n{result.stderr[-3000:]}")
