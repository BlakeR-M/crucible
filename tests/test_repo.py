"""Checks for reviewing a repository by URL: parsing, cloning, the server route.

Plain script, no test framework, so it runs anywhere Python does:
    python tests/test_repo.py

No network. The clone checks build a bare repository on disk and clone it by
path, which the module allows only when told to (allow_local=True); the CLI and
the server never say so, and a check here confirms that a path is refused
without it. The server checks stand that local repository in for a host and
run the whole route offline: admission, the clone events on the stream, the
policy scoped to the clone, the source in the ledger's first entry, and the
workspace gone afterwards.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from crucible import repo  # noqa: E402
from crucible.ledger import Ledger  # noqa: E402

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


def refuses(fn, kind=repo.RepoError) -> str:
    """The message of the RepoError fn raises, or '' if it did not raise."""
    try:
        fn()
    except kind as exc:
        return str(exc) or "(raised)"
    return ""


# ------------------------------------------------------------------- parse

def parse_checks() -> None:
    section("parse: every accepted shape")
    p = repo.parse_repo_ref
    check("https, bare", p("https://github.com/org/repo") == ("https://github.com/org/repo", None))
    check("https, .git", p("https://github.com/org/repo.git") == ("https://github.com/org/repo.git", None))
    check("https, trailing slash dropped",
          p("https://github.com/org/repo/") == ("https://github.com/org/repo", None))
    check("gitlab, nested groups",
          p("https://gitlab.com/group/sub/repo") == ("https://gitlab.com/group/sub/repo", None))
    check("scp form becomes https",
          p("git@github.com:org/repo.git") == ("https://github.com/org/repo.git", None))
    check("ssh scheme with the git user becomes https",
          p("ssh://git@gitlab.com/g/r.git") == ("https://gitlab.com/g/r.git", None))
    check("host is lowercased, path is not",
          p("https://GitHub.com/Org/Repo") == ("https://github.com/Org/Repo", None))

    section("parse: the @ref suffix")
    check("@branch", p("https://github.com/o/r@main") == ("https://github.com/o/r", "main"))
    check("@branch with a slash",
          p("https://github.com/o/r@release/1.2") == ("https://github.com/o/r", "release/1.2"))
    check("@tag on .git", p("https://github.com/o/r.git@v1.2.3") == ("https://github.com/o/r.git", "v1.2.3"))
    sha = "a" * 40
    check("@sha", p(f"https://github.com/o/r@{sha}") == ("https://github.com/o/r", sha))
    check("@sha on scp form", p(f"git@github.com:o/r.git@{sha}") == ("https://github.com/o/r.git", sha))

    section("parse: refusals")
    check("a relative path is not a URL", not repo.is_git_url("./here"))
    check("a Windows path is not a URL", not repo.is_git_url("C:/projects/x"))
    check("scp form is a URL", repo.is_git_url("git@github.com:o/r"))
    check("empty is refused", "URL" in refuses(lambda: p("")))
    check("plain words are refused", "does not look like" in refuses(lambda: p("not a url")))
    check("a host with no repository is refused",
          "names a host and no repository" in refuses(lambda: p("https://github.com")))
    check("a username on https is a credential and refused",
          "public repositories only" in refuses(lambda: p("https://tok@github.com/o/r")))
    check("a password is refused",
          "public repositories only" in refuses(lambda: p("https://u:p@github.com/o/r")))
    check("an option-shaped ref is refused",
          "not a branch, tag or commit" in refuses(lambda: p("https://github.com/o/r@--upload-pack=x")))
    check("an empty ref is refused", "@ on the end" in refuses(lambda: p("https://github.com/o/r@")))
    check("a ref with .. is refused",
          "not a branch" in refuses(lambda: p("https://github.com/o/r@a..b")))
    check("a file: URL is refused", "does not look like" in refuses(lambda: p("file:///tmp/x")))

    section("host allowlist")
    ok = refuses(lambda: repo.check_host("https://github.com/o/r", repo.DEFAULT_HOSTS))
    check("github.com is allowed by default", ok == "")
    check("gitlab.com is allowed by default",
          refuses(lambda: repo.check_host("https://gitlab.com/o/r", repo.DEFAULT_HOSTS)) == "")
    msg = refuses(lambda: repo.check_host("https://evil.example/o/r", repo.DEFAULT_HOSTS),
                  repo.RepoHostNotAllowed)
    check("another host is refused with the list", "github.com, gitlab.com" in msg and "evil.example" in msg)
    check("a subdomain of an allowed host passes",
          refuses(lambda: repo.check_host("https://x.gitlab.com/o/r", ["gitlab.com"])) == "")
    check("a lookalike suffix does not",
          refuses(lambda: repo.check_host("https://notgithub.com/o/r", ["github.com"])) != "")
    check("None means no allowlist", refuses(lambda: repo.check_host("https://any/o/r", None)) == "")


# ------------------------------------------------------------------- clone

def make_bare(tmp: Path) -> tuple[Path, str]:
    """A bare repository with two files on main. Returns (path, head sha)."""
    bare = tmp / "origin.git"
    subprocess.run(["git", "init", "--bare", "-q", "-b", "main", str(bare)], check=True)
    src = tmp / "src"
    subprocess.run(["git", "clone", "-q", str(bare), str(src)], check=True,
                   capture_output=True)
    (src / "app.py").write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
    (src / "lib").mkdir()
    (src / "lib" / "util.py").write_text("X = 1\n", encoding="utf-8")
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    subprocess.run(["git", "-C", str(src), "add", "."], check=True)
    subprocess.run(["git", "-C", str(src), "commit", "-q", "-m", "one"], check=True, env=env)
    subprocess.run(["git", "-C", str(src), "push", "-q", "origin", "HEAD:main"], check=True)
    sha = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True).stdout.strip()
    return bare, sha


def clone_checks(tmp: Path) -> None:
    section("clone: a local bare repository, allowed only when said so")
    bare, sha = make_bare(tmp)

    check("a path is refused without allow_local",
          "not a repository URL" in refuses(lambda: repo.clone(str(bare), None, tmp / "no")))
    check("and left nothing behind", not (tmp / "no").exists())

    result = repo.clone(str(bare), None, tmp / "ws", allow_local=True)
    check("the clone lands in dest", result.path == tmp / "ws" and (tmp / "ws" / "app.py").is_file())
    check("the commit sha is recorded", result.commit_sha == sha)
    check("files and bytes are measured", result.files == 2 and result.bytes > 0)
    check(".git is stripped", not (tmp / "ws" / ".git").exists())
    top = subprocess.run(["git", "-C", str(tmp / "ws"), "rev-parse", "--show-toplevel"],
                         capture_output=True, text=True)
    check("history is out of reach: the checkout is not a git work tree",
          top.returncode != 0 or Path(top.stdout.strip()).resolve() != (tmp / "ws").resolve())
    header = result.as_header()
    check("the header carries the source fields and tests_enabled, off by default",
          set(header) == {"repo_url", "repo_ref", "commit_sha", "tests_enabled"}
          and header["commit_sha"] == sha and header["tests_enabled"] is False)
    check("tests_enabled can be recorded on", result.as_header(tests_enabled=True)["tests_enabled"] is True)

    section("clone: refs")
    by_branch = repo.clone(str(bare), "main", tmp / "ws-branch", allow_local=True)
    check("by branch", by_branch.commit_sha == sha and by_branch.ref == "main")
    by_sha = repo.clone(str(bare), sha, tmp / "ws-sha", allow_local=True)
    check("by 40-hex sha", by_sha.commit_sha == sha and by_sha.ref == sha)
    msg = refuses(lambda: repo.clone(str(bare), "nope", tmp / "ws-nope", allow_local=True),
                  repo.RepoCloneFailed)
    check("a missing branch fails plainly", "missing from the repository" in msg, msg)
    check("and the workspace is wiped", not (tmp / "ws-nope").exists())
    check("an option-shaped ref never reaches git",
          "not a branch" in refuses(lambda: repo.clone(str(bare), "-x", tmp / "ws-opt", allow_local=True)))

    section("clone: caps")
    msg = refuses(lambda: repo.clone(str(bare), None, tmp / "ws-files", allow_local=True, max_files=1),
                  repo.RepoTooLarge)
    check("the file cap names the numbers", "2 files against a cap of 1" in msg, msg)
    check("and wipes the workspace", not (tmp / "ws-files").exists())
    msg = refuses(lambda: repo.clone(str(bare), None, tmp / "ws-bytes", allow_local=True, max_bytes=3),
                  repo.RepoTooLarge)
    check("the size cap names the numbers", "against a cap of" in msg and "MB" in msg, msg)
    check("and wipes the workspace", not (tmp / "ws-bytes").exists())

    section("clone: failure wipes")
    msg = refuses(lambda: repo.clone(str(tmp / "absent.git"), None, tmp / "ws-absent", allow_local=True),
                  repo.RepoCloneFailed)
    check("a missing repository fails as a clone failure", msg != "", msg)
    check("and leaves no directory", not (tmp / "ws-absent").exists())
    (tmp / "busy").mkdir()
    (tmp / "busy" / "x").write_text("x")
    check("a non-empty dest is refused",
          "already has files" in refuses(lambda: repo.clone(str(bare), None, tmp / "busy", allow_local=True)))

    section("clone: url canonicalisation reaches the record")
    # A URL form goes through parse and the host check even though nothing is
    # fetched: an allowlist that excludes the host stops it before git runs.
    msg = refuses(lambda: repo.clone("git@evil.example:o/r.git", None, tmp / "ws-host",
                                     allowed_hosts=["github.com"]),
                  repo.RepoHostNotAllowed)
    check("scp form is canonicalised before the host check", "evil.example" in msg)
    check("nothing was created", not (tmp / "ws-host").exists())

    section("remove_tree handles read-only files")
    ro = tmp / "ro"
    ro.mkdir()
    f = ro / "obj"
    f.write_text("x")
    os.chmod(f, 0o444)
    repo.remove_tree(ro)
    check("a read-only tree is removed", not ro.exists())
    repo.remove_tree(ro)
    check("removing it again is fine", True)


# ---------------------------------------------------------- policy + env

def policy_and_env_checks(tmp: Path) -> None:
    from crucible.ledger import Ledger as _Ledger
    from crucible.policy import review_policy
    from crucible.tools import Toolbox, subprocess_env

    section("policy: a URL run withholds run_tests unless the operator says so")
    work = tmp / "work"
    work.mkdir(parents=True)
    full = review_policy(work)
    ro = review_policy(work, run_tests=False)
    check("the default policy still has run_tests", "run_tests" in full.rules)
    check("the read-only variant does not", "run_tests" not in ro.rules)
    check("and says so in its name", ro.name == "review-read-only")
    check("every other tool is unchanged", set(ro.rules) == set(full.rules) - {"run_tests"})
    decision = ro.check("run_tests", {"path": str(work), "command": "pytest"})
    check("run_tests is refused under it", not decision.allowed)
    os.environ.pop(repo.URL_RUN_TESTS_ENV, None)
    check("URL tests are off by default", repo.url_tests_enabled() is False)
    os.environ[repo.URL_RUN_TESTS_ENV] = "1"
    check("and on with the variable", repo.url_tests_enabled() is True)
    os.environ.pop(repo.URL_RUN_TESTS_ENV, None)

    section("subprocess env: a tool's child sees no key, no CRUCIBLE_*, nothing secret-shaped")
    sample = {"PATH": "p", "HOME": "h", "LANG": "C", "SYSTEMROOT": "C:\Windows", "TEMP": "t",
              "OPENAI_API_KEY": "sk-x", "CRUCIBLE_USER": "u", "CRUCIBLE_PASS": "p",
              "MY_TOKEN": "t", "AWS_SECRET": "s", "DB_PASSWORD": "p", "GITHUB_KEY": "k",
              "SOMETHING_ELSE": "x", "openai_api_key": "lower"}
    env = subprocess_env(sample)
    check("the allowlist survives", {"PATH", "HOME", "LANG", "SYSTEMROOT", "TEMP"} <= set(env))
    check("nothing else does",
          not ({"OPENAI_API_KEY", "openai_api_key", "CRUCIBLE_USER", "CRUCIBLE_PASS", "MY_TOKEN",
                "AWS_SECRET", "DB_PASSWORD", "GITHUB_KEY", "SOMETHING_ELSE"} & set(env)), str(env))

    # The real thing: a run_tests child, with the key set in this process.
    os.environ["OPENAI_API_KEY"] = "sk-test-must-not-leak"
    os.environ["CRUCIBLE_SECRET"] = "also-not"
    (work / "probe_env.py").write_text(
        "import os, json; print(json.dumps(sorted(os.environ)))" + chr(10), encoding="utf-8")
    box = Toolbox(work, review_policy(work), _Ledger(tmp / "env.jsonl"))
    result = box.invoke("run_tests", {"path": str(work),
                                      "command": f'python "{work / "probe_env.py"}"'})
    out = result.content
    check("the child ran", "exit=0" in out, out[:200])
    check("and cannot see OPENAI_API_KEY", "OPENAI_API_KEY" not in out)
    check("nor CRUCIBLE_SECRET", "CRUCIBLE_SECRET" not in out)
    check("but does see PATH", '"PATH"' in out or '"Path"' in out, out[:300])
    del os.environ["OPENAI_API_KEY"]
    del os.environ["CRUCIBLE_SECRET"]


# ------------------------------------------------------------------ server

def server_checks(tmp: Path) -> None:
    os.environ["CRUCIBLE_OFFLINE"] = "1"
    os.environ.setdefault("CRUCIBLE_USER", "t")
    os.environ.setdefault("CRUCIBLE_PASS", "t")
    from crucible import server

    section("server: admission refuses without cloning")
    before = server.REGISTRY.listing()
    status, body = server.admit_run({"repo_url": "https://evil.example/o/r"})
    check("a disallowed host is a 400", status == 400)
    check("with the plain sentence", "github.com" in body["error"] and "evil.example" in body["error"])
    check("and no run was created", server.REGISTRY.listing() == before)
    status, body = server.admit_run({"repo_url": "https://github.com"})
    check("a host without a repository is a 400", status == 400 and "form https://" in body["error"])
    status, body = server.admit_run({"repo_url": "https://github.com/o/r@--x"})
    check("an option-shaped ref is a 400", status == 400)
    status, body = server.admit_run({"repo_url": "https://tok@github.com/o/r"})
    check("a credential in the URL is a 400", status == 400 and "public" in body["error"])
    status, body = server.admit_run({"repo_url": "https://github.com/" + "a" * 600})
    check("an absurdly long address is a 400", status == 400)
    status, body = server.admit_run({"repo_url": "https://github.com/o/r@main", "ref": "dev"})
    check("two different refs are a 400", status == 400 and "pick one" in body["error"])
    check("still no run created", server.REGISTRY.listing() == before)

    section("server: a run against a repository, offline, local origin standing in")
    bare, sha = make_bare(tmp / "srv")
    server.RUNS = tmp / "runs"
    real_clone = server.clone
    seen: dict = {}

    def local_clone(url, ref, dest, **kw):
        # The route asked for the canonical URL; the local origin is fetched
        # in its place, with every other guardrail argument as sent.
        seen.update(kw, url=url, ref=ref)
        return real_clone(str(bare), ref, dest, allow_local=True,
                          **{k: v for k, v in kw.items() if k != "allowed_hosts"})

    server.clone = local_clone
    try:
        status, body = server.admit_run({"repo_url": "https://github.com/octocat/example@main",
                                         "task": "full"})
        check("admitted", status == 200 and body.get("run_id"), str(body))
        run_id = body["run_id"]
        deadline = time.time() + 120
        state = server.REGISTRY.get(run_id)
        while not state["finished"] and time.time() < deadline:
            time.sleep(0.2)
        check("the run finished", state["finished"])
        kinds = [e["kind"] for e in state["events"]]
        check("clone_started came first", kinds[:1] == ["clone_started"], str(kinds[:3]))
        finished = next((e for e in state["events"] if e["kind"] == "clone_finished"), None)
        check("clone_finished carries the sha, files and bytes",
              finished is not None and finished["commit_sha"] == sha
              and finished["files"] == 2 and finished["bytes"] > 0, str(finished))
        check("the guardrail arguments reached the clone",
              seen.get("allowed_hosts") == server.REPO_HOSTS
              and seen.get("max_bytes") == server.REPO_MAX_BYTES
              and seen.get("max_files") == server.REPO_MAX_FILES
              and seen.get("url") == "https://github.com/octocat/example"
              and seen.get("ref") == "main", str(seen))
        started = next((e for e in state["events"] if e["kind"] == "run_started"), None)
        check("run_started names the source",
              started is not None and started.get("repo_url") == "https://github.com/octocat/example"
              and started.get("commit_sha") == sha)
        check("run_started records tests_enabled false for a URL run",
              started is not None and started.get("tests_enabled") is False)
        check("and the policy it ran under has no run_tests",
              started is not None and "run_tests" not in started["policy"]["tools"])
        # The workspace the policy was scoped to is the clone, and it is gone.
        scope = started["policy"]["tools"]["read_file"]["path_scopes"][0] if started else ""
        check("the policy was scoped to the temporary checkout",
              "crucible-repo-" in str(scope) and str(scope).endswith("checkout"), str(scope))
        check("the checkout was removed after the run", not Path(scope).exists())
        check("the source shows in the listing",
              any(r["run_id"] == run_id and r["source"].get("commit_sha") == sha
                  for r in server.REGISTRY.listing()))
        ledger = Ledger(server.RUNS / f"{run_id}.jsonl")
        first = ledger.entries()[0]
        check("the ledger's first entry carries repo_url, repo_ref, commit_sha and tests_enabled",
              first["event"] == "run_started" and first["payload"].get("repo_url")
              == "https://github.com/octocat/example" and first["payload"].get("repo_ref") == "main"
              and first["payload"].get("commit_sha") == sha
              and first["payload"].get("tests_enabled") is False)
        check("and the chain verifies", ledger.verify() is None)

        section("server: a clone that fails ends the run cleanly")
        server.clone = lambda url, ref, dest, **kw: real_clone(
            str(tmp / "srv" / "absent.git"), ref, dest, allow_local=True, max_files=kw["max_files"])
        status, body = server.admit_run({"repo_url": "https://github.com/octocat/absent"})
        run_id = body["run_id"]
        state = server.REGISTRY.get(run_id)
        deadline = time.time() + 60
        while not state["finished"] and time.time() < deadline:
            time.sleep(0.2)
        kinds = [e["kind"] for e in state["events"]]
        check("clone_failed is on the stream", "clone_failed" in kinds, str(kinds))
        check("and run_finished closes it", kinds[-1] == "run_finished")
        check("the run holds no slot", server.REGISTRY.active() == 0)
        stray = [p for p in Path(tempfile.gettempdir()).glob("crucible-repo-*")]
        check("no crucible-repo-* workspace is left in the temp directory",
              not stray, str(stray[:3]))
    finally:
        server.clone = real_clone


def cli_checks(tmp: Path) -> None:
    section("cli: a URL argument is refused before any config is read")
    r = subprocess.run([sys.executable, "-m", "crucible.cli", "run",
                        "https://github.com/o/r@--x", "--ledger-dir", str(tmp / "cli")],
                       capture_output=True, text=True, cwd=str(ROOT))
    check("exit 2", r.returncode == 2, r.stderr[-300:])
    check("with the plain reason", "repository:" in r.stderr and "not a branch" in r.stderr)
    r = subprocess.run([sys.executable, "-m", "crucible.cli", "run",
                        str(tmp / "nowhere-at-all")],
                       capture_output=True, text=True, cwd=str(ROOT))
    check("a missing directory still exits 2 with the old message",
          r.returncode == 2 and "no such directory" in r.stderr)


def main() -> None:
    with tempfile.TemporaryDirectory() as raw:
        tmp = Path(raw)
        parse_checks()
        clone_checks(tmp / "clone")
        policy_and_env_checks(tmp / "pol")
        server_checks(tmp)
        cli_checks(tmp)
    print(f"\n{PASSED} checks passed, {len(FAILED)} failed")
    if FAILED:
        for name in FAILED:
            print(f"  - {name}")
        sys.exit(1)


if __name__ == "__main__":
    main()
