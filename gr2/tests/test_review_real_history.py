"""gr2 review reads a member's change in the real history: a forged replace ref or commit-graph never moves it.

review_diff.change names the fork point (merge-base base head), the commit count (rev-list base..head) and the diff.
A replace ref that swaps the head for a commit whose parent is the base would make the change read as everything
between the two trees, against the base itself; a forged commit-graph can invent a fork point for two unrelated
histories. Both reads go through gitops.real_history_run (GIT_NO_REPLACE_OBJECTS, an empty graft file, and
-c core.commitGraph=false). Kills: dropping the env from real_history_run reds the first row; dropping the -c
core.commitGraph=false reds the second."""
from __future__ import annotations

import pytest

from gr2.python_cli import review_diff
from gr2.python_cli.grip import GripReviewRefused

from tests.test_is_ancestor_commit_graph import _forge, _world
from tests.test_is_ancestor_replace import _git, _replace_with_fake_child, _setup


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
