"""feature_moved_before_push names the real feature branch in its next line, not a <branch> placeholder."""
from __future__ import annotations

import subprocess

import pytest

from gr2.python_cli import merge_gate, next_steps
from tests.test_review_merge_gate import record_set, _git_identity, check, git, merge, mains, slice2, world  # noqa: F401


@pytest.mark.parametrize("refused", ["feature_moved: remote refs/heads/feat is " + "b" * 40,
                                     "feature_moved_before_push: remote feat is " + "b" * 40])
def test_feature_moved_next_names_the_branch(refused):
    line = next_steps.merge_row(refused, workspace="/ws", review="gr:abc", remote="/r.git", path="/ws/m",
                                head="a" * 40, checks=("test",))
    assert "--at-remote-heads feat --" in line and "<branch>" not in line, line


@pytest.mark.parametrize("refused", ["feature_moved_later: remote refs/heads/feat is " + "b" * 40,
                                     "feature_movedx: remote feat is " + "b" * 40])
def test_unknown_feature_moved_code_does_not_name_a_branch(refused):
    # Only the two known codes carry a branch the gate measured; a code that merely starts like them gets no
    # feature_moved line at all, so no branch is read out of it and no <branch> placeholder is printed either.
    line = next_steps.merge_row(refused, workspace="/ws", review="gr:abc", remote="/r.git", path="/ws/m",
                                head="a" * 40, checks=("test",))
    assert line is None or ("--at-remote-heads" not in line and "<branch>" not in line), line


@pytest.mark.parametrize("refused", ["feature_moved_later: remote refs/heads/feat is " + "b" * 40,
                                     "feature_movedx: remote feat is " + "b" * 40])
def test_unknown_feature_moved_code_gets_the_generic_line(refused):
    # The same answer as a code that starts like nothing merge_row knows.
    kw = dict(workspace="/ws", review="gr:abc", remote="/r.git", path="/ws/m", head="a" * 40, checks=("test",))
    generic = next_steps.merge_row("zz_unknown_code: remote refs/heads/feat is " + "b" * 40, **kw)
    assert next_steps.merge_row(refused, **kw) == generic


def _move_on_commit_tree(monkeypatch, repo, remote):
    real_run = subprocess.run
    fired = {"n": 0}

    def run(cmd, *a, **kw):
        result = real_run(cmd, *a, **kw)
        if "commit-tree" in [str(c) for c in cmd] and not fired["n"]:
            fired["n"] = 1  # the build is done; the push loop has not re-read the feature yet
            (repo / "late.txt").write_text("late\n")
            git(repo, "add", "late.txt")
            git(repo, "commit", "-m", "late")
            git(repo, "push", remote, "feat")
        return result
    monkeypatch.setattr(merge_gate.subprocess, "run", run)


def test_feature_moved_before_push_with_nothing_landed_prints_the_branch(slice2, monkeypatch):
    record_set(slice2)
    alpha = slice2["members"]["alpha"]
    _move_on_commit_tree(monkeypatch, alpha["repo"], alpha["remote"])
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", required_approvals=0)
    rows = {r["key"]: r for r in receipt["members"]}
    assert rows["alpha"]["refused"].startswith("feature_moved_before_push"), receipt
    assert rows["alpha"]["final"] == "unmerged" and rows["beta"]["final"] == "unmerged", receipt
    line = rows["alpha"]["next"]
    assert "--at-remote-heads feat --" in line and "<branch>" not in line, line
