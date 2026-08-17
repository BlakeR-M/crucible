"""Model access, with the money counted on the way past.

Two things live here. A thin provider interface, so the arena is not written
against one vendor and the hosted demo and the operator's own machine can run
different models without the orchestrator noticing. And a budget that is
checked before every call rather than totalled afterwards, because a cap you
discover you have exceeded is a bill, not a cap.

Tiers rather than model names in the calling code. The orchestrator asks for
the tier the work deserves, and which model that means is one table here. That
is the whole cost story: the expensive seat is the planner and the adversarial
verifier, and everything that fans out wide runs cheap.
"""

from __future__ import annotations

import json
import os
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path


class Tier(str, Enum):
    """What a call is worth, decided by the work rather than by the caller."""

    PLANNER = "planner"      # decomposes the task; runs once or twice
    WORKER = "worker"        # the fan-out; runs many times in parallel
    VERIFIER = "verifier"    # tries to destroy a finding; quality is the point


# Rates in US dollars per million tokens, input and output. These drive the
# budget guard, so they are deliberately rounded up: a cap computed from a
# stale-but-generous rate stops early, which is the safe direction. Check them
# against current pricing before quoting a real figure to anyone.
RATES: dict[str, tuple[float, float]] = {
    "gpt-5": (1.25, 10.00),
    "gpt-5-mini": (0.25, 2.00),
    "gpt-4.1-mini": (0.40, 1.60),
    "o4-mini": (1.10, 4.40),
}

DEFAULT_MODELS: dict[Tier, str] = {
    Tier.PLANNER: "gpt-5",
    Tier.WORKER: "gpt-5-mini",
    Tier.VERIFIER: "gpt-5",
}

# Models that think before they answer, and charge for the thinking out of the
# same allowance as the answer. A gpt-5 call capped at 400 output tokens spends
# all 400 reasoning and returns an empty string, which reads exactly like a
# refusal and is not one. Found by smoke test rather than by reading the docs,
# which is why the smoke test exists.
REASONING_MODELS = ("gpt-5", "o3", "o4")

# Headroom added on top of the caller's request for those models, so the answer
# has somewhere to go once the reasoning is paid for.
REASONING_HEADROOM = 4000


class BudgetExceeded(RuntimeError):
    """Raised before a call that would take the run past its ceiling."""


@dataclass
class Budget:
    """A ceiling in dollars, held against calls that have not finished yet.

    Checking "have I spent too much" and then spending is two steps, and a
    dozen agents run these in parallel. Every one of them can pass the check
    against the same low total a moment before any of them pays, and the run
    sails past its ceiling with every individual check having been correct.

    So a call reserves its worst case up front, at the pessimistic price of its
    full output allowance, and settles to the real usage afterwards. The
    ceiling therefore holds against work in flight rather than only against
    work already billed, and a run stops slightly early rather than at some
    number nobody chose.
    """

    ceiling_usd: float
    spent_usd: float = 0.0
    calls: int = 0
    by_model: dict[str, float] = field(default_factory=dict)
    # A run against hardware the operator owns bills nothing, and that is a
    # property of the run rather than of the model's name. Carried here instead
    # of by writing zero rates into the module-level table, which was the
    # earlier approach and was wrong three ways: setdefault cannot zero a name
    # already in the table, so a local build called "gpt-5" kept the hosted
    # price and its free run was refused by its own ceiling; the write was
    # never undone, so one unmetered provider zeroed that model for every later
    # run in the process; and a global mutated at construction time made two
    # tests sharing an interpreter depend on their order.
    unmetered: bool = False
    _reserved_usd: float = 0.0
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def reserve(self, model: str, prompt_tokens: int, max_output: int) -> float:
        """Hold the worst case, or refuse. Returns the amount held."""
        worst_case = self._price(model, prompt_tokens, max_output)
        with self._lock:
            committed = self.spent_usd + self._reserved_usd
            if committed + worst_case > self.ceiling_usd:
                raise BudgetExceeded(
                    f"run has committed ${committed:.4f} of its "
                    f"${self.ceiling_usd:.2f} ceiling, and this {model} call "
                    f"could take it past"
                )
            self._reserved_usd += worst_case
            return worst_case

    def settle(self, model: str, held: float, prompt_tokens: int,
               output_tokens: int) -> float:
        """Release the hold and book what the call actually cost."""
        cost = self._price(model, prompt_tokens, output_tokens)
        with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - held)
            self.spent_usd += cost
            self.calls += 1
            self.by_model[model] = self.by_model.get(model, 0.0) + cost
        return cost

    def release(self, held: float) -> None:
        """Give back a hold for a call that never billed, such as one that
        raised on the way out. Without this, failures leak the ceiling away."""
        with self._lock:
            self._reserved_usd = max(0.0, self._reserved_usd - held)

    def guard(self, model: str, prompt_tokens: int, max_output: int) -> None:
        """Reserve and immediately release. For callers that only want the
        check, and for the tests that assert the ceiling refuses."""
        self.release(self.reserve(model, prompt_tokens, max_output))

    def record(self, model: str, prompt_tokens: int, output_tokens: int) -> float:
        """Book a cost with no prior reservation, for providers that do not
        reserve, such as the scripted one used in tests."""
        return self.settle(model, 0.0, prompt_tokens, output_tokens)

    def _price(self, model: str, prompt_tokens: int, output_tokens: int) -> float:
        if self.unmetered:
            return 0.0
        # An unknown model is priced at the most expensive known rate rather
        # than at zero, so adding a model cannot silently disable the cap.
        rate_in, rate_out = RATES.get(model, max(RATES.values()))
        return (prompt_tokens * rate_in + output_tokens * rate_out) / 1_000_000

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.ceiling_usd - self.spent_usd)


@dataclass(frozen=True)
class Completion:
    text: str
    model: str
    prompt_tokens: int
    output_tokens: int
    cost_usd: float
    seconds: float


DEFAULT_BASE_URL = "https://api.openai.com/v1"


class OpenAIProvider:
    """Chat completions over plain urllib, so the arena has no dependencies.

    Written against the OpenAI chat API, which is the closest thing this field
    has to a wire standard: llama.cpp, Ollama, vLLM and LM Studio all speak it.
    So pointing the arena at a model on the operator's own hardware is a base
    URL rather than a second implementation, and the code path that gets
    exercised against a hosted model is the same one that runs air-gapped.

    Retries only the failures that are worth retrying: rate limits and 5xx. A
    refusal, a bad request or an auth failure is returned as an error rather
    than attempted four more times at the budget's expense.
    """

    name = "openai"

    def __init__(self, api_key: str, models: dict[Tier, str] | None = None,
                 *, max_attempts: int = 3, base_url: str | None = None,
                 metered: bool = True):
        if not api_key and metered:
            raise ValueError("OpenAIProvider needs an API key")
        self._key = api_key or "unused-local-key"
        self.models = dict(DEFAULT_MODELS if models is None else models)
        self.max_attempts = max_attempts
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.ENDPOINT = f"{self.base_url}/chat/completions"
        self.metered = metered

    @property
    def _token_field(self) -> str:
        """Which spelling of the output cap this endpoint accepts.

        The gpt-5 family rejects max_tokens outright and requires
        max_completion_tokens. Most OpenAI-compatible servers implement the
        older name and some have never heard of the newer one. Keyed off the
        host rather than the model, because it is the server that decides which
        field it will parse.

        Matched on the parsed hostname rather than by searching the whole URL
        for the string. A substring test says yes to
        http://evil.test/?x=api.openai.com and to a corporate proxy at
        api.openai.com.internal.example, and says no to Azure OpenAI, which
        needs the newer spelling and lives on a different host entirely.
        """
        from urllib.parse import urlsplit

        host = (urlsplit(self.base_url).hostname or "").lower()
        official = host == "api.openai.com"
        azure = host.endswith(".openai.azure.com")
        return "max_completion_tokens" if official or azure else "max_tokens"

    def complete(self, system: str, user: str, tier: Tier, budget: Budget,
                 *, max_output: int = 2000, reasoning: str = "low") -> Completion:
        model = self.models[tier]
        thinks = model.startswith(REASONING_MODELS)
        # Headroom for anything that reasons before it answers, and for every
        # unmetered model whether it does or not. The hosted reasoning families
        # are known by name; a local one is called whatever its author felt
        # like, and a Qwen or DeepSeek build that thinks will spend the whole
        # allowance doing it and return an empty string that reads exactly like
        # a refusal. Local tokens cost nothing, so the generous default is free.
        allowance = max_output + (REASONING_HEADROOM
                                  if thinks or not self.metered else 0)

        # Four characters to a token is the usual rough guide and it is close
        # enough for a guard that only has to be conservative. The guard prices
        # the full allowance, reasoning included, so the cap accounts for
        # tokens the caller never sees.
        estimated_prompt = (len(system) + len(user)) // 4
        held = budget.reserve(model, estimated_prompt, allowance)

        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            self._token_field: allowance,
        }
        if thinks and self.metered:
            # Kept low on purpose. These seats want judgement rather than long
            # deliberation, and the reasoning is the expensive part.
            body["reasoning_effort"] = reasoning
        started = time.time()
        try:
            payload = self._post(body)
        except Exception:
            # A call that never billed must give its hold back, or a run that
            # hits a few transient errors starves itself out of its own ceiling.
            budget.release(held)
            raise
        elapsed = time.time() - started

        try:
            choice = payload["choices"][0]
            text = choice["message"].get("content") or ""
            finish = choice.get("finish_reason", "")
        except (KeyError, IndexError, TypeError) as exc:
            budget.release(held)
            raise RuntimeError(f"{model} returned no usable choice: {exc}") from exc

        usage = payload.get("usage") or {}
        prompt_tokens = usage.get("prompt_tokens", estimated_prompt)
        # A missing completion count booked as zero would make an expensive
        # call free, and enough of those disable the ceiling entirely. Fall
        # back to the length actually returned rather than to nothing.
        output_tokens = usage.get("completion_tokens")
        if not isinstance(output_tokens, int):
            output_tokens = max(1, len(text) // 4)
        cost = budget.settle(model, held, prompt_tokens, output_tokens)
        if not text.strip() and finish == "length":
            # Silence because the allowance ran out reads identically to a
            # model declining to answer. Say which one it was, so a run that
            # goes quiet is diagnosable rather than mysterious.
            raise RuntimeError(
                f"{model} used its entire {allowance} token allowance without "
                f"producing an answer. Raise max_output or lower reasoning effort."
            )
        return Completion(text.strip(), model, prompt_tokens, output_tokens, cost, elapsed)

    def _post(self, body: dict) -> dict:
        data = json.dumps(body).encode("utf-8")
        last: Exception | None = None
        for attempt in range(self.max_attempts):
            req = urllib.request.Request(
                self.ENDPOINT,
                data=data,
                headers={
                    "Authorization": f"Bearer {self._key}",
                    "Content-Type": "application/json",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=180) as resp:
                    return json.loads(resp.read().decode("utf-8"))
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:400]
                if exc.code in (429, 500, 502, 503, 504) and attempt < self.max_attempts - 1:
                    time.sleep(2 ** attempt)
                    last = exc
                    continue
                raise RuntimeError(f"openai {exc.code}: {detail}") from exc
            except urllib.error.URLError as exc:
                if attempt < self.max_attempts - 1:
                    time.sleep(2 ** attempt)
                    last = exc
                    continue
                raise RuntimeError(f"openai unreachable: {exc}") from exc
        raise RuntimeError(f"openai failed after {self.max_attempts} attempts: {last}")


class ScriptedProvider:
    """Fixed answers, for tests and for a demo that must not spend money.

    Every check in the suite runs through this, so the orchestrator's control
    flow is tested without a network call, a key, or a cent.
    """

    name = "scripted"

    def __init__(self, replies: list[str]):
        self._replies = list(replies)
        self.calls: list[tuple[Tier, str]] = []

    def complete(self, system: str, user: str, tier: Tier, budget: Budget,
                 *, max_output: int = 2000) -> Completion:
        self.calls.append((tier, user))
        text = self._replies.pop(0) if self._replies else "{}"
        budget.record("gpt-5-mini", len(user) // 4, len(text) // 4)
        return Completion(text, "scripted", len(user) // 4, len(text) // 4, 0.0, 0.0)


def load_env(path: Path) -> dict[str, str]:
    """Read a KEY=value file without importing anything.

    Values are never logged by this module. The caller gets a plain dict and is
    responsible for keeping it out of the run record.
    """
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def provider_from_env(*, extra_env: Path | None = None):
    """Prefer the process environment, fall back to a named env file.

    On Railway the key arrives as a real environment variable. On this machine
    it currently lives in a file, so both paths work and neither is written
    into the repository.
    """
    key = os.environ.get("OPENAI_API_KEY", "")
    if not key and extra_env is not None:
        key = load_env(extra_env).get("OPENAI_API_KEY", "")
    if not key:
        raise RuntimeError(
            "no OPENAI_API_KEY in the environment or the named env file"
        )
    return OpenAIProvider(key)
