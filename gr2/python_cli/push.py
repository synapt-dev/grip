"""Native `push` command (grip#752 slice 1, D3).

This is the verb that started the scope-default investigation: `gr push`
(gr1) operates gripspace-wide by default with no --repo scoping (grip#755),
which is what led to Opus's steer -- and from there, to making cwd-repo
scope the consistent default across all four verbs, not just push.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .gitops import git


class PushError(Exception):
    pass


@dataclass(frozen=True)
class PushResult:
    pushed: bool
    reason: str | None = None


def push(repo: Path, *, set_upstream: bool = False, force: bool = False) -> PushResult:
    """Push the current branch in `repo`, natively -- no gr1 dependency.

    Diverged remote history raises `PushError` rather than silently forcing
    -- `force=True` is opt-in, matching git's own default-safe behavior.
    """
    branch = git(repo, "branch", "--show-current").stdout.strip()

    args = ["push"]
    if force:
        args.append("--force")
    if set_upstream:
        args.append("--set-upstream")
    args.extend(["origin", branch])

    proc = git(repo, *args)
    if proc.returncode != 0:
        raise PushError(proc.stderr.strip() or proc.stdout.strip())

    combined = f"{proc.stdout}\n{proc.stderr}".lower()
    if "up to date" in combined or "up-to-date" in combined:
        return PushResult(pushed=False, reason="nothing to push")
    return PushResult(pushed=True)
