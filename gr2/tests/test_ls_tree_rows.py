"""ls-tree entries read through gitops.ls_tree_rows: names git C-quotes without -z come back verbatim."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from gr2.python_cli import gitops
from gr2.python_cli.grip import grip_diff, grip_init, grip_log

ENV = {**os.environ, "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t"}
TAB, NL = "a\tb", "c\nd"


def _git(repo: Path, *args: str, data: str | None = None) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], input=data, capture_output=True, text=True,
                          check=True, env=ENV).stdout.strip()


def _tree(repo: Path, entries: dict[str, str | dict]) -> str:
    rows = []
    for name, value in entries.items():
        if isinstance(value, dict):
            rows.append(f"040000 tree {_tree(repo, value)}\t{name}\0")
        else:
            rows.append(f"100644 blob {_git(repo, 'hash-object', '-w', '--stdin', data=value)}\t{name}\0")
    return _git(repo, "mktree", "-z", data="".join(rows))


def _commit(repo: Path, repos: dict[str, str], parent: str | None) -> str:
    tree = _tree(repo, {"repos": {name: {"commit": commit} for name, commit in repos.items()}})
    sha = _git(repo, "commit-tree", tree, "-m", "snap", *(["-p", parent] if parent else []))
    _git(repo, "update-ref", "HEAD", sha)
    return sha


def test_reader_splits_on_nul_only() -> None:
    listing = f"100644 blob {'1' * 40}\t{TAB}\x00040000 tree {'2' * 40}\t{NL}\x00100644 blob {'3' * 40}\te f\0"
    assert gitops.ls_tree_rows(listing) == [
        ("100644", "blob", "1" * 40, TAB),
        ("040000", "tree", "2" * 40, NL),
        ("100644", "blob", "3" * 40, "e f"),
    ]


def test_reader_empty_tree() -> None:
    assert gitops.ls_tree_rows("") == []


def test_grip_log_and_diff_read_quoted_names(tmp_path: Path) -> None:
    grip_init(tmp_path)
    store = tmp_path / ".grip"
    a = _commit(store, {TAB: "1" * 40, NL: "1" * 40, "plain": "1" * 40}, None)
    b = _commit(store, {TAB: "2" * 40, NL: "1" * 40, "plain": "1" * 40}, a)
    assert sorted(grip_log(tmp_path)[0].repos) == sorted([TAB, NL, "plain"])
    diff = grip_diff(tmp_path, a, b)
    assert diff.changed == {TAB: {"old_commit": "1" * 40, "new_commit": "2" * 40}}
    assert diff.added == [] and diff.removed == []
