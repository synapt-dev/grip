"""`_unpushed_commits` reads `git log --format=%H %S` by "\\n", never str.splitlines: %S is a ref name, and a branch
name may hold U+2028, U+2029 or U+0085.

A repo with a local bare origin holds main (pushed) and one branch a<sep>b with one commit that is not pushed. The
walk must name that commit once, on the whole branch name; splitlines cut the name to refs/heads/a and listed "b" as a
commit. The control branch `ab` reads the same way at both.

The last row pins the shared ls-remote reader, which lane and review readers lean on: a name holding a separator and
an oid after it stays one whole row.
"""
from __future__ import annotations

import subprocess

import pytest

from gr2.python_cli import gitops
from gr2.python_cli.app import _unpushed_commits

SEPS = pytest.mark.parametrize("sep", ["\u2028", "\u2029", "\u0085"], ids=["u2028", "u2029", "u0085"])
_ID = ["-c", "user.name=t", "-c", "user.email=t@example.invalid"]


def _g(repo, *args):
    return subprocess.run(["git", *_ID, "-C", str(repo), *args], capture_output=True, text=True,
                          check=True).stdout.strip()


def _repo_with_unpushed(tmp_path, branch):
    origin = tmp_path / "origin.git"
    repo = tmp_path / "repo"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "f.txt").write_text("one\n")
    _g(repo, "add", "f.txt")
    _g(repo, "commit", "-q", "-m", "one")
    _g(repo, "remote", "add", "origin", str(origin))
    _g(repo, "push", "-q", "origin", "main")
    _g(repo, "fetch", "-q", "origin")
    _g(repo, "checkout", "-q", "-b", branch)
    (repo / "f.txt").write_text("two\n")
    _g(repo, "commit", "-q", "-am", "two")
    return repo, _g(repo, "rev-parse", "HEAD")


@SEPS
def test_a_branch_name_holding_a_separator_is_one_whole_row(tmp_path, sep):
    branch = f"a{sep}b"
    repo, sha = _repo_with_unpushed(tmp_path, branch)

    assert _unpushed_commits(repo, fetch=True) == [(sha, f"refs/heads/{branch}")]


def test_control_branch_ab_is_one_whole_row(tmp_path):
    repo, sha = _repo_with_unpushed(tmp_path, "ab")

    assert _unpushed_commits(repo, fetch=True) == [(sha, "refs/heads/ab")]


@SEPS
def test_ls_remote_rows_keeps_a_name_holding_a_separator_whole(sep):
    a, c = "a" * 40, "c" * 40

    assert gitops.ls_remote_rows(f"{a}\trefs/heads/trunk{sep}{c}\n") == [(a, f"refs/heads/trunk{sep}{c}")]
