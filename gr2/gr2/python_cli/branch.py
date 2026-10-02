"""Native `branch` command (grip#752 slice 1, D3).

Create/switch, --base support (grip#746: gr1's `gr branch` has no base-ref
argument, forcing raw-git fallbacks), existing-branch-switches-not-errors
(mirrors gr1's grip#401 fix), worktree-conflict detection (mirrors gr1's
real, cited pain -- grip#750).
"""

from __future__ import annotations

from pathlib import Path

from .gitops import git


class BranchError(Exception):
    pass


def _branch_exists(repo: Path, name: str) -> bool:
    proc = git(repo, "rev-parse", "--verify", f"refs/heads/{name}")
    return proc.returncode == 0


def _ref_exists(repo: Path, ref: str) -> bool:
    proc = git(repo, "rev-parse", "--verify", ref)
    return proc.returncode == 0


def _raise_for_checkout_failure(name: str, stderr: str) -> None:
    if "is already used by worktree at" in stderr:
        marker = "worktree at '"
        start = stderr.find(marker)
        if start != -1:
            start += len(marker)
            end = stderr.find("'", start)
            worktree_path = stderr[start:end] if end != -1 else "another worktree"
            raise BranchError(
                f"Branch '{name}' is checked out in another worktree at '{worktree_path}'. "
                f"Use that worktree or choose a different branch name."
            )
        raise BranchError(f"Branch '{name}' is already checked out in another worktree.")
    raise BranchError(stderr.strip())


def create_branch(repo: Path, name: str, base: str | None = None) -> None:
    """Create and switch to branch `name` in `repo`.

    If `name` already exists, switch to it instead of erroring (grip#401).
    If `base` is given, the branch is created (or reset, if it already
    exists) to point at `base` -- resolved per-repo, matching `git checkout
    -B`'s own create-or-reset semantics. A missing `base` ref raises
    immediately, before any git mutation -- never a silent fallback to HEAD
    (grip#746's acceptance criteria).
    """
    if base is not None and not _ref_exists(repo, base):
        raise BranchError(f"base ref '{base}' does not exist in {repo}")

    if base is not None:
        proc = git(repo, "checkout", "-B", name, base)
    elif _branch_exists(repo, name):
        proc = git(repo, "checkout", name)
    else:
        proc = git(repo, "checkout", "-b", name)

    if proc.returncode != 0:
        _raise_for_checkout_failure(name, proc.stderr)
