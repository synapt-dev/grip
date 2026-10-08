"""Native single-repository staging for the Python gr2 CLI."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .gitops import git


class AddError(Exception):
    pass


@dataclass(frozen=True)
class AddResult:
    requested_paths: tuple[str, ...]
    staged_files: tuple[str, ...]


def stage_files(repo: Path, paths: list[str]) -> AddResult:
    """Stage only the requested pathspecs and report their observed index rows.

    ``git add --all`` is deliberate. A deleted tracked path does not exist on
    disk, so a filesystem existence precheck turns a valid deletion into a
    false "missing" error. Git's index is the authority for pathspec validity.
    """
    if not paths:
        raise AddError("at least one path is required")

    requested = tuple(paths)
    try:
        staged = git(repo, "add", "--all", "--", *requested)
    except OSError as exc:
        raise AddError(f"failed to launch git add in {repo}: {exc}") from exc
    if staged.returncode != 0:
        detail = (staged.stderr or staged.stdout).strip()
        raise AddError(detail or f"git add failed with exit {staged.returncode}")

    try:
        observed = git(repo, "diff", "--cached", "--name-only", "-z", "--", *requested)
    except OSError as exc:
        raise AddError(
            f"git add succeeded but staged-path evidence was unavailable: {exc}"
        ) from exc
    if observed.returncode != 0:
        detail = (observed.stderr or observed.stdout).strip()
        raise AddError(detail or "git add succeeded but staged-path evidence was unavailable")

    staged_files = tuple(sorted(name for name in observed.stdout.split("\0") if name))
    return AddResult(requested_paths=requested, staged_files=staged_files)
