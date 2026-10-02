"""A review bind on a NATIVE root lives in the root's own `.git` under `refs/dev.synapt.grip/__reviews__/<commit>`.

THE SMALLEST WORKING PROOF: one bind into the root ref, read back by `verify`,
content-equivalent to the old store.

  * no `<root>/.grip/.git` appears (there is no second repo);
  * exactly one `refs/dev.synapt.grip/__reviews__/<commit>` exists and it is the printed `gr:<commit>`;
  * the root's commit log and `git status` are unchanged by the bind (the ref is outside both);
  * `review verify` on the id reports `tree_matches: True`;
  * the bound TREE equals the tree the alpha `.grip/.git` store makes for the SAME rows. A tree is
    content-addressed, so this is the exact-equivalence witness that nothing but the home moved.
"""

from __future__ import annotations

from pathlib import Path

from gr2.python_cli import grip as grip_mod

from tests.test_review_bind_native_store import _bind_args, _unpushed_head
from tests.test_store_break_attempts import _cli, _git_out, two_member_ws  # noqa: F401

_REFS = "refs/dev.synapt.grip/__reviews__/"


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


def test_the_root_ref_bind_tree_equals_the_alpha_store_bind_tree(two_member_ws: Path, tmp_path: Path) -> None:
    """CONTROL: the same rows bound through the alpha `.grip/.git` path produce the same tree."""
    ws = two_member_ws
    assert _cli("store", "init", str(ws))[0] == 0
    remote, base, head = _unpushed_head(ws)
    code, out = _cli(*_bind_args(ws, remote, base, head))
    assert code == 0, out
    native_commit = out.strip().splitlines()[-1][3:]
    native_tree = grip_mod.verify_review_commit(ws, native_commit)["stored_tree"]

    alpha_root = tmp_path / "alpha-root"
    alpha_root.mkdir()
    grip_mod.grip_init(alpha_root)
    assert not grip_mod._is_native_workspace(alpha_root), "control must take the alpha path"
    row = {"key": "alpha", "remote": remote, "path": "alpha", "head": head, "base": base,
           "ref": "refs/heads/main", "title": "", "body": "", "source": str(ws / "alpha")}
    alpha_commit = grip_mod.create_review_bind_commit(alpha_root, [row])
    alpha_tree = grip_mod.verify_review_commit(alpha_root, alpha_commit)["stored_tree"]

    assert native_tree == alpha_tree
    assert (alpha_root / ".grip" / ".git").is_dir(), "control did not use the alpha store"
