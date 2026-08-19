"""Runs every plain-script check file under pytest, one test per script.

The check files are plain scripts on purpose (they run anywhere Python does).
This wrapper exists so that a single `python -m pytest -q` in a pipeline runs
all of them, and a failing script shows up as a failing test with its output.
Run a script directly when you want the per-check detail.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
SCRIPTS = sorted(p for p in HERE.glob("test_*.py") if p.name != Path(__file__).name)


@pytest.mark.parametrize("script", SCRIPTS, ids=lambda p: p.stem)
def test_script_passes(script: Path) -> None:
    result = subprocess.run([sys.executable, str(script)], capture_output=True,
                            text=True, cwd=str(HERE.parent), timeout=600)
    assert result.returncode == 0, (
        f"{script.name} exited {result.returncode}\n"
        f"--- stdout (tail) ---\n{result.stdout[-3000:]}\n"
        f"--- stderr (tail) ---\n{result.stderr[-3000:]}")
