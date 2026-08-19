"""Bring your own key: a visitor's provider key, held for one session in memory.

The hosted arena spends the operator's money by default, and the operator has
a daily ceiling for exactly that reason. A visitor who wants a live model past
that ceiling, or on a deployment that runs the free stand-in, can attach a key
of their own for the length of their session. Three properties hold:

The key lives in this process's memory and nowhere else. It is keyed by the
session cookie, expires when the cookie does, and is dropped on logout. It is
never written to disk, to a ledger, to an event, or to a log line, and it is
never echoed back to the browser.

The provider is one of two named vendors. A visitor names "openai" or
"gemini" and the base URL is ours; a stranger's URL would turn the hosted box
into a request proxy, so there is no field for one.

The models come from a fixed list. Every model a visitor may name is priced in
the rates table, so the budget that caps a run can always price the call.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.error
import urllib.request

from .providers import (
    DEFAULT_BASE_URL, DEFAULT_MODELS, GEMINI_BASE_URL, GEMINI_DEFAULT_MODELS,
    RATES, Tier,
)

PROVIDERS: dict[str, dict] = {
    "openai": {"base_url": DEFAULT_BASE_URL, "defaults": DEFAULT_MODELS,
               "models": tuple(m for m in RATES if not m.startswith("gemini"))},
    "gemini": {"base_url": GEMINI_BASE_URL, "defaults": GEMINI_DEFAULT_MODELS,
               "models": tuple(m for m in RATES if m.startswith("gemini"))},
}

# A key is a printable token. Anything else is refused before it goes near a
# request header, where a newline would be a header injection.
KEY_SHAPE = re.compile(r"^[A-Za-z0-9._\-]{16,256}$")

REDACTED = "[redacted]"
# The shortest run of a key that is still recognisably the key. A provider's
# error body is clipped to a few hundred characters before it becomes a
# message, and a clip that lands inside the key leaves a prefix; prefixes at
# least this long are scrubbed too.
FRAGMENT_MIN = 8


class Redactor:
    """Scrubs registered secrets, and their leading fragments, out of text.

    Applied at the boundaries where text leaves the process: the ledger, the
    event stream, an exception message, stderr. A provider's 4xx body may
    quote the Authorization header it rejected, and that body is what becomes
    the error message, so scrubbing at the message is the safe place rather
    than trusting every vendor's error format.
    """

    def __init__(self, secrets=()):
        self._lock = threading.Lock()
        self._secrets: set[str] = set()
        for secret in secrets:
            self.add(secret)

    def add(self, secret: str) -> None:
        if secret and len(secret) >= FRAGMENT_MIN:
            with self._lock:
                self._secrets.add(secret)

    def discard(self, secret: str) -> None:
        with self._lock:
            self._secrets.discard(secret)

    def scrub(self, text: str) -> str:
        with self._lock:
            secrets = list(self._secrets)
        for secret in secrets:
            if secret in text:
                text = text.replace(secret, REDACTED)
            # Longest prefix first, so a partial key becomes one marker
            # rather than a marker followed by the tail of the key.
            for length in range(len(secret) - 1, FRAGMENT_MIN - 1, -1):
                fragment = secret[:length]
                if fragment in text:
                    text = text.replace(fragment, REDACTED)
        return text

    def scrub_value(self, value):
        """The same scrub, walked through a JSON-shaped value."""
        if isinstance(value, str):
            return self.scrub(value)
        if isinstance(value, dict):
            return {k: self.scrub_value(v) for k, v in value.items()}
        if isinstance(value, (list, tuple)):
            return [self.scrub_value(v) for v in value]
        return value

    def __bool__(self) -> bool:
        with self._lock:
            return bool(self._secrets)


def probe_models(base_url: str, api_key: str, *, timeout: float = 15.0) -> None:
    """One cheap request that a key must pass before it is kept.

    GET /models is the lightest authenticated call both vendors offer. A
    rejected key raises with a sentence and no body, since the body is where a
    vendor might echo the header. Held as a module-level name so a check can
    stand a fake in for the network.
    """
    req = urllib.request.Request(
        f"{base_url.rstrip('/')}/models",
        headers={"Authorization": f"Bearer {api_key}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            raise ValueError("the provider rejected that key") from None
        raise ValueError(f"the provider answered {exc.code} to a models "
                         f"request, so the key was kept out") from None
    except urllib.error.URLError as exc:
        raise ValueError(f"the provider could not be reached: "
                         f"{type(exc.reason).__name__ if exc.reason else 'no route'}") from None
    except (ValueError, OSError):
        raise ValueError("the provider answered with something other than a "
                         "model list, so the key was kept out") from None


def choose_models(provider: str, wanted: dict | None) -> dict[Tier, str]:
    """The provider's tier defaults, with any override checked against the
    allowlist. Raises ValueError naming the first model that is not on it."""
    spec = PROVIDERS[provider]
    models = dict(spec["defaults"])
    for tier in Tier:
        name = str((wanted or {}).get(tier.value) or "").strip()
        if not name:
            continue
        if name not in spec["models"]:
            raise ValueError(f"{name} is not a model this arena can price for "
                             f"{provider}; pick from {', '.join(spec['models'])}")
        models[tier] = name
    return models


def validate_key(provider: str, api_key: str, models: dict | None) -> dict:
    """Admit a key or raise ValueError with the sentence to show.

    Returns the record to hold: provider, key, base_url and per-tier models.
    Nothing is kept on failure; the caller only ever sees the record or the
    sentence.
    """
    provider = str(provider or "").strip().lower()
    if provider not in PROVIDERS:
        raise ValueError("provider must be openai or gemini")
    api_key = str(api_key or "").strip()
    if not KEY_SHAPE.match(api_key):
        raise ValueError("that does not read as an API key")
    chosen = choose_models(provider, models)
    base_url = PROVIDERS[provider]["base_url"]
    # Looked up by name at call time, so a check can replace it.
    probe = globals()["probe_models"]
    probe(base_url, api_key)
    return {"provider": provider, "key": api_key, "base_url": base_url,
            "models": chosen}


class VisitorKeys:
    """Session id to attached key, in memory, with the session's expiry.

    The one place a visitor's key exists. Reads drop an entry whose session
    has expired, so an expired cookie and a forgotten key are the same event.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._keys: dict[str, dict] = {}

    def attach(self, sid: str, record: dict, *, ceiling_usd: float,
               expires: float) -> None:
        with self._lock:
            self._keys[sid] = {**record, "ceiling_usd": float(ceiling_usd),
                               "expires": float(expires), "spent_usd": 0.0}

    def detach(self, sid: str) -> bool:
        with self._lock:
            return self._keys.pop(sid, None) is not None

    def get(self, sid: str) -> dict | None:
        """A copy of the record, or None. Never the live dict, so a caller
        cannot mutate the table by accident."""
        with self._lock:
            held = self._keys.get(sid)
            if held is None:
                return None
            if held["expires"] <= time.time():
                del self._keys[sid]
                return None
            return dict(held)

    def add_spend(self, sid: str, usd: float) -> None:
        with self._lock:
            held = self._keys.get(sid)
            if held is not None:
                held["spent_usd"] += float(usd)

    def status(self, sid: str) -> dict:
        """What the browser may know: everything except the key."""
        held = self.get(sid)
        if held is None:
            return {"attached": False}
        return {
            "attached": True,
            "provider": held["provider"],
            "models": {tier.value: name for tier, name in held["models"].items()},
            "ceiling_usd": round(held["ceiling_usd"], 2),
            "spent_usd": round(held["spent_usd"], 5),
        }

    def reap(self) -> None:
        with self._lock:
            now = time.time()
            for sid in [k for k, v in self._keys.items() if v["expires"] <= now]:
                del self._keys[sid]

    def count(self) -> int:
        with self._lock:
            return len(self._keys)
