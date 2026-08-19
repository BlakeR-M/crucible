"""A model running on this machine, and the option to constrain what it emits.

Two reasons this exists rather than a flag on the OpenAI provider.

The first is money. Every other provider is priced per token against a rate
table, and an unknown model deliberately prices at the most expensive known
rate so that adding one cannot silently disable the budget ceiling. A local
model genuinely costs nothing, so it declares itself unmetered and the Budget
it is handed prices every call at zero. That belongs on the run rather than on
the model's name: an earlier version wrote zero rates into the module-level
table instead, which could not zero a name already priced and never undid
itself.

The second is the experiment this was written for. llama.cpp will accept a JSON
schema alongside the prompt, compile it to a grammar, and refuse to sample any
token that would take the output outside it, which makes a malformed tool call
not unlikely but unrepresentable. Whether that helps a small model do agentic
work, or whether it buys well-formed nonsense by taking away the model's room
to think, is the question. Both arms therefore have to run through the same
code path with the constraint as the only difference between them, which is
what this class is: one provider, one optional argument.
"""

from __future__ import annotations

import json
import time

from .providers import Budget, Completion, OpenAIProvider, Tier

# The local server accepts any key and checks none of them. Sent anyway because
# the shared _post always sets the header.
UNUSED_KEY = "local-no-key-required"

# Extra output allowance for a model that reasons before it answers. Sized from
# the failure it exists to prevent rather than guessed: transcripts late in an
# agent loop are long, the reasoning grows with them, and 4000 was still not
# enough to stop two verifiers in a real run going quiet.
THINKING_HEADROOM = 8_000


class LocalProvider(OpenAIProvider):
    """llama.cpp's OpenAI-compatible server, priced at nothing.

    The schema, when given, is chosen per call from the system prompt, the same
    way the offline provider picks its scripted answer. The provider has no
    other way to know which seat it is filling, and the orchestrator should not
    have to grow a parameter for the benefit of one experiment.
    """

    name = "local"

    def __init__(self, base_url: str, model: str, *, schema_for=None,
                 on_reply=None, max_attempts: int = 3, think: bool = False,
                 temperature: float = 0.7, seed: int = 1234):
        # Skips OpenAIProvider.__init__ only in that it tolerates no key.
        super().__init__(UNUSED_KEY, models={t: model for t in Tier},
                         max_attempts=max_attempts, base_url=base_url,
                         metered=False)
        self.ENDPOINT = base_url.rstrip("/") + "/v1/chat/completions"
        self.model = model
        # A callable taking the system prompt and returning a JSON schema, or
        # None for the unconstrained arm. Kept as a callable rather than a dict
        # so the caller decides how roles are recognised.
        self._schema_for = schema_for
        # Every raw reply, handed out before anything parses it. The whole
        # measurement depends on seeing what the model actually wrote rather
        # than what the tolerant parser managed to make of it.
        self._on_reply = on_reply
        self._think = think
        # Greedy decoding looked like the careful choice and is the wrong one
        # here. Three verifiers judging one finding get the same prompt, so at
        # temperature zero they return character-identical verdicts: the panel
        # is one opinion counted three times, and a threshold of two out of
        # three is really one out of one. Observed, not theorised, in the first
        # run of this benchmark. So the arms sample, and independence is real.
        self._temperature = temperature
        # Reproducible anyway: the seed advances by call, execution is serial,
        # so the same script over the same workspace replays the same run.
        self._seed = seed
        self._calls = 0
        self._seed_lock = __import__("threading").Lock()

    def _next_seed(self) -> int:
        with self._seed_lock:
            self._calls += 1
            return self._seed + self._calls

    def complete(self, system: str, user: str, tier: Tier, budget: Budget,
                 *, max_output: int = 2000, reasoning: str = "low") -> Completion:
        model = self.model
        estimated_prompt = (len(system) + len(user)) // 4
        held = budget.reserve(model, estimated_prompt, max_output)

        body = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            # The local server takes the older spelling. max_completion_tokens
            # is accepted by newer builds but max_tokens works on every one.
            #
            # A thinking model is paid for its thinking out of this same
            # allowance, and llama.cpp routes the <think> block into
            # reasoning_content while leaving content empty. Handed the bare
            # 2200 an agent step asks for, Qwen3.5 spent all of it reasoning and
            # returned an empty string on two replies in five. The first
            # thinking-on arms of this benchmark ran that way and measured
            # nothing but the allowance being too small: 89% of their dead
            # replies were empty, every one of them stopped on length, every one
            # at exactly 2200 tokens. Local tokens are free, so the headroom is
            # generous rather than tuned.
            "max_tokens": max_output + (THINKING_HEADROOM if self._think else 0),
            "temperature": self._temperature,
            "seed": self._next_seed(),
            # Qwen3.5 thinks by default and wraps it in <think> tags, which
            # llama.cpp routes to reasoning_content and leaves content empty.
            # A constraint over the whole output would forbid that block
            # outright, so thinking is off in both arms rather than in one, and
            # the comparison stays about the constraint. Verified by smoke
            # test: left on with a small allowance, the model spends every
            # token thinking and returns nothing, which reads exactly like a
            # refusal and is not one.
            "chat_template_kwargs": {"enable_thinking": self._think},
        }
        schema = self._schema_for(system) if self._schema_for else None
        if schema:
            body["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "step", "schema": schema, "strict": True},
            }

        started = time.time()
        try:
            payload = self._post(body)
        except Exception:
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
        output_tokens = usage.get("completion_tokens")
        if not isinstance(output_tokens, int):
            output_tokens = max(1, len(text) // 4)
        cost = budget.settle(model, held, prompt_tokens, output_tokens)

        if self._on_reply is not None:
            self._on_reply({
                "system": system,
                "text": text,
                "tier": tier.value,
                "constrained": bool(schema),
                "finish": finish,
                "prompt_tokens": prompt_tokens,
                "output_tokens": output_tokens,
                "seconds": round(elapsed, 2),
            })

        # Deliberately not raising on an empty reply the way the hosted
        # provider does. A constrained model that runs out of allowance
        # mid-object is a result this experiment wants counted, not an
        # exception that ends the run before it is recorded.
        return Completion(text.strip(), model, prompt_tokens, output_tokens,
                          cost, elapsed)

    def health(self) -> dict:
        """Confirm the server is up and reports the model we think it has."""
        import urllib.request

        url = self.ENDPOINT.rsplit("/v1/", 1)[0] + "/v1/models"
        with urllib.request.urlopen(url, timeout=10) as resp:
            return json.loads(resp.read().decode("utf-8"))
