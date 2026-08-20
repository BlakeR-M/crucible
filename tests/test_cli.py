"""Checks for the command line tool: configuration, exit codes, verification.

Plain script, no test framework, so it runs anywhere Python does:
    python tests/test_cli.py

Three things are worth more than the rest here.

The exit-code contract, because a pipeline reads it and cannot see anything
else. The dangerous failure is a run that could not complete exiting zero, since
"checked and clean" and "never checked" would then be indistinguishable to
whatever ships next.

The verify command, because it is the project's headline claim. A verifier that
passes everything is worse than none, so the tampering it is supposed to catch
is constructed here and it has to fail. That includes a forgery whose hash chain
has been rebuilt correctly, which the chain alone cannot catch and only the
policy replay can.

And the provider's backward compatibility, because 427 existing checks were
written against a class that had one hardcoded endpoint and now takes a base URL.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crucible import config as cfg  # noqa: E402
from crucible.ledger import GENESIS, Ledger, _digest  # noqa: E402
from crucible.policy import Policy, review_policy  # noqa: E402
from crucible.providers import (  # noqa: E402
    DEFAULT_BASE_URL, RATES, Budget, BudgetExceeded, OpenAIProvider, Tier,
)

ROOT = Path(__file__).resolve().parent.parent

PASSED = 0
FAILED: list[str] = []


def check(label: str, condition: bool, detail: str = "") -> None:
    global PASSED
    if condition:
        PASSED += 1
        print(f"  ok  {label}")
    else:
        FAILED.append(label)
        print(f"  FAIL  {label} {detail}")


def section(name: str) -> None:
    print(f"\n{name}")


def cli(*args: str) -> subprocess.CompletedProcess:
    """The tool as a pipeline would actually invoke it: a separate process,
    with a real exit code rather than a return value."""
    return subprocess.run(
        [sys.executable, "-m", "crucible.cli", *args],
        capture_output=True, text=True, cwd=str(ROOT),
    )


# ------------------------------------------------------------------- config

def config_checks(tmp: Path) -> None:
    section("config: layering")

    default = cfg.load(None)
    check("no file gives built-in defaults",
          default.base_url == DEFAULT_BASE_URL and default.metered)
    check("defaults name their source", "default" in default.source)

    path = tmp / "cfg" / cfg.CONFIG_NAME
    path.parent.mkdir(parents=True)
    path.write_text(
        '[provider]\n'
        'base_url = "http://localhost:9999/v1"\n'
        'metered = false\n'
        '[models]\n'
        'worker = "tiny"\n'
        '[run]\n'
        'ceiling_usd = 2.5\n'
        'max_workers = 3\n'
        '[gate]\n'
        'fail_on = "never"\n',
        encoding="utf-8")
    loaded = cfg.load(path)
    check("base_url comes from the file",
          loaded.base_url == "http://localhost:9999/v1")
    check("metered false is read as a bool", loaded.metered is False)
    check("a named seat is overridden", loaded.models[Tier.WORKER] == "tiny")
    check("seats left unnamed keep their default",
          loaded.models[Tier.PLANNER] == cfg.DEFAULT_MODELS[Tier.PLANNER])
    check("ceiling and workers are coerced",
          loaded.ceiling_usd == 2.5 and loaded.max_workers == 3)
    check("fail_on is read", loaded.fail_on == "never")

    section("config: refusing what cannot work")

    bad = tmp / "bad.toml"
    bad.write_text('[models]\nwroker = "gpt-5"\n', encoding="utf-8")
    try:
        cfg.load(bad)
        check("a typo'd seat name is refused", False, "no error raised")
    except cfg.ConfigError as exc:
        check("a typo'd seat name is refused", True)
        check("the refusal lists the real seats",
              all(t.value in str(exc) for t in Tier))

    broken = tmp / "broken.toml"
    broken.write_text("this is not toml = = =", encoding="utf-8")
    try:
        cfg.load(broken)
        check("malformed TOML is refused", False, "no error raised")
    except cfg.ConfigError:
        check("malformed TOML is refused", True)

    missing = tmp / "nope.toml"
    try:
        cfg.load(missing)
        check("an unreadable file is refused", False, "no error raised")
    except cfg.ConfigError:
        check("an unreadable file is refused", True)

    badgate = tmp / "badgate.toml"
    badgate.write_text('[gate]\nfail_on = "sometimes"\n', encoding="utf-8")
    try:
        cfg.load(badgate)
        check("an unknown fail_on is refused", False, "no error raised")
    except cfg.ConfigError:
        check("an unknown fail_on is refused", True)

    section("config: discovery and keys")

    nested = path.parent / "pkg" / "deep"
    nested.mkdir(parents=True)
    check("the nearest config is found by walking up",
          cfg.find_config(nested) == path.resolve())
    check("nothing is found where there is nothing",
          cfg.find_config(Path(tempfile.gettempdir()) / "crucible-absent") is None)

    metered = cfg.Config(api_key_env="CRUCIBLE_TEST_ABSENT_KEY")
    try:
        metered.require_key()
        check("a metered run without a key is refused", False, "no error")
    except cfg.ConfigError as exc:
        check("a metered run without a key is refused", True)
        check("the refusal names the variable to set",
              "CRUCIBLE_TEST_ABSENT_KEY" in str(exc))

    free = cfg.Config(api_key_env="CRUCIBLE_TEST_ABSENT_KEY", metered=False)
    check("an unmetered run needs no key", bool(free.require_key()))


# ----------------------------------------------------------------- providers

def provider_checks() -> None:
    section("providers: base url and billing")

    plain = OpenAIProvider("sk-test")
    check("the default endpoint is unchanged",
          plain.ENDPOINT == "https://api.openai.com/v1/chat/completions")
    check("the hosted endpoint takes max_completion_tokens",
          plain._token_field == "max_completion_tokens")

    local = OpenAIProvider("", {t: "some-local-model" for t in Tier},
                           base_url="http://127.0.0.1:8080/v1", metered=False)
    check("an unmetered provider needs no key", local.metered is False)
    check("a trailing slash on the base url is tolerated",
          OpenAIProvider("", {t: "m" for t in Tier},
                         base_url="http://x/v1/", metered=False).ENDPOINT
          == "http://x/v1/chat/completions")
    check("a local endpoint takes max_tokens",
          local._token_field == "max_tokens")

    check("declaring a provider unmetered leaves the rate table alone",
          "some-local-model" not in RATES)

    free_budget = Budget(ceiling_usd=0.01, unmetered=True)
    free_budget.guard("some-local-model", 100_000, 100_000)
    check("an unmetered budget does not exhaust a tiny ceiling",
          free_budget.remaining_usd == 0.01)
    # The name collision that setdefault could never fix: a local build called
    # after a priced model kept the hosted rate and refused its own free run.
    collide = Budget(ceiling_usd=0.01, unmetered=True)
    collide.guard("gpt-5", 1_000_000, 1_000_000)
    check("an unmetered run is free even when the model is named gpt-5",
          collide.remaining_usd == 0.01)
    metered_budget = Budget(ceiling_usd=0.01)
    try:
        metered_budget.guard("gpt-5", 1_000_000, 1_000_000)
        check("a metered budget still refuses what it cannot afford", False)
    except BudgetExceeded:
        check("a metered budget still refuses what it cannot afford", True)

    try:
        OpenAIProvider("")
        check("a metered provider still demands a key", False, "no error")
    except ValueError:
        check("a metered provider still demands a key", True)

    section("providers: pricing an unknown model stays pessimistic")
    dearest = max(RATES.values())
    check("an unknown metered model prices at the dearest rate",
          Budget(ceiling_usd=1.0)._price("model-nobody-registered",
                                         1_000_000, 0) == dearest[0])


# -------------------------------------------------------------------- policy

def policy_roundtrip_checks(tmp: Path) -> None:
    section("policy: rebuilt from what a run recorded")

    work = tmp / "ws"
    (work / "src").mkdir(parents=True)
    (work / "src" / "app.py").write_text("x = 1", encoding="utf-8")
    original = review_policy(work)
    rebuilt = Policy.from_dict(original.as_dict())

    check("the name survives the round trip", rebuilt.name == original.name)
    check("every tool survives", set(rebuilt.rules) == set(original.rules))

    inside = {"path": str(work / "src" / "app.py")}
    outside = {"path": str(tmp / "elsewhere.txt")}
    check("a permitted call is still permitted",
          bool(rebuilt.check("read_file", inside)))
    check("a call outside the workspace is still refused",
          not rebuilt.check("read_file", outside))
    check("an unlisted tool is still refused",
          not rebuilt.check("send_email", inside))
    check("the command allowlist survives",
          not rebuilt.check("run_tests", {"path": str(work), "command": "whoami"}))
    check("the interpreter rule survives",
          not rebuilt.check("run_tests",
                            {"path": str(work), "command": 'python -c "print(1)"'}))

    # Every tool, every recorded decision, compared against the original rather
    # than spot-checked. A round trip that drops one rule would otherwise pass.
    same = all(
        rebuilt.check(tool, args).allowed == original.check(tool, args).allowed
        for tool in set(original.rules) | {"send_email"}
        for args in (inside, outside, {"path": str(work), "command": "pytest"},
                     {"path": str(work), "command": "curl"}, {})
    )
    check("every decision matches the original policy", same)


# -------------------------------------------------------------------- verify

def _write_chain(path: Path, entries: list[dict]) -> None:
    path.write_text(
        "\n".join(json.dumps(e, sort_keys=True, separators=(",", ":"))
                  for e in entries) + "\n",
        encoding="utf-8")


def _rechain(entries: list[dict]) -> list[dict]:
    """Rebuild every hash so the file is internally perfect. This is what a
    competent forger produces, and what the chain alone cannot catch."""
    prev = GENESIS
    for entry in entries:
        entry["prev"] = prev
        entry["hash"] = _digest(entry["seq"], entry["ts"], entry["event"],
                                entry["payload"], prev)
        prev = entry["hash"]
    return entries


def verify_checks(tmp: Path) -> None:
    section("verify: an honest record")

    work = tmp / "vws"
    (work / "src").mkdir(parents=True)
    (work / "src" / "app.py").write_text("x = 1", encoding="utf-8")
    policy = review_policy(work)

    path = tmp / "run.jsonl"
    ledger = Ledger(path)
    ledger.append("run_started", run_id="test01", task="t",
                  workspace=str(work), policy=policy.as_dict(),
                  budget_ceiling_usd=1.0)
    ledger.append("tool_call", agent="hunter-1", tool="read_file",
                  args={"path": str(work / "src" / "app.py")})
    ledger.append("tool_denied", agent="prober", tool="read_file",
                  args={"path": str(tmp / "outside.txt")}, reason="outside")
    ledger.append("run_finished", run_id="test01", raised=0, survived=0,
                  spend_usd=0.0, halted="")
    honest = [json.loads(l) for l in path.read_text(encoding="utf-8").splitlines()]

    result = cli("verify", str(path))
    check("an intact record verifies", result.returncode == 0, result.stdout)
    check("it reports the chain intact", "chain     intact" in result.stdout)
    check("it reports replayed decisions", "replayed  2 tool decisions" in result.stdout)

    section("verify: tampering")

    naive = tmp / "naive.jsonl"
    edited = [dict(e) for e in honest]
    edited[2]["event"] = "tool_call"
    _write_chain(naive, edited)
    result = cli("verify", str(naive))
    check("a naive edit fails", result.returncode == 2)
    check("it names the broken entry", "CHAIN BROKEN" in result.stdout)

    forged = tmp / "forged.jsonl"
    rebuilt = _rechain([dict(e) for e in honest])
    rebuilt[2]["event"] = "tool_call"
    _rechain(rebuilt)
    _write_chain(forged, rebuilt)
    result = cli("verify", str(forged))
    check("a re-hashed forgery still has an intact chain",
          "chain     intact" in result.stdout)
    check("but the replay catches the flipped decision",
          result.returncode == 2 and "DO NOT REPRODUCE" in result.stdout,
          result.stdout)

    truncated = tmp / "truncated.jsonl"
    _write_chain(truncated, _rechain([dict(e) for e in honest[:-1]]))
    result = cli("verify", str(truncated))
    check("a run with no run_finished entry fails",
          result.returncode == 2 and "cut short" in result.stdout)

    headless = tmp / "headless.jsonl"
    _write_chain(headless, _rechain([dict(e) for e in honest[1:]]))
    result = cli("verify", str(headless))
    check("a record with no run_started fails", result.returncode == 2)

    missing = tmp / "absent.jsonl"
    check("a ledger that does not exist fails",
          cli("verify", str(missing)).returncode == 2)

    empty = tmp / "empty.jsonl"
    empty.write_text("", encoding="utf-8")
    check("an empty ledger fails", cli("verify", str(empty)).returncode == 2)

    section("verify: reading leaves the file alone")
    before = path.read_bytes()
    cli("verify", str(path))
    check("verifying does not modify the record", path.read_bytes() == before)


# ---------------------------------------------------------------- exit codes

def cli_checks(tmp: Path) -> None:
    section("cli: init")

    target = tmp / "initdir"
    target.mkdir()
    result = cli("init", str(target))
    written = target / cfg.CONFIG_NAME
    check("init writes a config", result.returncode == 0 and written.is_file())
    check("the starter config parses", cfg.load(written).base_url != "")
    check("init refuses to clobber", cli("init", str(target)).returncode == 2)
    check("--force overwrites",
          cli("init", str(target), "--force").returncode == 0)

    section("cli: refusing to run rather than running wrong")

    check("a missing workspace exits failed",
          cli("run", str(tmp / "no-such-dir")).returncode == 2)

    bad = tmp / "badcfg.toml"
    bad.write_text('[models]\nnonsense = "x"\n', encoding="utf-8")
    result = cli("run", str(tmp), "--config", str(bad))
    check("a bad config exits failed rather than running", result.returncode == 2)
    check("the reason reaches stderr", "configuration" in result.stderr)

    result = cli("run", str(tmp), "--config", str(tmp / "absent.toml"))
    check("a named config that is absent exits failed", result.returncode == 2)

    # No key and a metered provider must stop before any work happens, rather
    # than starting a run that cannot make its first call.
    nokey = tmp / "nokey.toml"
    nokey.write_text('[provider]\napi_key_env = "CRUCIBLE_TEST_ABSENT_KEY"\n',
                     encoding="utf-8")
    result = cli("run", str(tmp), "--config", str(nokey))
    check("a metered run with no key exits failed", result.returncode == 2)
    check("it never reaches the model",
          "CRUCIBLE_TEST_ABSENT_KEY" in (result.stderr + result.stdout))

    section("cli: shape")
    check("the help lists every subcommand",
          all(word in cli("--help").stdout
              for word in ("run", "init", "models", "verify")))
    check("no subcommand is an error", cli().returncode != 0)


# --------------------------------------------------------------- regressions

class HalfDeadProvider:
    """Answers the planner, then fails every agent after it.

    The exact shape of a real misconfiguration: one model name in the worker
    seat that the endpoint rejects, or a proxy fronting only some models. The
    planner call succeeds, so lanes exist and nothing sets `halted`, and every
    hunter and verifier dies. A review that read no code then looks identical
    to a clean one.
    """

    name = "half-dead"

    def __init__(self):
        self.calls = 0

    def complete(self, system, user, tier, budget, *, max_output=2000,
                 reasoning="low"):
        from crucible.providers import Completion

        self.calls += 1
        if "You divide a code review" in system:
            text = json.dumps({"lanes": [
                {"name": "money", "brief": "money handling", "files": ["a.py"]}]})
            return Completion(text, "stub", 10, 10, 0.0, 0.0)
        raise RuntimeError("openai 400: the model 'typo-5' does not exist")


def regression_checks(tmp: Path) -> None:
    section("regression: a run that never happened must not pass")

    from crucible.ledger import Ledger as _L
    from crucible.orchestrator import Orchestrator

    work = tmp / "halfdead"
    (work / "src").mkdir(parents=True)
    (work / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")

    provider = HalfDeadProvider()
    report = Orchestrator(
        provider, work, review_policy(work), _L(tmp / "halfdead.jsonl"),
        Budget(ceiling_usd=1.0), max_workers=1,
    ).run("review")

    check("the planner succeeded so nothing is marked halted",
          report.halted == "" and report.lanes)
    check("the review produced no findings", report.raised == 0)
    check("but the failed agents are counted", report.agent_failures > 0)
    check("and the record names them",
          any(e["event"] == "agent_failed"
              for e in _L(tmp / "halfdead.jsonl").entries()))

    from crucible.cli import _credibility

    reason = _credibility(report)
    check("the run is judged not credible", bool(reason))
    check("the reason names the failed agents", "did not finish" in reason)

    healthy = type(report)(
        run_id="r", task="t", lanes=["one"], raised=0, survived=0, findings=[],
        spend_usd=0.0, calls=1, tool_calls=1, refusals=0, ledger_head="h",
        seconds=1.0, halted="", agent_failures=0, agents_run=3,
        probe={"held": True},
    )
    check("a genuinely clean run is still credible", _credibility(healthy) == "")
    breached = type(report)(
        run_id="r", task="t", lanes=["one"], raised=0, survived=0, findings=[],
        spend_usd=0.0, calls=1, tool_calls=1, refusals=0, ledger_head="h",
        seconds=1.0, halted="", agent_failures=0, agents_run=3,
        probe={"held": False, "note": "read a file outside the workspace"},
    )
    check("a run whose boundary failed is not credible",
          bool(_credibility(breached)))

    section("regression: a malformed config fails as configuration, not as a finding")

    for body, label in (
        ('[run]\nceiling_usd = "1.00 AUD"\n', "a non-numeric ceiling"),
        ('[run]\nmax_workers = "six"\n', "a non-numeric worker count"),
        ('[run]\nceiling_usd = 0\n', "a ceiling of zero"),
        ('run = 3\n', "a section written as a scalar"),
    ):
        path = tmp / f"reg-{abs(hash(label))}.toml"
        path.write_text(body, encoding="utf-8")
        try:
            cfg.load(path)
            check(f"{label} is refused", False, "no error raised")
        except cfg.ConfigError:
            check(f"{label} is refused", True)
        result = cli("run", str(tmp), "--config", str(path))
        check(f"{label} exits 2 rather than 1", result.returncode == 2,
              f"got {result.returncode}")

    utf16 = tmp / "utf16.toml"
    utf16.write_bytes('[run]\nceiling_usd = 1.0\n'.encode("utf-16"))
    try:
        cfg.load(utf16)
        check("a UTF-16 config is refused", False, "no error raised")
    except cfg.ConfigError as exc:
        check("a UTF-16 config is refused", True)
        check("the refusal says to save it as UTF-8", "UTF-8" in str(exc))

    section("regression: policy round trip keeps the restrictive value")

    strict = Policy.from_dict({
        "name": "p",
        "tools": {"write_scratch": {"path_scopes": [str(tmp)], "commands": [],
                                    "url_hosts": [], "max_bytes": 0}},
    })
    check("max_bytes of zero survives rather than becoming a megabyte",
          strict.rules["write_scratch"].max_bytes == 0)

    stringy = Policy.from_dict({
        "name": "p",
        "tools": {"read_file": {"path_scopes": str(tmp), "commands": [],
                                "url_hosts": [], "max_bytes": 100}},
    })
    check("a path_scopes string becomes one scope, not one per character",
          len(stringy.rules["read_file"].path_scopes) == 1)

    section("regression: the endpoint host is matched, not searched for")

    def field_for(url: str) -> str:
        return OpenAIProvider("k", base_url=url).\
            _token_field

    check("azure openai takes max_completion_tokens",
          field_for("https://mine.openai.azure.com/v1") == "max_completion_tokens")
    check("a host merely containing the name does not",
          field_for("https://api.openai.com.evil.test/v1") == "max_tokens")
    check("the name appearing in a query string does not",
          field_for("http://box.local/v1?upstream=api.openai.com") == "max_tokens")
    check("the real host still does",
          field_for("https://api.openai.com/v1") == "max_completion_tokens")

    section("regression: verify reads the run's outcome, not only its chain")

    work2 = tmp / "vhalt"
    work2.mkdir()
    policy = review_policy(work2)
    halted_path = tmp / "halted.jsonl"
    led = Ledger(halted_path)
    led.append("run_started", run_id="h1", task="t", workspace=str(work2),
               policy=policy.as_dict(), budget_ceiling_usd=1.0)
    led.append("run_finished", run_id="h1", raised=0, survived=0, spend_usd=0.0,
               halted="run has committed $1.00 of its $1.00 ceiling",
               agent_failures=4, agents_run=6, probe_held=True)
    result = cli("verify", str(halted_path))
    check("a halted run does not verify as clean", result.returncode == 2,
          result.stdout)
    check("the output says the run was not clean",
          "did not come back clean" in result.stdout)

    survived_path = tmp / "survived.jsonl"
    led = Ledger(survived_path)
    led.append("run_started", run_id="s1", task="t", workspace=str(work2),
               policy=policy.as_dict(), budget_ceiling_usd=1.0)
    led.append("run_finished", run_id="s1", raised=3, survived=1, spend_usd=0.1,
               halted="", agent_failures=0, agents_run=6, probe_held=True)
    result = cli("verify", str(survived_path))
    check("a record whose findings survived exits 1, not 0",
          result.returncode == 1, f"got {result.returncode}")

    section("regression: truncated arguments are skipped, not replayed literally")

    trunc = tmp / "truncated-args.jsonl"
    led = Ledger(trunc)
    led.append("run_started", run_id="t1", task="t", workspace=str(work2),
               policy=policy.as_dict(), budget_ceiling_usd=1.0)
    # A truncated payload leaves the path decidable, so the call is replayed
    # and only its size limit goes unchecked.
    led.append("tool_call", agent="h", tool="write_scratch",
               args={"path": str(work2 / ".crucible-scratch" / "n.txt"),
                     "content": "<812 chars>"})
    led.append("run_finished", run_id="t1", raised=0, survived=0, spend_usd=0.0,
               halted="", agent_failures=0, agents_run=1, probe_held=True)
    result = cli("verify", str(trunc), "--workspace", str(work2))
    check("a truncated payload still verifies",
          result.returncode == 0, result.stdout)
    check("and says the size limit went unchecked",
          "size limits were not rechecked" in result.stdout)

    # A truncated PATH cannot be re-decided at all, and skipping it silently is
    # how a forger hides a call from the replay. Fails closed instead.
    hidden = tmp / "hidden-call.jsonl"
    led = Ledger(hidden)
    led.append("run_started", run_id="t2", task="t", workspace=str(work2),
               policy=policy.as_dict(), budget_ceiling_usd=1.0)
    led.append("tool_call", agent="h", tool="read_file",
               args={"path": "<812 chars>"})
    led.append("run_finished", run_id="t2", raised=0, survived=0, spend_usd=0.0,
               halted="", agent_failures=0, agents_run=1, probe_held=True)
    result = cli("verify", str(hidden), "--workspace", str(work2))
    check("a truncated path makes the call unverifiable and fails the record",
          result.returncode == 2, result.stdout)
    check("and says which argument could not be replayed",
          "COULD NOT BE REPLAYED" in result.stdout)

    garbage = tmp / "garbage.jsonl"
    garbage.write_text("{not json at all\n", encoding="utf-8")
    result = cli("verify", str(garbage))
    check("a malformed ledger exits 2 rather than crashing",
          result.returncode == 2, result.stderr[:120])
    check("nothing escapes as a traceback",
          "Traceback" not in result.stderr, result.stderr[:120])

    section("regression: verify can be told not to trust the file's own policy")

    honest = tmp / "indep.jsonl"
    led = Ledger(honest)
    led.append("run_started", run_id="i1", task="t", workspace=str(work2),
               policy=policy.as_dict(), budget_ceiling_usd=1.0)
    led.append("tool_denied", agent="p", tool="read_file",
               args={"path": str(tmp / "outside.txt")}, reason="outside")
    led.append("run_finished", run_id="i1", raised=0, survived=0, spend_usd=0.0,
               halted="", agent_failures=0, agents_run=1, probe_held=True)
    result = cli("verify", str(honest))
    check("the default replay warns that the policy is self-reported",
          "SELF-REPORTED" in result.stdout)
    result = cli("verify", str(honest), "--workspace", str(work2))
    check("an independent policy verifies an honest record",
          result.returncode == 0, result.stdout)
    check("and says the policy was rebuilt independently",
          "independent" in result.stdout)

    # The forgery the self-reported replay cannot catch: rewrite the recorded
    # policy so that everything the file claims was allowed really is allowed.
    forged = tmp / "permissive.jsonl"
    entries = [json.loads(l) for l in honest.read_text(encoding="utf-8").splitlines()]
    entries[1]["event"] = "tool_call"
    entries[0]["payload"]["policy"] = {
        "name": "review",
        "tools": {"read_file": {"path_scopes": [str(tmp.parent)], "commands": [],
                                "url_hosts": [], "max_bytes": 1_000_000}},
    }
    _write_chain(forged, _rechain(entries))
    result = cli("verify", str(forged))
    check("a forger who rewrites the policy passes the self-reported replay",
          result.returncode == 0, result.stdout)
    result = cli("verify", str(forged), "--workspace", str(work2))
    check("but fails once the policy comes from the workspace",
          result.returncode == 2 and "DO NOT REPRODUCE" in result.stdout,
          result.stdout)


class SelectiveProvider:
    """Answers everyone except the seats named in `fail`.

    Lets a test kill exactly one role and watch what the gate does about it,
    which is the difference between the two ways the credibility counter was
    wrong: it failed runs on the one agent whose answer is discarded, and it
    passed runs that had lost a whole lane.
    """

    name = "selective"
    MARKERS = {
        "planner": "You divide a code review",
        "hunter": "You are hunting for real defects",
        "verifier": "You are trying to REFUTE",
        "prober": "Your lane is the boundary itself",
    }

    def __init__(self, fail=(), lane_files=("a.py",)):
        self.fail = set(fail)
        self.lane_files = lane_files

    def _role(self, system):
        for role, marker in self.MARKERS.items():
            if marker in system:
                return role
        return "unknown"

    def complete(self, system, user, tier, budget, *, max_output=2000,
                 reasoning="low"):
        from crucible.providers import Completion

        role = self._role(system)
        if role in self.fail:
            raise RuntimeError("openai 400: no model for the " + role + " seat")
        if role == "planner":
            text = json.dumps({"lanes": [{"name": "money", "brief": "money",
                                          "files": self.lane_files}]})
        elif role == "hunter":
            text = json.dumps({"done": True, "findings": []})
        elif role == "verifier":
            text = json.dumps({"done": True, "refuted": True,
                               "confidence": "high", "concrete_failure": "",
                               "reasoning": "no"})
        else:
            text = json.dumps({"done": True, "got_through": [], "notes": "held"})
        return Completion(text, "stub", 10, 10, 0.0, 0.0)


def _run_with(provider, work, ledger):
    from crucible.ledger import Ledger as _L
    from crucible.orchestrator import Orchestrator

    return Orchestrator(provider, work, review_policy(work), _L(ledger),
                        Budget(ceiling_usd=1.0), max_workers=1).run("review")


def regression_checks_two(tmp):
    from crucible.cli import _credibility

    section("regression: the gate counts the agents whose answers are used")

    work = tmp / "roles"
    (work / "src").mkdir(parents=True)
    (work / "src" / "a.py").write_text("x = 1\n", encoding="utf-8")

    report = _run_with(SelectiveProvider(fail={"prober"}), work,
                       tmp / "prober-died.jsonl")
    check("a failed boundary agent is recorded", report.probe_agent_failed)
    check("but is kept out of the blocking count", report.agent_failures == 0)
    check("the fixed sweep still proves the boundary",
          report.probe.get("held") is True)
    check("so a complete review is still credible", _credibility(report) == "")

    # A lane whose files are null raises inside one_lane before the agent loop
    # is entered, which _failed() never sees.
    report = _run_with(SelectiveProvider(lane_files=None), work,
                       tmp / "lane-died.jsonl")
    check("a worker thread that dies is counted", report.agent_failures >= 1)
    check("and counts as an agent that did nothing", report.agents_silent >= 1)
    check("so the run is judged not credible", bool(_credibility(report)))
    check("the record carries the worker failure",
          any(e["event"] == "worker_failed"
              for e in Ledger(tmp / "lane-died.jsonl").entries()))

    section("regression: unmetered cannot be declared over a billed endpoint")

    for host in ("https://api.openai.com/v1",
                 "https://mine.openai.azure.com/v1",
                 "https://api.anthropic.com/v1"):
        try:
            OpenAIProvider("k", base_url=host, metered=False)
            check("metered=false is refused for " + host, False, "allowed")
        except ValueError as exc:
            check("metered=false is refused for " + host,
                  "spend ceiling" in str(exc))
    try:
        OpenAIProvider("", base_url="http://127.0.0.1:8080/v1", metered=False)
        check("a local endpoint may still be unmetered", True)
    except ValueError:
        check("a local endpoint may still be unmetered", False)

    section("regression: verify survives a malformed record")

    stray = tmp / "stray.jsonl"
    stray.write_text("null\n[1,2,3]\n", encoding="utf-8")
    result = cli("verify", str(stray))
    check("a line that is valid JSON but not an entry exits 2",
          result.returncode == 2, "got %s" % result.returncode)
    check("without a traceback", "Traceback" not in result.stderr)

    numeric = Policy.from_dict({
        "name": "p",
        "tools": {"read_file": {"path_scopes": [42], "commands": [],
                                "url_hosts": [], "max_bytes": 10}},
    })
    check("a path scope recorded as a number does not crash from_dict",
          len(numeric.rules["read_file"].path_scopes) == 1)

    section("regression: --workspace has to be the workspace the run used")

    work2 = tmp / "declared"
    work2.mkdir()
    other = tmp / "elsewhere"
    other.mkdir()
    path = tmp / "declared.jsonl"
    led = Ledger(path)
    led.append("run_started", run_id="d1", task="t", workspace=str(work2),
               policy=review_policy(work2).as_dict(), budget_ceiling_usd=1.0)
    led.append("run_finished", run_id="d1", raised=0, survived=0, spend_usd=0.0,
               halted="", agent_failures=0, agents_run=1, probe_held=True)
    result = cli("verify", str(path), "--workspace", str(other))
    check("a different workspace is refused rather than silently compared",
          result.returncode == 2)
    check("and the mismatch is named", "recorded its workspace" in result.stderr)
    check("the declared workspace still verifies",
          cli("verify", str(path), "--workspace", str(work2)).returncode == 0)

    section("regression: verify agrees with run on what blocks")

    breach = tmp / "breach.jsonl"
    led = Ledger(breach)
    led.append("run_started", run_id="b1", task="t", workspace=str(work2),
               policy=review_policy(work2).as_dict(), budget_ceiling_usd=1.0)
    led.append("run_finished", run_id="b1", raised=2, survived=1, spend_usd=0.1,
               halted="", agent_failures=0, agents_run=6, probe_held=False)
    check("a boundary breach exits 2 even when a finding survived",
          cli("verify", str(breach)).returncode == 2)


    section("regression: the record keeps what a decision turns on")

    from crucible.tools import DECISION_ARG_CHARS, MAX_ARG_CHARS, Toolbox

    long_path = "C:" + chr(92) + ("verylongdirectory" + chr(92)) * 40 + "f.py"
    long_cmd = "pytest " + " ".join("-k test_%d" % i for i in range(80))
    long_body = "x" * 5000
    check("the fixture really is longer than the payload limit",
          len(long_path) > MAX_ARG_CHARS and len(long_cmd) > MAX_ARG_CHARS)

    recorded = Toolbox._safe_args({"path": long_path, "command": long_cmd,
                                   "content": long_body})
    check("a long path is kept whole so the call stays replayable",
          recorded["path"] == long_path)
    check("a long command is kept whole too",
          recorded["command"] == long_cmd)
    check("a long payload is still reduced to its length",
          recorded["content"] == "<5000 chars>")

    huge = Toolbox._safe_args({"path": "y" * (DECISION_ARG_CHARS + 1)})
    check("a path past its own ceiling is a payload in disguise and is cut",
          huge["path"].endswith("chars>"))

    section("regression: the completeness gate turns off out loud")

    off = tmp / "off.toml"
    off.write_text("[gate]\nrequire_complete = false\n", encoding="utf-8")
    check("require_complete is read", cfg.load(off).require_complete is False)
    check("and defaults to on", cfg.Config().require_complete is True)


def offline_run_checks(tmp: Path) -> None:
    """The exit-code contract, composed: a real `crucible run` as a subprocess,
    on the stand-in provider, from clone to exit code. This is the keyless
    demo the README offers, exercised the way a stranger runs it."""
    section("run: offline end to end, exit codes composed")
    env = dict(os.environ, CRUCIBLE_OFFLINE="1", CRUCIBLE_OFFLINE_PACE="0")
    ledgers = tmp / "offline-runs"
    out = subprocess.run(
        [sys.executable, "-m", "crucible.cli", "run", "demo_target",
         "--ledger-dir", str(ledgers), "--quiet"],
        capture_output=True, text=True, cwd=str(ROOT), env=env)
    check("an offline run on the demo target exits 1: findings survived",
          out.returncode == 1, out.stdout[-300:] + out.stderr[-300:])
    written = list(ledgers.glob("*.jsonl"))
    check("and it left exactly one ledger, named after the run",
          len(written) == 1 and not written[0].name.startswith(".pending"),
          str(written))

    verify = subprocess.run(
        [sys.executable, "-m", "crucible.cli", "verify", str(written[0]),
         "--workspace", str(ROOT / "demo_target")],
        capture_output=True, text=True, cwd=str(ROOT))
    check("the ledger it wrote verifies against the real workspace",
          verify.returncode == 1 and "intact" in verify.stdout,
          verify.stdout[-300:])

    gate = tmp / "gate" / cfg.CONFIG_NAME
    gate.parent.mkdir(parents=True)
    gate.write_text('[gate]\nfail_on = "never"\n', encoding="utf-8")
    out = subprocess.run(
        [sys.executable, "-m", "crucible.cli", "run", "demo_target",
         "--ledger-dir", str(ledgers), "--config", str(gate), "--quiet"],
        capture_output=True, text=True, cwd=str(ROOT), env=env)
    check("fail_on never turns the same survivors into exit 0",
          out.returncode == 0, out.stdout[-300:] + out.stderr[-300:])


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        config_checks(tmp)
        provider_checks()
        policy_roundtrip_checks(tmp)
        verify_checks(tmp)
        cli_checks(tmp)
        regression_checks(tmp)
        regression_checks_two(tmp)
        offline_run_checks(tmp)
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
