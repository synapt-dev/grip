"""gitops.is_ancestor on a shallow clone: a "no" from inside a truncated graph is "could not tell", never False.

A shallow clone cuts every walk at .git/shallow, and GIT_GRAFT_FILE does not lift it. Fetch an older commit at depth 1
into a depth-1 clone and both commits are present, each a shallow tip: `git merge-base --is-ancestor old new` exits 1
with empty stderr although old IS an ancestor of new. Callers that read False act on a wrong no."""
from __future__ import annotations

import subprocess
from pathlib import Path

from gr2.python_cli import gitops


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(name + "\n")
    _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", name)
    return _git(repo, "rev-parse", "HEAD")


def _full_and_shallow(tmp_path: Path) -> tuple[Path, Path, str, str]:
    """full repo c0 <- c1 <- c2 with tag old at c0; a depth-1 clone that then fetched old at depth 1."""
    full = tmp_path / "full"
    _git(tmp_path, "init", "-q", "-b", "main", str(full))
    old = _commit(full, "c0")
    _git(full, "tag", "old")
    _commit(full, "c1")
    new = _commit(full, "c2")
    shallow = tmp_path / "shallow"
    _git(tmp_path, "clone", "-q", "--depth", "1", "--no-local", str(full), str(shallow))
    _git(shallow, "fetch", "-q", "--depth", "1", "origin", "refs/tags/old:refs/tags/old")
    return full, shallow, old, new


def test_shallow_clone_ancestry_is_unknown_not_false(tmp_path):
    full, shallow, old, new = _full_and_shallow(tmp_path)
    assert _git(shallow, "rev-parse", "--is-shallow-repository") == "true"
    assert _git(shallow, "cat-file", "-t", old) == "commit"  # present, so this is not the missing-object None
    assert gitops.is_ancestor(full, old, new) is True
    assert gitops.is_ancestor(shallow, old, new) is None


def test_full_history_no_is_still_false(tmp_path):
    full, _, old, new = _full_and_shallow(tmp_path)
    assert gitops.is_ancestor(full, new, old) is False
