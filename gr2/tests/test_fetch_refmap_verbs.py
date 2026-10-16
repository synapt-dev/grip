"""A configured `remote.<name>.fetch` refspec, or `remote.<name>.tagOpt=--tags`, writes nothing on a verb's fetch that
names its own refspec.

git-fetch(1), CONFIGURED REMOTE-TRACKING BRANCHES: a fetch by remote name with an explicit refspec also updates every
ref the configured refspecs map the fetched refs to, unless `--refmap=` is passed; `tagOpt=--tags` fetches every tag
unless `--no-tags` is passed. Each row configures `+<fetched ref>:refs/heads/keep` and `tagOpt=--tags` with a tag on
the remote, runs one verb's fetch, and asserts `keep` did not move and no tag arrived.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import approvals, gitops, grip, push


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), "-c", "user.name=t", "-c", "user.email=t@t", *args],
                          check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def clone(tmp_path):
    remote = tmp_path / "remote.git"
    work = tmp_path / "work"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "dev", str(remote)], check=True)
    subprocess.run(["git", "init", "-q", "-b", "dev", str(work)], check=True)
    for n in ("one", "two"):
        (work / n).write_text(n)
        git(work, "add", n)
        git(work, "commit", "-qm", n)
    git(work, "remote", "add", "origin", str(remote))
    git(work, "push", "-q", "origin", "dev")
    git(work, "branch", "keep", "HEAD~1")
    git(remote, "tag", "t1", "dev")
    return {"repo": work, "remote": remote, "keep": git(work, "rev-parse", "keep"), "tip": git(work, "rev-parse", "dev")}


def configure(repo: Path, src: str) -> None:
    git(repo, "config", "--replace-all", "remote.origin.fetch", f"+{src}:refs/heads/keep")
    git(repo, "config", "remote.origin.tagOpt", "--tags")


def untouched(c) -> bool:
    return git(c["repo"], "rev-parse", "keep") == c["keep"] and git(c["repo"], "tag", "-l") == ""


def advance(c, branch: str = "dev") -> str:
    """Another clone pushes past `branch` at the remote; this clone does not have the new tip."""
    other = c["remote"].parent / "other"
    subprocess.run(["git", "clone", "-q", "--branch", branch, str(c["remote"]), str(other)], check=True)
    (other / "three").write_text("three")
    git(other, "add", "three")
    git(other, "commit", "-qm", "three")
    git(other, "push", "-q", "origin", branch)
    return git(other, "rev-parse", "HEAD")


def test_approvals_remote_tip_fetch_writes_nothing(clone):
    ref = approvals.PREFIX + "r1"
    git(clone["repo"], "push", "-q", "origin", f"dev:{ref}")
    configure(clone["repo"], ref)
    tips = approvals._remote_tips(clone["repo"], {"review_id": "gr:r1", "members": [{"remote": "origin"}]})
    assert tips == {"origin": clone["tip"]}
    assert untouched(clone)


def test_push_lane_branch_fetch_writes_nothing(clone):
    tip = advance(clone)
    configure(clone["repo"], "refs/heads/dev")
    receipt = push.PushReceipt(remote="origin", branch="feat", local_sha=clone["tip"], remote_sha=clone["tip"],
                               set_upstream=False, force_with_lease=False)
    assert push._lane_branch_uncovered(clone["repo"], receipt, "dev") is None
    assert git(clone["repo"], "cat-file", "-t", tip) == "commit"
    assert untouched(clone)


def test_gitops_fetch_ref_writes_only_its_named_branch(clone):
    configure(clone["repo"], "refs/heads/dev")
    gitops.fetch_ref(clone["repo"], "origin", "refs/heads/dev", "lane")
    assert git(clone["repo"], "rev-parse", "lane") == clone["tip"]
    assert untouched(clone)


def test_gitops_refresh_existing_branch_writes_only_its_named_branch(clone):
    git(clone["repo"], "branch", "lane", "keep")
    tip = advance(clone)
    configure(clone["repo"], "refs/heads/dev")
    gitops.refresh_existing_branch(clone["repo"], "origin", "refs/heads/dev", "lane")
    assert git(clone["repo"], "rev-parse", "lane") == tip
    assert untouched(clone)


def test_grip_pin_source_remote_branch_fetch_writes_nothing(clone):
    tip = advance(clone)
    configure(clone["repo"], "refs/heads/dev")
    with pytest.raises(grip.GripReviewRefused):
        grip._judge_remote_branch(clone["repo"], "origin", str(clone["repo"]), clone["tip"], "refs/heads/main",
                                  {"refs/heads/dev": tip})
    assert untouched(clone)


def run_printed(command: str) -> None:
    import shlex
    subprocess.run(shlex.split(command), check=True, capture_output=True)


def test_merge_tracking_fallback_printed_for_an_unnamed_remote_writes_nothing(clone):
    # A row remote spelled as a name the clone's remotes do not match by URL prints a fetch by that name.
    from gr2.python_cli import merge_gate
    advance(clone)
    configure(clone["repo"], "refs/heads/dev")
    miss = merge_gate._refresh_tracking(clone["repo"], "origin", "dev")
    assert miss and miss["reason"] == "no remote of this clone names origin"
    run_printed(miss["command"])
    assert untouched(clone)


def test_merge_tracking_fallback_printed_after_a_failed_fetch_writes_nothing(clone, monkeypatch):
    from gr2.python_cli import merge_gate
    advance(clone)
    configure(clone["repo"], "refs/heads/dev")
    real = merge_gate.gitops.run
    monkeypatch.setattr(merge_gate.gitops, "run", lambda repo, *a, **k: subprocess.CompletedProcess(a, 1, "", "")
                        if a[0] == "fetch" else real(repo, *a, **k))
    miss = merge_gate._refresh_tracking(clone["repo"], str(clone["remote"]), "dev")
    assert miss and miss["reason"] == "the fetch failed"
    monkeypatch.undo()
    run_printed(miss["command"])
    assert git(clone["repo"], "rev-parse", "origin/dev") == git(clone["remote"], "rev-parse", "dev")
    assert untouched(clone)
