"""Native `commit` command (grip#752 slice 1, D3)."""

from __future__ import annotations

from pathlib import Path

from .gitops import git


class CommitError(Exception):
    pass


def create_commit(repo: Path, message: str, *, amend: bool = False) -> str:
    """Commit staged changes in `repo`, natively -- no gr1 dependency.

    Raises `CommitError` with a message containing "nothing" when there is
    nothing staged to commit, so callers can distinguish that case from a
    real git failure. Returns the resulting commit's full hash.
    """
    args = ["commit", "-m", message]
    if amend:
        args.append("--amend")

    proc = git(repo, *args)
    if proc.returncode != 0:
        combined = f"{proc.stdout}\n{proc.stderr}".lower()
        if "nothing to commit" in combined:
            raise CommitError("nothing staged to commit")
        raise CommitError(proc.stderr.strip() or proc.stdout.strip())

    return git(repo, "rev-parse", "HEAD").stdout.strip()
