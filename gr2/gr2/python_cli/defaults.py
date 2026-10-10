"""What gr2 can know from the repo, so a user never types it: the remote, the head, the review, the branches.

Every value here is a default: a flag the user passes always wins. Each resolver says where its answer came
from, so the verb can print it, and refuses only when the repo genuinely does not say (no upstream and no
origin; zero or several candidate reviews; members on different branches).
"""
from __future__ import annotations

from pathlib import Path

from . import gitops


class Unresolved(ValueError):
    """The repo does not determine this value; the message names the flag that supplies it."""


def _git(repo: Path, *args: str) -> str | None:
    """stdout of one git command through the shared gitops helper, or None when it fails or `repo` is not a dir."""
    if not repo.is_dir():
        return None
    p = gitops.git(repo, *args)
    return p.stdout.strip() if p.returncode == 0 else None


def own_repo(path: Path) -> bool:
    """True only when `path` is itself a repository's top level, never a directory inside an enclosing one."""
    top = _git(path, "rev-parse", "--show-toplevel") if path.is_dir() else None
    return top is not None and Path(top).resolve() == path.resolve()


def current_head(path: Path) -> str | None:
    """The member clone's HEAD commit, or None when the path is not its own repository."""
    return _git(path, "rev-parse", "--verify", "HEAD^{commit}") if own_repo(path) else None


def remote(repo: Path, given: str | None) -> tuple[str, str]:
    """(url-or-path, source). A given value that names a configured remote resolves to its URL."""
    if given:
        url = _git(repo, "remote", "get-url", "--", given) if not given.startswith("-") else None
        return (url, "given remote name") if url else (given, "given")
    # The upstream's remote is read from config, never split from "<remote>/<branch>": both names may hold "/",
    # and "." means the branch tracks a local branch, which has no remote of its own.
    current = _git(repo, "symbolic-ref", "--quiet", "--short", "HEAD")
    configured = _git(repo, "config", "--get", f"branch.{current}.remote") if current else None
    name, source = (configured, "upstream") if configured and configured != "." else ("origin", "origin")
    url = _git(repo, "remote", "get-url", "--", name)
    if not url:
        missing = (f"the branch's upstream remote {name!r} is not configured" if source == "upstream"
                   else "the current branch has no upstream and there is no 'origin'")
        raise Unresolved(f"remote_unresolved: {missing}; pass --remote <url-or-name>")
    return url, source


def shown(url: str) -> str:
    """A remote URL fit to print: any user or password in it is replaced, never echoed."""
    from urllib.parse import urlsplit, urlunsplit
    try:
        parts = urlsplit(url)
    except ValueError:
        return "<remote>"
    if parts.scheme and "@" in parts.netloc:
        return urlunsplit(parts._replace(netloc="***@" + parts.netloc.rsplit("@", 1)[1]))
    return url


def head(repo: Path, given: str | None) -> tuple[str, str]:
    """(full commit id, source). A given value is resolved to its full id when the repo has it."""
    if given:
        full = _git(repo, "rev-parse", "--verify", "--quiet", f"{given}^{{commit}}") if not given.startswith("-") else None
        return (full or given, "given")
    full = _git(repo, "rev-parse", "--verify", "HEAD^{commit}")
    if not full:
        raise Unresolved("head_unresolved: the repository has no commit at HEAD; pass --head <sha>")
    return full, "HEAD"


def branch(repos: list[Path], given: str | None) -> tuple[str, str]:
    """(branch name, source): the members' current branch, which must be the same in every member."""
    if given:
        return given, "given"
    names = {r: _git(r, "symbolic-ref", "--quiet", "--short", "HEAD") if own_repo(r) else None for r in repos}
    found = set(names.values())
    if None in found or len(found) != 1:
        listed = ", ".join(f"{r.name}={n or ('detached' if own_repo(r) else 'not a repository')}"
                           for r, n in names.items())
        raise Unresolved(f"feature_unresolved: members are not on one branch ({listed}); pass --from <branch>")
    return found.pop(), "current branch"


def target_branch(members: list[tuple[Path, str, str]], given: str | None) -> tuple[str, str]:
    """(target branch, source): the one branch, on every member's remote, whose tip is still the reviewed base.

    `members` holds (clone, remote, base) per member. The merge requires the target to sit at the reviewed base,
    so the real target is always a candidate; the remote's HEAD is never consulted. Two or more branches at the
    base (a dev just cut from main, say) refuse rather than guess."""
    if given:
        return given, "given"
    common: set[str] | None = None
    for repo, remote_url, base in members:
        out = _git(repo, "ls-remote", "--symref", "--", remote_url, "HEAD", "refs/heads/*") or ""
        at_base, default = set(), None
        for line in out.splitlines():
            left, _, ref = line.partition("\t")
            if left.startswith("ref: refs/heads/") and ref == "HEAD":
                default = left.removeprefix("ref: refs/heads/")
            elif left == base and ref.startswith("refs/heads/"):
                at_base.add(ref.removeprefix("refs/heads/"))
        # The remote's default branch is a VETO only: it never selects a target, but a branch at the base that is
        # not the default (a stale branch left at the base after the real target moved) is never chosen.
        # Exactly one branch at the base, and it must be the default: two at the base stay ambiguous even when
        # HEAD names one of them.
        at_base = at_base if len(at_base) == 1 and at_base == {default} else set()
        common = at_base if common is None else common & at_base
    if common and len(common) == 1:
        return common.pop(), "default branch at the reviewed base"
    raise Unresolved("into_unresolved: no remote's default branch sits at the reviewed base; pass --into <branch>")


def review(binds: list[str], members_of, heads_of) -> tuple[str, str]:
    """(review id, source): THE bind whose every member head equals that member clone's current HEAD.

    `binds` is every bind id in the workspace; `members_of(bind)` returns its members as (path, head) pairs;
    `heads_of(path)` returns the clone's current HEAD or None."""
    matches, unreadable = [], []
    for bind in binds:
        try:
            members = members_of(bind)
        except Exception:  # one unreadable bind must not hide every other; it is named if nothing matches
            unreadable.append(bind)
            continue
        if members and all(heads_of(path) == h for path, h in members):
            matches.append(bind)
    if len(matches) == 1:
        return matches[0], "pinned review at the current heads"
    if not matches:
        skipped = f" ({len(unreadable)} unreadable pinned review(s) skipped)" if unreadable else ""
        raise Unresolved("review_ambiguous: no pinned review matches the members' current heads" + skipped
                         + "; pin one, or pass the review id")
    raise Unresolved("review_ambiguous: several pinned reviews match the current heads ("
                     + ", ".join(f"gr:{m.removeprefix('gr:')}" for m in matches) + "); pass the review id")
