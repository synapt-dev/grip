"""convert_worktree_to_clone deletes the staging clone's copied refs/remotes/origin/* by the names for-each-ref lists.

A ref name may hold U+0085 or U+2028, and str.split() cuts at both: an owner branch named
``a<U+0085>refs/heads/x`` arrives in the staging clone as ``refs/remotes/origin/a<U+0085>refs/heads/x`` and, read
by str.split(), becomes the two names ``refs/remotes/origin/a`` and ``refs/heads/x``, so ``update-ref -d`` deletes
the clone's own branch x and leaves the copied tracking ref behind. The listing is read by "\n" only
(gitops.for_each_ref_rows).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import repo_maintenance_prototype as repo_proto


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True)
    assert done.returncode == 0, f"git {' '.join(args)} failed in {cwd}: {done.stderr}"
    return done.stdout.strip()


def _owner_with_real_origin(tmp_path: Path) -> Path:
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q")
    _git(seed, "config", "user.email", "dev@layne.pro")
    _git(seed, "config", "user.name", "Layne Penney")
    (seed / "README.md").write_text("a\n")
    _git(seed, "add", ".")
    _git(seed, "commit", "-qm", "init")
    origin = tmp_path / "real-origin.git"
    _git(tmp_path, "clone", "-q", "--bare", str(seed), str(origin))
    owner = tmp_path / "owner"
    _git(tmp_path, "clone", "-q", str(origin), str(owner))
    return owner


@pytest.mark.parametrize("cut", [chr(0x85), chr(0x2028)], ids=["U+0085", "U+2028"])
def test_a_copied_tracking_ref_holding_a_line_break_is_deleted_whole_and_spares_the_branch(tmp_path, cut):
    owner = _owner_with_real_origin(tmp_path)
    base = _git(owner, "rev-parse", "--abbrev-ref", "HEAD")
    desk = tmp_path / "desk"
    _git(owner, "worktree", "add", "-q", "-b", "x", str(desk), base)
    odd = f"refs/heads/a{cut}refs/heads/x"
    _git(owner, "update-ref", odd, "HEAD")
    copied = "refs/remotes/origin/" + odd[len("refs/heads/"):]
    tip = _git(desk, "rev-parse", "HEAD")

    repo_proto.convert_worktree_to_clone(desk)

    names = _git(desk, "for-each-ref", "--format=%(refname)").split("\n")
    assert copied not in names, names
    assert "refs/heads/x" in names, names
    assert _git(desk, "rev-parse", "refs/heads/x") == tip


@pytest.mark.parametrize("cut", [chr(0x85), chr(0x2028)], ids=["U+0085", "U+2028"])
def test_a_cut_naming_a_branch_the_desk_has_not_checked_out_keeps_that_branch(tmp_path, cut):
    """The cut piece names a tracked branch that is NOT the desk's checkout: on the old read the conversion finished
    and that branch was gone with no error, since the HEAD check only guards the checked-out branch."""
    owner = _owner_with_real_origin(tmp_path)
    base = _git(owner, "rev-parse", "--abbrev-ref", "HEAD")
    desk = tmp_path / "desk"
    _git(owner, "worktree", "add", "-q", "-b", "x", str(desk), base)
    _git(owner, "update-ref", f"refs/heads/z{cut}refs/heads/{base}", "HEAD")

    repo_proto.convert_worktree_to_clone(desk)

    heads = _git(desk, "for-each-ref", "--format=%(refname)", "refs/heads").split("\n")
    assert f"refs/heads/{base}" in heads, heads
    assert "refs/heads/x" in heads, heads
