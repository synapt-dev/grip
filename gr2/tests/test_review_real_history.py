"""gr2 review reads a member's change in the real history: a forged replace ref or commit-graph never moves it.

review_diff.change names the fork point (merge-base base head), the commit count (rev-list base..head) and the diff.
A replace ref that swaps the head for a commit whose parent is the base would make the change read as everything
between the two trees, against the base itself; a forged commit-graph can invent a fork point for two unrelated
histories. Both reads go through gitops.real_history_run (GIT_NO_REPLACE_OBJECTS, an empty graft file, and
-c core.commitGraph=false). Kills: dropping the env from real_history_run reds the first row; dropping the -c
core.commitGraph=false reds the second."""
from __future__ import annotations

import json
import os
import subprocess

import pytest

from gr2.python_cli import read_review, review_diff
from gr2.python_cli.app import app
from gr2.python_cli.grip import GripReviewRefused

from tests.test_is_ancestor_commit_graph import _forge, _world
from tests.test_is_ancestor_replace import _commit, _git, _replace_with_fake_child, _setup
from tests.test_review_approvals import approve_as, runner, set_policy, world  # noqa: F401  (world is a fixture)
from tests.test_review_read_pr import forge  # noqa: F401  (a fixture)
from tests.test_review_reads import repin, since


def _member(work, base: str, head: str) -> dict[str, object]:
    return {"key": "m", "path": work.name, "base": base, "head": head}


def test_change_under_a_forged_replace_ref_reads_the_real_history(tmp_path):
    work, feature, target = _setup(tmp_path)
    real = review_diff.change(tmp_path, _member(work, target, feature), with_diff=True)
    assert real["files"] == ["f"]
    assert real["diffstat"] == {"files": 1, "commits": 1, "added": 1, "removed": 0}
    _replace_with_fake_child(work, feature, target)
    # The probe is real: plain git now forks feature from target and reads "t" as removed by the change.
    assert _git(work, "merge-base", target, feature) == target
    forged = review_diff.change(tmp_path, _member(work, target, feature), with_diff=True)
    assert forged["files"] == real["files"]
    assert forged["diffstat"] == real["diffstat"]
    assert forged["diff"] == real["diff"]


def test_change_under_a_forged_commit_graph_has_no_fork_point(tmp_path):
    w, x, p, t = _world(tmp_path)
    _forge(w, x, p)
    # The probe is real: plain git reads x as the fork point of x and t through the forged graph.
    assert _git(w, "merge-base", x, t) == x
    with pytest.raises(GripReviewRefused) as caught:
        review_diff.change(tmp_path, _member(w, x, t), with_diff=False)
    assert "member_no_merge_base" in str(caught.value)


def test_change_under_a_forged_replace_ref_reads_the_real_files_count_and_diff(tmp_path):
    """review_diff._git: the numstat diff, the commit count and the diff. History r-b-x-h; a replace ref swaps h for a
    child of b that adds t, so plain git reads files [t] and 1 commit; the real change is files [h, x] and 2 commits.
    The first row's forge keeps the tree and the count, so it cannot see these reads."""
    work = tmp_path / "work"
    _git(tmp_path, "init", "-q", "-b", "main", str(work))
    _commit(work, "r")
    b = _commit(work, "b")
    _commit(work, "x")
    h = _commit(work, "h")
    real = review_diff.change(tmp_path, _member(work, b, h), with_diff=True)
    _git(work, "checkout", "-q", "-b", "side", b)
    _git(work, "replace", h, _commit(work, "t"))
    # The probe is real: plain git now reads the change as t alone, in one commit.
    assert _git(work, "diff", "--name-only", f"{b}...{h}").split() == ["t"]
    assert _git(work, "rev-list", "--count", f"{b}..{h}") == "1"
    for with_diff in (False, True):
        read = review_diff.change(tmp_path, _member(work, b, h), with_diff=with_diff)
        assert read["files"] == ["h", "x"], read
        assert read["diffstat"] == {"files": 2, "commits": 2, "added": 2, "removed": 0}, read
    assert read["diff"] == real["diff"] and "+t" not in read["diff"] and "+x" in read["diff"], read


def _fake(repo, tree: str, *parents: str, name: str = "t") -> str:
    """A commit object with `tree` and `parents`, authored by `name`; nothing points at it."""
    args = [a for p in parents for a in ("-p", p)]
    env = {**os.environ, "GIT_AUTHOR_NAME": name, "GIT_AUTHOR_EMAIL": "t@example.invalid",
           "GIT_COMMITTER_NAME": name, "GIT_COMMITTER_EMAIL": "t@example.invalid"}
    return subprocess.run(["git", "-C", str(repo), "commit-tree", tree, *args, "-m", "fake"], env=env, check=True,
                          capture_output=True, text=True).stdout.strip()


def test_read_pr_diffstat_under_a_forged_replace_ref_is_the_real_change(tmp_path, forge):
    """read_review:92: the three-dot stat reads the fork point's tree itself; a replace ref that gives the fork point
    main's tree would make the stat read c.txt as removed by the PR."""
    bare, base, head = forge
    dest = tmp_path / "dest"
    _git(tmp_path, "init", "-q", str(dest))
    _git(dest, "fetch", "-q", str(bare), "main:refs/x/main")
    _git(dest, "replace", base, _fake(dest, _git(dest, "rev-parse", "refs/x/main^{tree}")))
    read = read_review._fill(dest, "acme/widget", 7, str(bare))
    assert "c.txt" in _git(dest, "diff", "--stat", f"{base}...{head}")  # the probe is real: plain git reads the forge
    assert read.base == base and "b.txt" in read.stat and "c.txt" not in read.stat, read.stat


def test_since_my_stamp_under_a_forged_replace_ref_reads_the_real_range(world):
    """approvals:457: a replace ref that gives the new head an invented parent would add a commit to the range the
    reviewer is shown. (A forged tree on a pinned head is refused before :459, approval_head_tree_unmeasurable.)"""
    assert approve_as(world, "Approver B").exit_code == 0
    _, old = world["rid"], world["head"]
    _, new = repin(world, "moved\n")
    member = world["member"]
    tree = _git(member, "rev-parse", f"{new}^{{tree}}")
    _git(member, "replace", new, _fake(member, tree, _fake(member, tree, old)))
    assert len(_git(member, "rev-list", f"{old}..{new}").split()) == 2  # the probe is real
    out = json.loads(since(world).stdout)
    assert "members" in out, out
    [m] = out["members"]
    assert m["commits"] == [new] and m["paths"] == ["payload"], m


def test_merge_under_a_forged_replace_ref_reads_the_real_fork_point(world):
    """merge_gate:403: the receipt's merge-base must be the target; a replace ref that makes the head a root commit
    would make plain git find no merge-base and refuse a head the real history carries."""
    set_policy(world, 1)
    assert approve_as(world, "Approver B").exit_code == 0
    member, head = world["member"], world["head"]
    _git(member, "replace", head, _fake(member, _git(member, "rev-parse", f"{head}^{{tree}}")))
    probe = subprocess.run(["git", "-C", str(member), "merge-base", head, world["base"]], capture_output=True, text=True)
    assert probe.returncode == 1 and not probe.stdout  # the probe is real: plain git finds no fork point
    merged = runner.invoke(app, ["review", "merge"])
    [row] = json.loads(merged.stdout)["members"]
    # The ancestry read at :403 and the gate's merge-tree both read the real history, so the forge refuses
    # nothing: the head merges.
    assert row["state"] == "merged" and row["refused"] is None, merged.output


def test_approve_under_a_forged_replace_ref_reads_the_real_authors(world):
    """approvals:226: the approver is refused when they authored the range; a replace ref that slips a commit by the
    approver under the head would refuse a stamp the real history allows."""
    member = world["member"]
    tree = _git(member, "rev-parse", f"{world['head']}^{{tree}}")
    planted = _fake(member, tree, world["base"], name="Approver B")
    _git(member, "replace", world["head"], _fake(member, tree, planted))
    assert "Approver B" in _git(member, "log", "--format=%an", f"{world['base']}..{world['head']}")  # the probe is real
    approved = approve_as(world, "Approver B")
    assert approved.exit_code == 0, approved.output


def test_read_pr_base_under_a_forged_replace_ref_is_the_real_fork_point(tmp_path, forge):
    """read_review:90: a replace ref that makes the PR head a root commit would leave plain git no fork point."""
    bare, base, head = forge
    dest = tmp_path / "dest"
    _git(tmp_path, "init", "-q", str(dest))
    _git(dest, "fetch", "-q", str(bare), f"{head}:refs/x/head")
    _git(dest, "replace", head, _fake(dest, _git(dest, "rev-parse", f"{head}^{{tree}}")))
    probe = subprocess.run(["git", "-C", str(dest), "merge-base", base, head], capture_output=True, text=True)
    assert probe.returncode == 1  # the probe is real: plain git finds no fork point
    read = read_review._fill(dest, "acme/widget", 7, str(bare))
    assert read.base == base and read.head == head, read
