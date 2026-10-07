"""A review bind on a NATIVE root lives in the root's own `.git` under `the review ref`.

THE SMALLEST WORKING PROOF: one bind into the root ref, read back by `verify`,
content-equivalent to the old store.

  * no `<root>/.grip/.git` appears (there is no second repo);
  * exactly one `the review ref` exists and it is the printed `gr:<commit>`;
  * the root's commit log and `git status` are unchanged by the bind (the ref is outside both);
  * `review verify` on the id reports `tree_matches: True`;
  * the bound tree keeps the `gr2-review-bind/v2` layout (only the home moved).
"""

from __future__ import annotations


from pathlib import Path

from tests.review_ref_helper import REVIEW_REF_PREFIX
from gr2.python_cli import grip as grip_mod

from tests.test_review_bind_native_store import _bind_args, _unpushed_head
from tests.test_store_break_attempts import _cli, _git_out, two_member_ws  # noqa: F401

_REFS = REVIEW_REF_PREFIX


def _ref_names(root: Path) -> list[str]:
    out = _git_out(root, "for-each-ref", "--format=%(refname)", _REFS)
    return [line for line in out.splitlines() if line]


def test_a_bind_is_one_ref_in_the_root_git_and_no_second_repo(two_member_ws: Path) -> None:
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    log_before = _git_out(ws, "rev-list", "--count", "--branches")
    status_before = _git_out(ws, "status", "--short")

    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    gr_id = out.strip().splitlines()[-1]
    commit = gr_id[3:]

    assert not (ws / ".grip" / ".git").exists(), "a bind made a second repo"
    assert _ref_names(ws) == [_REFS + commit]
    assert _git_out(ws, "rev-list", "--count", "--branches") == log_before
    assert _git_out(ws, "status", "--short") == status_before
    assert _git_out(ws, "rev-list", "--parents", "-n", "1", commit).split() == [commit], "a bind commit has a parent"

    code, out = _cli("review", "verify", str(ws), gr_id)
    assert code == 0 and "tree_matches: True" in out, out


def test_the_bound_tree_keeps_the_v2_layout(two_member_ws: Path) -> None:
    """Only the home moved: the tree a bind stores is the `gr2-review-bind/v2` layout the alpha store
    wrote. (At the change itself the SAME rows were also bound through the alpha `.grip/.git` writer and
    the two `stored_tree` ids were equal; that writer is gone, so this pins the layout instead.)"""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    commit = out.strip().splitlines()[-1][3:]

    paths = _git_out(ws, "ls-tree", "-r", "--name-only", commit).splitlines()
    assert sorted(paths) == sorted([
        ".grip/kind", ".grip/policy", ".grip/schema",
        "objects/alpha/head-tree", "objects/alpha/metadata", "objects/alpha/range.patch",
        "observed/alpha/remote-head",
        "repos/alpha/base", "repos/alpha/commit", "repos/alpha/path", "repos/alpha/remote",
        "texts/alpha/body", "texts/alpha/title",
    ])
    assert _git_out(ws, "show", f"{commit}:.grip/schema") == "gr2-review-bind/v2"


def test_the_same_rows_bound_twice_in_one_second_are_one_bind(two_member_ws: Path) -> None:
    """A bind commit is parentless, so identical rows in the same second are the identical commit and
    the second create finds its ref already there. That is the same bind, not an error."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    row = {"key": "alpha", "remote": remote, "path": "alpha", "head": head, "base": base,
           "ref": "refs/heads/main", "title": "", "body": "", "source": str(ws / "alpha")}
    import os
    env = {"GIT_AUTHOR_DATE": "2026-10-02T00:00:00Z", "GIT_COMMITTER_DATE": "2026-10-02T00:00:00Z"}
    old = {k: os.environ.get(k) for k in env}
    os.environ.update(env)
    try:
        first = grip_mod.create_review_bind_commit(ws, [row])
        second = grip_mod.create_review_bind_commit(ws, [row])
    finally:
        for k, v0 in old.items():
            os.environ.pop(k, None) if v0 is None else os.environ.__setitem__(k, v0)
    assert first == second
    assert _ref_names(ws) == [_REFS + first]
