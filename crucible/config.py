"""Where a run gets its settings, and who wins when two places disagree.

A tool that only takes flags cannot be used the same way twice. The point of a
file is that the configuration for reviewing a repository lives in that
repository, gets reviewed like anything else in it, and produces the same run on
a laptop and in a pipeline. Flags stay, because a one-off needs to override
without editing a committed file, and they win when both are present.

Secrets never live here. The file names an environment variable and the value
is read from the environment at the moment it is needed, so a config file is
safe to commit and safe to paste into a bug report.

Three layers, lowest first:

    built-in defaults  ->  crucible.toml  ->  command line flags

Model choice is per tier rather than one name for the whole run, because the
tiers are the only reason the cost story works: the wide fan-out can run on
something cheap or local while the seat where being right matters runs on
something strong. Collapsing that to a single model is the most expensive
mistake available in the config, so it stays impossible to express by accident.
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .providers import DEFAULT_MODELS, Tier

CONFIG_NAME = "crucible.toml"

DEFAULT_TASK = ("Review this codebase for correctness defects. Report only "
                "real bugs that produce wrong behaviour.")

# What a run's exit code is allowed to hang on. "survived" fails the pipeline
# when a finding got through every verifier, which is the useful gate. "never"
# is for a run that reports without blocking, which is how most teams will want
# to start.
FAIL_MODES = ("survived", "never")


class ConfigError(ValueError):
    """A configuration that cannot be used, said plainly enough to fix."""


@dataclass
class Config:
    """Everything a run needs that is not the code under review."""

    base_url: str = "https://api.openai.com/v1"
    api_key_env: str = "OPENAI_API_KEY"
    # Whether calls cost money. A local endpoint bills nothing, and pricing it
    # against the hosted rate table would have the budget refuse a free run.
    # Declared rather than guessed: a model served from a company's own vLLM
    # cluster is not localhost and is still not billed by the token.
    metered: bool = True
    models: dict = field(default_factory=lambda: dict(DEFAULT_MODELS))
    task: str = DEFAULT_TASK
    ceiling_usd: float = 1.00
    max_workers: int = 6
    fail_on: str = "survived"
    # Whether a run that could not finish blocks the pipeline. On by default,
    # because the alternative is a gate that reports success for work it did not
    # do. Off is a decision a team can make out loud, not one they should reach
    # by turning the whole check off.
    require_complete: bool = True
    # Where it came from, for the run header. A report that cannot say which
    # configuration produced it is a report nobody can reproduce.
    source: str = "built-in defaults"

    @property
    def api_key(self) -> str:
        return os.environ.get(self.api_key_env, "")

    def require_key(self) -> str:
        """The key, or a message naming the variable that is empty.

        Unmetered endpoints accept anything, so a missing key there is not an
        error and must not be reported as one.
        """
        key = self.api_key
        if key or not self.metered:
            return key or "unused-local-key"
        raise ConfigError(
            f"no API key: ${self.api_key_env} is empty. Export it, or point "
            f"[provider] at a local endpoint and set metered = false."
        )

    def describe(self) -> str:
        seats = ", ".join(f"{t.value}={self.models[t]}" for t in Tier)
        billing = "metered" if self.metered else "unmetered"
        return f"{self.base_url} ({billing}) — {seats}"


def find_config(start: Path) -> Path | None:
    """The nearest crucible.toml at or above a directory.

    Walking upward means a monorepo can carry one file at its root and have it
    apply to every package reviewed inside it, which is the arrangement most
    teams already use for every other tool they run.
    """
    start = Path(start).resolve()
    for directory in (start, *start.parents):
        candidate = directory / CONFIG_NAME
        if candidate.is_file():
            return candidate
    return None


def _reject_strays(raw: dict, path: Path) -> None:
    """Refuse a key nobody reads, rather than ignoring it.

    A tightened ceiling written as `celing_usd`, or a `[gate]` block spelled
    `[gates]`, is dropped in silence and the run proceeds on the default. The
    author believes the limit is in force, the file says it is, and only the
    behaviour disagrees. Every other refusal in this module exists for the same
    reason: a setting that does nothing should say so.
    """
    known_sections = {"provider", "run", "gate", "models"}
    strays = sorted(set(raw) - known_sections)
    if strays:
        raise ConfigError(
            f"{path}: unknown section(s) {', '.join(strays)}. "
            f"Sections are: {', '.join(sorted(known_sections))}."
        )
    known_keys = {
        "provider": {"base_url", "api_key_env", "metered"},
        "run": {"task", "ceiling_usd", "max_workers"},
        "gate": {"fail_on", "require_complete"},
    }
    for section, allowed in known_keys.items():
        block = raw.get(section)
        if not isinstance(block, dict):
            continue
        unknown = sorted(set(block) - allowed)
        if unknown:
            raise ConfigError(
                f"{path}: [{section}] has no setting called "
                f"{', '.join(unknown)}. Settings are: {', '.join(sorted(allowed))}."
            )


def _table(raw: dict, name: str, path: Path) -> dict:
    """One section, or a refusal naming it.

    `run = 3` is valid TOML and gives a section that is an integer. Read with
    .get it silently contributes nothing, so every setting under it is ignored
    and the run proceeds on defaults the author never chose. Saying so is the
    difference between a typo and a mystery.
    """
    value = raw.get(name)
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ConfigError(
            f"{path}: [{name}] must be a section, found "
            f"{type(value).__name__}. Write it as [{name}] on its own line."
        )
    return value


def _number(value, section: str, key: str, cast):
    """A number, or a refusal that names the setting rather than a traceback.

    float("1.00 AUD") and int("6 workers") raise ValueError, a dict or list
    raises TypeError, and int(float("inf")) raises OverflowError. None of them
    is a ConfigError, so each escapes the handler in main() and ends the process
    with exit code 1, the code reserved for a finding surviving verification. A
    malformed config would then be indistinguishable to a pipeline from a
    genuine review failure.

    Booleans are refused rather than cast. TOML has a real boolean type and
    Python's bool is an int, so `max_workers = true` becomes 1 and runs the
    whole review serially without a word. A setting that means something else
    entirely should say so instead of quietly working.

    Non-finite values are refused for a sharper reason. `ceiling_usd = nan` gets
    past a `<= 0` guard, because every comparison against NaN is False, and then
    every comparison inside the budget is False too: the ceiling stops
    refusing anything and the run has no spend limit at all.
    """
    import math

    if isinstance(value, bool):
        raise ConfigError(
            f"[{section}] {key} must be a number, found the boolean {value!r}"
        )
    try:
        number = cast(value)
    except (TypeError, ValueError, OverflowError) as exc:
        raise ConfigError(
            f"[{section}] {key} must be a number, found {value!r}"
        ) from exc
    if not math.isfinite(number):
        raise ConfigError(
            f"[{section}] {key} must be a finite number, found {value!r}"
        )
    return number


def _models_from(raw: dict, base: dict) -> dict:
    """Per-tier model names, refusing anything that is not a tier.

    A typo like `wroker = "gpt-5-mini"` would otherwise be accepted silently and
    the run would use the default for that seat, which is the sort of quiet
    wrongness that costs money and gets discovered in a bill.
    """
    models = dict(base)
    known = {t.value: t for t in Tier}
    for name, value in raw.items():
        tier = known.get(name)
        if tier is None:
            raise ConfigError(
                f"[models] has no seat called '{name}'. "
                f"Seats are: {', '.join(sorted(known))}."
            )
        if not isinstance(value, str) or not value.strip():
            raise ConfigError(f"[models] {name} needs a model name")
        models[tier] = value.strip()
    return models


def load(path: Path | None) -> Config:
    """Read a config file, or return the defaults if there is none."""
    if path is None:
        return Config()
    try:
        raw = tomllib.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        # A file saved as UTF-16 by a Windows editor, which is the ordinary way
        # this happens. Left uncaught it escapes as neither OSError nor a TOML
        # error and takes the process down on a traceback.
        raise ConfigError(
            f"{path} is not UTF-8 text: {exc.reason}. Save it as UTF-8."
        ) from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path} is not valid TOML: {exc}") from exc

    _reject_strays(raw, path)
    provider = _table(raw, "provider", path)
    run = _table(raw, "run", path)
    gate = _table(raw, "gate", path)
    config = Config(source=str(path))

    if "base_url" in provider:
        config.base_url = str(provider["base_url"]).rstrip("/")
    if "api_key_env" in provider:
        config.api_key_env = str(provider["api_key_env"])
    if "metered" in provider:
        config.metered = bool(provider["metered"])

    config.models = _models_from(_table(raw, "models", path), config.models)

    if "task" in run:
        config.task = str(run["task"])
    if "ceiling_usd" in run:
        config.ceiling_usd = _number(run["ceiling_usd"], "run", "ceiling_usd",
                                     float)
        if config.ceiling_usd <= 0:
            raise ConfigError("[run] ceiling_usd must be greater than zero")
    if "max_workers" in run:
        config.max_workers = max(1, _number(run["max_workers"], "run",
                                            "max_workers", int))
    if "require_complete" in gate:
        config.require_complete = bool(gate["require_complete"])
    if "fail_on" in gate:
        mode = str(gate["fail_on"]).strip().lower()
        if mode not in FAIL_MODES:
            raise ConfigError(
                f"[gate] fail_on must be one of {', '.join(FAIL_MODES)}"
            )
        config.fail_on = mode
    return config


STARTER = """\
# Crucible. Committed to the repository it reviews, so the same run happens on
# a laptop and in a pipeline. No secrets here: the key is named, never written.

[provider]
base_url    = "https://api.openai.com/v1"
api_key_env = "OPENAI_API_KEY"
metered     = true

# A local model instead. Anything speaking the OpenAI chat API works:
# llama.cpp, Ollama, vLLM, LM Studio.
#
# [provider]
# base_url = "http://localhost:8080/v1"
# metered  = false

# One model per seat. The planner divides the work once, workers fan out wide
# and cheap, and verifiers attack what the workers found.
#
# Worth knowing before you put a small model in the verifier seat: verifiers
# are told to default to refuted, and a model that is uncertain about
# everything refutes everything. It then reports zero survivors, which looks
# exactly like a clean bill of health and means the opposite.
[models]
planner  = "gpt-5"
worker   = "gpt-5-mini"
verifier = "gpt-5"

[run]
task        = "Review this codebase for correctness defects. Report only real bugs that produce wrong behaviour."
ceiling_usd = 1.00
max_workers = 6

[gate]
# "survived" exits non-zero when a finding survived every verifier, so a
# pipeline can block on it. "never" reports without blocking.
fail_on = "survived"

# A run that could not finish exits 2 rather than reporting its empty finding
# list as a pass. Turn this off only deliberately: with it off, a review whose
# agents all died is indistinguishable from a clean one.
require_complete = true
"""
