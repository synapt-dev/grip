"""prune's cherry and squash checks and the pin coverage read take their history in the real history.

A replace ref (refs/replace/<sha>) swaps a commit for another on every plain read git makes, so a plain `git merge-base`
can name a later base than the real one. A later base drops the branch's own unlanded commits from the range it
compares: prune then reads a branch carrying unlanded work as squash-merged, and coverage reads a pin as landed. Both
reads use gitops.real_history_run, as gitops.is_ancestor already does."""
from __future__ import annotations

import subprocess
from pathlib import Path

from gr2.python_cli import grip_cli, prune


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True).stdout.strip()


def _commit(repo: Path, name: str, *, remove: bool = False, msg: str = "") -> str:
    if remove:
        _git(repo, "rm", "-q", name)
    else:
        (repo / name).write_text(name + "\n")
        _git(repo, "add", name)
    _git(repo, "-c", "user.name=t", "-c", "user.email=t@example.invalid", "commit", "-qm", msg or ("rm " if remove else "") + name)
    return _git(repo, "rev-parse", "HEAD")


def _fake(repo: Path, tree_of: str, parent: str) -> str:
    """A commit holding `tree_of`'s tree whose only parent is `parent`."""
    tree = _git(repo, "rev-parse", f"{tree_of}^{{tree}}")
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                           "commit-tree", tree, "-p", parent, "-m", "fake"],
                          check=True, capture_output=True, text=True).stdout.strip()


def _init(tmp_path: Path) -> tuple[Path, str]:
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    return work, _commit(work, "a")


def test_squash_check_does_not_drop_unlanded_work_through_a_replaced_target(tmp_path):
    # feature: A - P (adds p) - Q (adds q). main: A - S (adds q only). p never landed.
    work, a = _init(tmp_path)
    _git(work, "checkout", "-q", "-b", "feature")
    _commit(work, "p")
    q = _commit(work, "q")
    _git(work, "checkout", "-q", "main")
    s = _commit(work, "q")
    target_ids = {prune._patch_id(work, a, s): s}
    # refs/replace/S -> a commit with S's tree on top of P: plain merge-base(main, feature) is now P, and P..Q is
    # just the q change, the same patch-id as S.
    p = _git(work, "rev-parse", "feature^")
    _git(work, "replace", s, _fake(work, q, p))
    assert _git(work, "merge-base", "main", "feature") == p
    assert prune._squash_merged(work, "main", "feature", target_ids) == (False, None)


def test_cherry_check_does_not_read_unlanded_work_as_upstream_through_a_replaced_target(tmp_path):
    # feature: A - P (adds p). main: A - S (adds s). p never landed.
    work, _ = _init(tmp_path)
    _git(work, "checkout", "-q", "-b", "feature")
    p = _commit(work, "p")
    _git(work, "checkout", "-q", "main")
    s = _commit(work, "s")
    assert prune._cherry_merged(work, "main", "feature") == (False, 1)
    # refs/replace/S -> a commit with S's tree on top of P: a plain walk of main now reaches P, so plain
    # `git cherry main feature` has no commit unique to feature and prints no '+' line.
    _git(work, "replace", s, _fake(work, s, p))
    assert _git(work, "cherry", "main", "feature") == ""
    assert prune._cherry_merged(work, "main", "feature") == (False, 1)


def test_coverage_does_not_read_a_pin_as_landed_through_a_replaced_pin(tmp_path):
    # pin: A - P (adds p) - Q (adds q). upstream: A - B (adds p) - R (removes p) - S (adds q). p landed, then was
    # reverted, so the pin's work is not on upstream.
    work, a = _init(tmp_path)
    _git(work, "checkout", "-q", "-b", "pin")
    _commit(work, "p")
    q = _commit(work, "q")
    _git(work, "checkout", "-q", "main")
    b = _commit(work, "p", msg="land p")  # not P itself: same tree, parent and second make the same commit
    _commit(work, "p", remove=True)
    s = _commit(work, "q")
    assert grip_cli._coverage_of(work, q, s) == grip_cli.COVERAGE_UNCOVERED
    # refs/replace/Q -> a commit with Q's tree whose only parent is B: plain merge-base(Q, S) is now B, and B..Q
    # reads as the q change alone, which S lands.
    _git(work, "replace", q, _fake(work, q, b))
    assert _git(work, "merge-base", q, s) == b
    assert grip_cli._coverage_of(work, q, s) == grip_cli.COVERAGE_UNCOVERED


def _unlanded_feature(tmp_path: Path) -> tuple[Path, str, str, str]:
    # feature: A - P (adds p). main: A - S (adds s). p never landed; main is checked out.
    work, a = _init(tmp_path)
    _git(work, "checkout", "-q", "-b", "feature")
    p = _commit(work, "p")
    _git(work, "checkout", "-q", "main")
    s = _commit(work, "s")
    assert prune.list_merged_branches(work, "main")[0] == []
    return work, a, p, s


def test_prune_does_not_delete_unlanded_work_put_on_the_target_mainline_by_a_replace_ref(tmp_path):
    work, _, p, s = _unlanded_feature(tmp_path)
    # refs/replace/S -> a commit with S's tree on top of P: a plain first-parent walk of main now passes P, whose
    # own patch-id is exactly feature's aggregate diff.
    _git(work, "replace", s, _fake(work, s, p))
    assert p in _git(work, "rev-list", "--first-parent", "main").split()
    assert prune.list_merged_branches(work, "main")[0] == []
    report = prune.prune(work, target="main", execute=True)
    assert report.deleted == []
    assert _git(work, "rev-parse", "feature") == p


def test_squash_map_does_not_read_a_replaced_target_commit_as_the_feature_squash(tmp_path):
    work, a, p, s = _unlanded_feature(tmp_path)
    # refs/replace/S -> a commit with P's tree on top of A: the first-parent walk still names S and A, but a plain
    # `git diff S^ S` now shows the p change, feature's aggregate diff.
    _git(work, "replace", s, _fake(work, p, a))
    assert _git(work, "diff", f"{s}^", s, "--name-only") == "p"
    assert prune.list_merged_branches(work, "main")[0] == []


def test_squash_map_reads_a_replaced_root_as_a_root(tmp_path):
    work, a, _, _ = _unlanded_feature(tmp_path)
    # refs/replace/A -> a commit with A's tree whose parent is an unrelated root X: a plain `A^` now resolves, but
    # the real A has no parent, so a real diff A^ A has nothing to read.
    tree = _git(work, "rev-parse", f"{a}^{{tree}}")
    x = subprocess.run(["git", "-C", str(work), "-c", "user.name=t", "-c", "user.email=t@example.invalid",
                        "commit-tree", tree, "-m", "unrelated root"],
                       check=True, capture_output=True, text=True).stdout.strip()
    _git(work, "replace", a, _fake(work, a, x))
    assert _git(work, "rev-parse", f"{a}^") == x
    assert prune.list_merged_branches(work, "main")[0] == []
