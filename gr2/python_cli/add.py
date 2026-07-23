"""Native `add` command (grip#752 slice 1, D3).

grip#754's root cause, revisited: gr1's `gr add` stages files individually
via multi-repo path attribution (a gripspace-relative path has to be matched
to the right repo before `git add` ever runs), and blanket-treats any git
stderr containing "did not match any files" as "not in this repo" -- which
fired on a genuinely valid, real, untracked file (confirmed reproduced twice,
different sessions). gr2's cwd-repo-scope-by-default (grip#755's finding,
now product canon) removes the attribution step entirely: there is exactly
one target repo, so "is this path in this repo" is answered by checking the
path resolves under repo root, not by parsing git's stderr text. A path that
genuinely doesn't exist gets a precise, checked message; anything else that
makes `git add` fail is a real error, not a silent skip.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .gitops import git


class AddError(Exception):
    pass


@dataclass(frozen=True)
class AddResult:
    staged: list[str]
    missing: list[str]


def _staged_file_names(repo: Path) -> list[str]:
    proc = git(repo, "diff", "--cached", "--name-only")
    return [line for line in proc.stdout.splitlines() if line]


def stage_files(repo: Path, paths: list[str]) -> AddResult:
    """Stage `paths` in `repo`, natively -- no gr1 multi-repo attribution.

    A path that does not exist on disk (checked directly, never inferred
    from git's stderr) is reported in `missing` and does not block staging
    the rest. A path that exists on disk but git still refuses (e.g. it
    resolves outside `repo`) raises `AddError` -- that's a real error, never
    a silent skip (grip#754's exact failure mode, inverted).
    """
    staged: list[str] = []
    missing: list[str] = []

    for path in paths:
        if path == ".":
            before = set(_staged_file_names(repo))
            proc = git(repo, "add", ".")
            if proc.returncode != 0:
                raise AddError(proc.stderr.strip())
            after = set(_staged_file_names(repo))
            staged.extend(sorted(after - before))
            continue

        candidate = Path(path)
        full_path = candidate if candidate.is_absolute() else (repo / candidate)
        if not full_path.exists():
            missing.append(path)
            continue

        proc = git(repo, "add", "--", path)
        if proc.returncode != 0:
            raise AddError(proc.stderr.strip())
        staged.append(path)

    return AddResult(staged=staged, missing=missing)
