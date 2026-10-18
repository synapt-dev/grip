"""The four for-each-ref readers read each listing by "\\n" (gitops.for_each_ref_rows), never str.splitlines.

A ref name may hold U+2028 or U+0085 (check-ref-format accepts both); it cannot hold "\\n". str.splitlines cuts a
row at either, and str.strip eats either at a row's end, so a name read that way is not the name git listed.
Each row builds the lookalike in a real repo and calls the real reader.

Rows:
F1 grip.list_review_binds: a ref <P>zz<sep><P><c> is one unknown spelling (skipped), not a noncanonical bind <P>zz;
F2 grip.migrate_review_binds: the same name at another commit is not a legacy bind <P><c> whose target mismatches;
F3 prune._local_branches: branches feat<sep>main and main<sep> are listed as named, not as feat, main, main;
F4 agent_record.fold (guard): its rows are "<oid> <tree>", hex only, so no name reaches the split; a lookalike tip
   folds the same as the real one. It is a guard, not a red row: a splitlines reader could not be fooled here either.
"""
from __future__ import annotations

import subprocess

import pytest

from gr2.python_cli import agent_record, grip, prune
from tests.native_root_helper import native_root

SEPS = pytest.mark.parametrize("sep", ["\u2028", "\u0085"], ids=["u2028", "u0085"])
P = grip._REVIEW_REF_PREFIX


def _g(repo, *args):
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          capture_output=True, text=True, check=True).stdout.strip()


def _commits(repo, n):
    out = []
    for i in range(n):
        _g(repo, "commit", "-q", "--allow-empty", "-m", f"c{i}")
        out.append(_g(repo, "rev-parse", "HEAD"))
    return out


@SEPS
def test_f1_list_review_binds(tmp_path, sep):
    ws = native_root(tmp_path / "ws")
    (c,) = _commits(ws, 1)
    _g(ws, "update-ref", f"{P}zz{sep}{P}{c}", c)
    assert grip.list_review_binds(ws) == []


@SEPS
def test_f2_migrate_review_binds(tmp_path, sep):
    ws = native_root(tmp_path / "ws")
    x, y = _commits(ws, 2)
    _g(ws, "update-ref", f"{P}zz{sep}{P}{x}", y)
    _, rows = grip.migrate_review_binds(ws)
    assert rows == []


@SEPS
def test_f3_local_branches(tmp_path, sep):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (c,) = _commits(repo, 1)
    for name in (f"feat{sep}main", f"main{sep}"):
        _g(repo, "update-ref", f"refs/heads/{name}", c)
    assert sorted(prune._local_branches(repo)) == sorted(["main", f"feat{sep}main", f"main{sep}"])


@SEPS
def test_f4_fold_guard(tmp_path, sep):
    repo = tmp_path / "r"
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    (c,) = _commits(repo, 1)
    agents = agent_record.prefix("example")
    _g(repo, "update-ref", f"{agents}a/STOOK", c)
    alone = agent_record.fold(repo, org="example", message="Event")
    _g(repo, "update-ref", f"{agents}b{sep}a/STOOK", c)
    assert agent_record.fold(repo, org="example", message="Event") == alone == {}
