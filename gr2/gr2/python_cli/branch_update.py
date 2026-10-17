"""`gr2 update --into <branch>`: bring the pushed feature branch you are on up to a moved integration branch.

`review update` acts only on a pinned review; this is the same merge for a branch no review has pinned yet. It
fetches `<branch>` from the remote, merges its tip into the current branch, and stops. It never pushes, never pins,
and never resolves a conflict.

Every fetch passes `--refmap=` and `--no-tags`, so neither a configured `remote.<name>.fetch` refspec nor
`remote.<name>.tagOpt=--tags` writes a ref.

A merge, not a rebase: the pushed head stays an ancestor of the new head, so the next push is a fast-forward.

Refusals, each before anything moves: bad_into (--into is not a plain branch name; checked before any other git
call, so a value like `dev:keep` can never become a fetch that writes a ref), bad_remote (--remote is not a
configured remote: a path, URL or `.` would redefine pushed), detached_head, same_branch (the current branch is the one to merge in),
unresolved_merge, dirty (tracked changes), unpushed (commits the branch of the same name on the remote lacks, or no
such branch), behind_remote (that branch has commits this one lacks), fetch_failed, untracked_in_the_way. Always a
merge commit, even under `merge.ff=only` or when the integration branch already contains this one. A conflict aborts
the merge, so the tree is left as it was; any other failed merge (merge_failed, e.g. no committer identity) is
aborted the same way.

Exit codes: 0 done (merged, or already up to date), 2 refused, 3 conflict (merge aborted).
"""
from __future__ import annotations

from pathlib import Path

from . import gitops
from .review_update import _conflicts, _head, _in_the_way, merge_in_progress

EXIT_DONE, EXIT_REFUSED, EXIT_CONFLICT = 0, 2, 3

# Refused outright, whatever git would make of them: a colon or a leading '+' gives the fetch refspec a write side,
# a leading '-' reads as an option, and the rest are revision syntax or not a name at all.
_NOT_IN_A_NAME = (":", "*", "^", "~", "?", "[", "\\", "..", "@{")


def _bad_into(repo: Path, into: str) -> str:
    if (not into or into[0] in "-+" or into.endswith(".lock") or any(c.isspace() for c in into)
            or any(s in into for s in _NOT_IN_A_NAME)):
        return f"bad_into: {into!r} is not a plain branch name"
    if gitops.run(repo, "check-ref-format", "--branch", into).returncode:
        return f"bad_into: {into!r} is not a branch name git accepts"
    return ""


def _fetch(repo: Path, remote: str, branch: str) -> str:
    proc = gitops.run(repo, "fetch", "-q", "--refmap=", "--no-tags", "--", remote, f"refs/heads/{branch}")
    if proc.returncode:
        return ""
    return gitops.run(repo, "rev-parse", "-q", "--verify", "FETCH_HEAD^{commit}").stdout.strip()


def update(repo: Path, into: str, remote: str = "origin") -> tuple[int, dict]:
    repo = repo.resolve()
    receipt: dict = {"repo": str(repo), "branch": None, "into": into, "remote": remote, "base": None,
                     "head": None, "state": None, "conflicts": [], "refused": None, "exit": EXIT_REFUSED}

    def done(code: int, state: str | None = None, refused: str | None = None) -> tuple[int, dict]:
        receipt.update(exit=code, state=state, refused=refused)
        return code, receipt

    bad = _bad_into(repo, into)
    if bad:
        return done(EXIT_REFUSED, refused=bad)

    # A configured remote only: a path, a URL or `.` would make "pushed" mean whatever that repository holds.
    if gitops.run(repo, "config", "--get", f"remote.{remote}.url").returncode:
        return done(EXIT_REFUSED, refused=f"bad_remote: {remote!r} is not a configured remote")

    # The full ref, not --short: a tag of the same name would shorten it to heads/<name>.
    ref = gitops.run(repo, "symbolic-ref", "-q", "HEAD").stdout.strip()
    branch = ref.removeprefix("refs/heads/") if ref.startswith("refs/heads/") else ""
    if not branch:
        return done(EXIT_REFUSED, refused="detached_head: check out the feature branch first")
    receipt["branch"] = branch
    if branch == into:
        return done(EXIT_REFUSED, refused=f"same_branch: {branch} is the branch to merge in; check out the feature branch")
    if merge_in_progress(repo):
        return done(EXIT_REFUSED, refused="unresolved_merge: resolve and commit, or `git merge --abort`, then run update again")
    changed = [line[3:] for line in gitops.run(repo, "status", "--porcelain", "--untracked-files=no").stdout.splitlines()]
    if changed:
        return done(EXIT_REFUSED, refused=f"dirty: {', '.join(changed)}; commit or stash, then run update again")
    head = receipt["head"] = _head(repo)

    # Pushed means the branch of the same name on the remote is this head (the configured upstream is not
    # consulted): the merge lands on work others can already see.
    pushed = _fetch(repo, remote, branch)
    if not pushed or gitops.run(repo, "merge-base", "--is-ancestor", head, pushed).returncode:
        return done(EXIT_REFUSED, refused=f"unpushed: {remote} {branch} lacks {head[:12]}; push it, then run update again")
    if pushed != head:
        return done(EXIT_REFUSED, refused=f"behind_remote: {remote} {branch} is at {pushed[:12]}, past {head[:12]}; "
                                          "bring the branch level with it first")

    tip = _fetch(repo, remote, into)
    if not tip:
        return done(EXIT_REFUSED, refused=f"fetch_failed: {remote} {into}")
    receipt["base"] = tip
    if gitops.run(repo, "merge-base", "--is-ancestor", tip, "HEAD").returncode == 0:
        return done(EXIT_DONE, "up_to_date")
    paths = _in_the_way(repo, tip)
    if paths:
        return done(EXIT_REFUSED, refused=f"untracked_in_the_way: {', '.join(paths)}; move or remove them, then run update again")

    proc = gitops.run(repo, "merge", "--no-edit", "--no-ff", "-m", f"Merge {into} ({tip[:12]}) into {branch}", tip)
    if proc.returncode == 0:
        receipt["head"] = _head(repo)
        return done(EXIT_DONE, "merged")
    # A refusing hook (pre-merge-commit, commit-msg) also leaves a merge in progress, with no conflicted path.
    if merge_in_progress(repo):
        receipt["conflicts"] = _conflicts(repo)
        gitops.run(repo, "merge", "--abort")
    if receipt["conflicts"]:
        return done(EXIT_CONFLICT, refused=f"conflict: {', '.join(receipt['conflicts'])}; merge aborted, the tree is as it "
                                           f"was; merge {remote}/{into} by hand to resolve")
    return done(EXIT_REFUSED, refused=f"merge_failed: {(proc.stderr or proc.stdout).strip()}")
