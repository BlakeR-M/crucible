"""A public git repository, fetched into a workspace the review can be scoped to.

The review reads a directory. This module turns a URL into that directory and
nothing more: a shallow clone of one commit, measured against a size cap and a
file cap, with the `.git` directory removed once the commit is recorded, so the
agents read the tree at that commit and never its history.

Public repositories only. No credential is ever passed, and git is told so
(`GIT_TERMINAL_PROMPT=0`, `GIT_ASKPASS=echo`), which turns a private
repository into a fast, plain failure instead of a process waiting on a prompt
nobody will answer.

Standard library only, like the rest of the tool. `git` on the PATH is the one
thing this needs, and it says so when it is missing.
"""

from __future__ import annotations

import os
import re
import shutil
import stat
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_HOSTS = ("github.com", "gitlab.com")

# Whether a repository fetched by URL may have its own tests executed. Off
# unless the operator says so: a stranger's test files running beside this
# process is a decision, and CRUCIBLE_URL_RUN_TESTS=1 is how it is made.
URL_RUN_TESTS_ENV = "CRUCIBLE_URL_RUN_TESTS"


def url_tests_enabled() -> bool:
    return os.environ.get(URL_RUN_TESTS_ENV, "").strip().lower() in (
        "1", "true", "yes", "on")

# A branch, a tag, or a full commit. Underscores, dots, slashes and dashes are
# fine; anything git could read as an option (a leading dash) is refused before
# it reaches the command line.
_REF = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/+-]{0,199}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_SCP = re.compile(r"^(?P<user>[A-Za-z0-9._-]+)@(?P<host>[A-Za-z0-9._-]+):(?P<path>[^:@]+(?:@[^@]*)?)$")


# ---------------------------------------------------------------- errors

class RepoError(Exception):
    """Anything that stops a URL becoming a workspace. The message is written
    to be shown to the person who typed the URL."""


class RepoUrlError(RepoError):
    """The string is not a repository reference this tool takes."""


class RepoHostNotAllowed(RepoError):
    """The host is outside the allowlist."""


class RepoTooLarge(RepoError):
    """The checkout is past the size or file cap. Carries the numbers."""

    def __init__(self, *, files: int, bytes: int, max_files: int, max_bytes: int):
        self.files, self.bytes = files, bytes
        self.max_files, self.max_bytes = max_files, max_bytes
        over = []
        if files > max_files:
            over.append(f"{files:,} files against a cap of {max_files:,}")
        if bytes > max_bytes:
            over.append(f"{bytes / 1_000_000:.1f} MB against a cap of "
                        f"{max_bytes / 1_000_000:.0f} MB")
        super().__init__("that repository is larger than this reviewer takes: "
                         + "; ".join(over) + ". A smaller repository, or a "
                         "subdirectory checked out locally, will go through.")


class RepoCloneFailed(RepoError):
    """git could not produce the checkout: unreachable, private, no such ref,
    or too slow."""


@dataclass(frozen=True)
class CloneResult:
    path: Path
    url: str
    ref: str | None
    commit_sha: str
    files: int
    bytes: int

    def as_header(self, *, tests_enabled: bool = False) -> dict:
        """The fields the run record carries for a fetched workspace."""
        return {"repo_url": self.url, "repo_ref": self.ref,
                "commit_sha": self.commit_sha, "tests_enabled": tests_enabled}


# --------------------------------------------------------------- parsing

def is_git_url(value: str) -> bool:
    """Whether the string names a remote repository rather than a directory."""
    text = (value or "").strip()
    if not text:
        return False
    if _SCP.match(text):
        return True
    scheme = urlsplit(text).scheme.lower()
    return scheme in ("https", "http", "ssh", "git")


def host_of(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def parse_repo_ref(value: str) -> tuple[str, str | None]:
    """Split `URL[@ref]` into a canonical https URL and an optional ref.

    Accepted shapes:
        https://github.com/org/repo
        https://github.com/org/repo.git
        https://gitlab.com/group/sub/repo
        git@github.com:org/repo.git
    each with an optional `@branch`, `@tag` or `@<40-hex-sha>` on the end.

    The scp form is rewritten to https, so one canonical URL reaches the host
    check and the record. Public repositories read the same over either, and
    this tool never carries the key that would make ssh mean something else.
    A URL that carries its own credentials is refused for the same reason.
    """
    text = (value or "").strip()
    if not text:
        raise RepoUrlError("give a repository URL to review")

    scp = _SCP.match(text)
    if scp:
        path = scp.group("path")
        path, ref = _split_ref(path)
        url = f"https://{scp.group('host')}/{path.lstrip('/')}"
        return _canonical(url), _check_ref(ref)

    parts = urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("https", "http", "ssh", "git"):
        raise RepoUrlError(
            f"'{text[:80]}' does not look like a repository URL. It wants the "
            f"form https://github.com/org/repo, with an optional @branch, "
            f"@tag or @commit on the end.")
    # `ssh://git@host/...` carries the conventional ssh user and no secret. A
    # username on https, or a password anywhere, is a credential.
    ssh_user = scheme in ("ssh", "git") and parts.username == "git"
    if parts.password or (parts.username and not ssh_user):
        raise RepoUrlError("this reviewer reads public repositories only, so a "
                           "URL carrying a username or token is refused")
    if not parts.hostname:
        raise RepoUrlError(f"'{text[:80]}' names no host")
    path, ref = _split_ref(parts.path)
    if scheme in ("ssh", "git"):
        scheme = "https"
    url = f"{scheme}://{parts.hostname.lower()}"
    if parts.port:
        url += f":{parts.port}"
    url += "/" + path.lstrip("/")
    return _canonical(url), _check_ref(ref)


def _split_ref(path: str) -> tuple[str, str | None]:
    """`org/repo.git@v1.2` -> (`org/repo.git`, `v1.2`). Refs carry no `@`, so
    the last one is the split, and a path with none is a path with no ref."""
    if "@" not in path:
        return path, None
    base, _, ref = path.rpartition("@")
    return base, ref


def _check_ref(ref: str | None) -> str | None:
    if ref is None:
        return None
    ref = ref.strip()
    if not ref:
        raise RepoUrlError("the @ on the end wants a branch, tag or commit "
                           "after it")
    if not _REF.match(ref) or ".." in ref:
        raise RepoUrlError(f"'{ref[:60]}' is not a branch, tag or commit name "
                           f"this tool passes to git")
    return ref


def _canonical(url: str) -> str:
    """Strip a trailing slash and demand a path with something in it. The
    `.git` suffix is left as typed: both forms clone, and the record should
    show what was asked for."""
    url = url.rstrip("/")
    path = urlsplit(url).path.strip("/")
    if not path or "/" not in path:
        raise RepoUrlError(f"'{url[:80]}' names a host and no repository. It "
                           f"wants the form https://github.com/org/repo.")
    return url


def check_host(url: str, allowed_hosts) -> None:
    """Raise unless the URL's host is in the allowlist. `None` means no
    allowlist, which is the CLI's position and never the server's."""
    if allowed_hosts is None:
        return
    host = host_of(url)
    permitted = [h.lower().strip() for h in allowed_hosts if h.strip()]
    if any(host == h or host.endswith("." + h) for h in permitted):
        return
    raise RepoHostNotAllowed(
        f"this reviewer takes repositories from {', '.join(permitted)}. "
        f"'{host}' is outside that list.")


# ----------------------------------------------------------------- clone

def _git_env() -> dict:
    env = dict(os.environ)
    # A private repository fails on the spot instead of holding a thread on a
    # password prompt. `echo` as askpass answers every question with a blank.
    env["GIT_TERMINAL_PROMPT"] = "0"
    env["GIT_ASKPASS"] = "echo"
    env["GIT_SSH_COMMAND"] = "ssh -o BatchMode=yes"
    # No LFS smudge on clone: a repository whose large files live elsewhere
    # would otherwise reach out for them and defeat the size cap.
    env["GIT_LFS_SKIP_SMUDGE"] = "1"
    return env


def _git(args: list[str], *, cwd: Path | None, timeout_s: float) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args], cwd=str(cwd) if cwd else None, env=_git_env(),
            capture_output=True, text=True, timeout=timeout_s,
            stdin=subprocess.DEVNULL,
        )
    except FileNotFoundError as exc:
        raise RepoCloneFailed("git is not on the PATH, and reviewing a URL "
                              "needs it") from exc
    except subprocess.TimeoutExpired as exc:
        raise RepoCloneFailed(f"git took longer than {timeout_s:.0f}s, so the "
                              f"clone was stopped") from exc


def _fail_text(proc: subprocess.CompletedProcess) -> str:
    text = (proc.stderr or proc.stdout or "").strip()
    lowered = text.lower()
    if "authentication" in lowered or "could not read username" in lowered \
            or "terminal prompts disabled" in lowered:
        return ("git asked for a credential, which points at a private or "
                "missing repository. Public repositories only.")
    if "remote branch" in lowered and "not found" in lowered:
        return "that branch or tag is missing from the repository"
    if "not found" in lowered or ("repository" in lowered and "exist" in lowered):
        return "git found no repository at that address"
    # Last line of git's own words, trimmed. Credentials never appear here
    # because none were supplied.
    last = text.splitlines()[-1] if text else "git gave no reason"
    return f"git could not clone: {last[:200]}"


def _remove_readonly(func, path, _exc_info) -> None:
    """Windows marks git's object files read-only, and rmtree stops on the
    first one. Clear the bit and try that path again."""
    os.chmod(path, stat.S_IWRITE)
    func(path)


def remove_tree(path: Path) -> None:
    """Remove a workspace, read-only objects included. Missing is fine."""
    path = Path(path)
    if path.exists():
        shutil.rmtree(path, onerror=_remove_readonly)


def measure(root: Path) -> tuple[int, int]:
    """(files, bytes) under root, `.git` excluded."""
    files = total = 0
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != ".git"]
        for name in filenames:
            files += 1
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:
                continue
    return files, total


def clone(url: str, ref: str | None, dest: Path, *, depth: int = 1,
          timeout_s: float = 120, max_bytes: int = 50_000_000,
          max_files: int = 5000, allowed_hosts=None,
          allow_local: bool = False) -> CloneResult:
    """Shallow-clone `url` at `ref` into `dest` and hand back the tree.

    `dest` is created here and removed again on any failure, so the caller
    holds a checkout or nothing. On success the `.git` directory is gone and
    the commit it stood at is in the result. `allow_local` lets a file path
    through for the tests' benefit; the CLI and the server never set it.
    """
    dest = Path(dest)
    ref = _check_ref(ref)
    if not is_git_url(url):
        if not allow_local:
            raise RepoUrlError(f"'{str(url)[:80]}' is not a repository URL")
    else:
        # The canonical form is what was parsed, and the record shows it. A
        # host check on the string as given rather than as parsed would let a
        # user@host form slip past by looking different.
        url, inline_ref = parse_repo_ref(url)
        if inline_ref and ref and inline_ref != ref:
            raise RepoUrlError("the URL names one ref and --ref another; "
                               "pick one")
        ref = ref or inline_ref
        check_host(url, allowed_hosts)
    if url.startswith("-"):
        raise RepoUrlError("a repository address starts with a scheme, not a dash")

    if dest.exists() and any(dest.iterdir()):
        raise RepoError(f"{dest} already has files in it")
    dest.parent.mkdir(parents=True, exist_ok=True)

    try:
        args = ["clone", "--quiet", f"--depth={depth}", "--no-recurse-submodules",
                "--no-tags",
                # Symlinks stay as text files. A link out of the tree is a
                # read past the boundary the policy would otherwise refuse.
                "-c", "core.symlinks=false"]
        want_sha = bool(ref and _SHA.match(ref))
        if ref and not want_sha:
            args += ["--branch", ref]
        args += ["--", url, str(dest)]
        proc = _git(args, cwd=None, timeout_s=timeout_s)
        if proc.returncode != 0:
            raise RepoCloneFailed(_fail_text(proc))

        if want_sha:
            # A commit rather than a name. Fetched by hash at depth one, which
            # every current GitHub and GitLab allows and a server that does
            # not says so plainly.
            fetched = _git(["fetch", "--quiet", "--depth=1", "origin", ref],
                           cwd=dest, timeout_s=timeout_s)
            if fetched.returncode != 0:
                lines = (fetched.stderr or "").strip().splitlines()
                why = lines[-1] if lines else "no reason given"
                raise RepoCloneFailed(f"the repository would not serve commit "
                                      f"{ref[:12]}: {why[:200]}")
            out = _git(["checkout", "--quiet", "--detach", "FETCH_HEAD"],
                       cwd=dest, timeout_s=timeout_s)
            if out.returncode != 0:
                raise RepoCloneFailed(f"checkout of {ref[:12]} failed: "
                                      f"{(out.stderr or '').strip()[-200:]}")

        head = _git(["rev-parse", "HEAD"], cwd=dest, timeout_s=30)
        commit = head.stdout.strip()
        if head.returncode != 0 or not _SHA.match(commit):
            raise RepoCloneFailed("git produced a checkout with no readable "
                                  "HEAD, so nothing about it can be recorded")

        files, size = measure(dest)
        if files > max_files or size > max_bytes:
            raise RepoTooLarge(files=files, bytes=size, max_files=max_files,
                               max_bytes=max_bytes)

        # History is out of scope for the review and out of reach for the
        # agents. Recorded first, removed second, and never the other way.
        remove_tree(dest / ".git")
    except BaseException:
        remove_tree(dest)
        raise

    return CloneResult(path=dest, url=url, ref=ref, commit_sha=commit,
                       files=files, bytes=size)


def workspace_dir(prefix: str = "crucible-repo-") -> Path:
    """A fresh, empty, private directory for one clone. The caller removes
    it, and does so in a `finally`."""
    return Path(tempfile.mkdtemp(prefix=prefix))
