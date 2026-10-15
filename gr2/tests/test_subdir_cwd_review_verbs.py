"""Review verbs run from a subdirectory of a member repo, against a real pinned review.

git discovers the member repo from `<root>/member/sub`, so a verb that hands git the cwd would write
the review store, read the approver's name, or place the lane in the member. Each verb here must
answer for the workspace root above it (the lane above it, for `review run`), the same as from the root.

Premium boundary: OSS (grip). Local workspace orchestration over git; no identity, org, or
entitlement semantics.
"""
from __future__ import annotations

import sys

from typer.testing import CliRunner

from gr2.python_cli import review_run
from gr2.python_cli.app import app
from tests.review_ref_helper import REVIEW_REF_PREFIX
from tests.test_review_merge_gate import _git_identity, git, world  # noqa: F401  (fixtures)

runner = CliRunner()


def _sub(world, monkeypatch):
    sub = world["member"] / "sub"
    sub.mkdir()
    monkeypatch.chdir(sub)
    return sub


def _reviews(repo):
    return git(repo, "for-each-ref", "--format=%(refname)", REVIEW_REF_PREFIX).split()


def test_pin_stamp_publish_receive_from_a_member_subdirectory_write_the_root_store(world, monkeypatch, tmp_path):
    author, member, remote, review = world["author"], world["member"], str(world["remote"]), world["review"]
    _sub(world, monkeypatch)
    for verb in ("publish", "receive"):
        done = runner.invoke(app, ["review", verb, review, "--remote", remote])
        assert done.exit_code == 0, (verb, done.output)
    git(author, "config", "user.name", "Second Reader")
    git(member, "config", "user.name", "Member Config")
    for k in ("GIT_AUTHOR_NAME", "GIT_COMMITTER_NAME"):
        monkeypatch.delenv(k)
    stamped = runner.invoke(app, ["review", "stamp"])
    assert stamped.exit_code == 0, stamped.output
    assert '"approver": "Second Reader"' in stamped.output, stamped.output
    (member / "next.txt").write_text("next\n")
    git(member, "add", "next.txt")
    git(member, "commit", "-m", "next")
    pinned = runner.invoke(app, ["review", "pin", "--repo", "member", "--remote", remote, "--base", world["base"],
                                 "--head", git(member, "rev-parse", "HEAD"), "--ref", "refs/heads/main",
                                 "--source", str(member)])
    assert pinned.exit_code == 0, pinned.output
    new = pinned.output.strip().splitlines()[-1]
    assert new.startswith("gr:") and new != review, pinned.output
    assert len(_reviews(author)) == 2 and _reviews(member) == [], (_reviews(author), _reviews(member))


def test_open_from_a_member_subdirectory_places_the_lane_beside_the_root(world, monkeypatch):
    _sub(world, monkeypatch)
    opened = runner.invoke(app, ["review", "open"])
    assert opened.exit_code == 0, opened.output
    lane = world["author"].parent / "author.review" / world["review"][3:11]
    assert f"lane: {lane}" in opened.output and (lane / "payload.txt").read_text() == "reviewed\n", opened.output
    deep = lane / "deep"
    deep.mkdir()
    assert review_run.resolve_run_lane(None, cwd=deep) == lane


def test_check_run_and_merge_from_a_member_subdirectory_merge_the_reviewed_head(world, monkeypatch):
    _sub(world, monkeypatch)
    made = runner.invoke(app, ["check", "run", ".", "--", sys.executable, "-c", "pass"])
    assert made.exit_code == 0, made.output
    merged = runner.invoke(app, ["review", "merge", "--approvals", "0"])
    assert merged.exit_code == 0, merged.output
    assert f"review={world['review']} (pinned review at the current heads)" in merged.stderr
    tip = git(world["remote"], "rev-parse", "main")
    assert git(world["remote"], "rev-list", "--parents", "-n", "1", tip).split()[1:] == [world["base"], world["head"]]
