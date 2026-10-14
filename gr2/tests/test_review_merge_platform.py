"""`review merge` lands a member through its hosting platform when the platform has a PR whose head is the pinned head.

Measured on GitHub stacks (two private scratch repos, merge-async on the stack's top PR): a member can be refused
before anything merges, and a commit pushed to a PR's branch while the land runs is left out of the merge under a PR
that reads "merged". The fake platform here merges on the bare remote the way that measurement did: one merge
commit of the target and the pinned head.
"""
from __future__ import annotations

import pytest

from gr2.python_cli import merge_gate
from tests.test_review_merge_gate import git, mains, slice2  # noqa: F401  (slice2 is a fixture)


class FakePlatform:
    """One open PR per member remote, head = the member's pinned head, base = main. Merges like merge-async."""

    def __init__(self, members):
        self.prs = {str(m["remote"]): {"number": 2, "state": "open", "draft": False, "head": m["head"], "base": "main"}
                    for m in members.values()}
        self.remotes = {str(m["remote"]): m for m in members.values()}
        self.merges: list[str] = []
        self.after_merge = None  # test hook: what happens on the platform right after a merge

    def pr_for_head(self, repo, sha):
        pr = self.prs[repo]
        return [dict(pr)] if pr["head"] == sha and pr["state"] == "open" else []

    def pr_for_branch(self, repo, branch):
        return []  # one-PR stacks: the top PR's base is the target

    def pr_head(self, repo, number):
        return dict(self.prs[repo])

    def merge_pr_async(self, repo, number, sha, method="merge"):
        remote = self.remotes[repo]["remote"]
        tree = git(remote, "merge-tree", "--write-tree", "main", sha).split()[0]
        merged = git(remote, "commit-tree", tree, "-p", git(remote, "rev-parse", "main"), "-p", sha, "-m", f"Merge PR #{number}")
        git(remote, "update-ref", "refs/heads/main", merged)
        self.merges.append(repo)
        self.prs[repo]["state"] = "merged"
        if self.after_merge:
            self.after_merge(repo)
        return {"status": "merged", "sha": merged}


@pytest.fixture
def platform(slice2, monkeypatch):  # noqa: F811
    fake = FakePlatform(slice2["members"])
    monkeypatch.setattr(merge_gate, "platform_for_remote",
                        lambda remote: (fake, remote) if remote in fake.prs else None)
    return fake


def test_both_platform_members_land_through_the_platform_in_order(slice2, platform):  # noqa: F811
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", no_set=True)
    assert code == merge_gate.EXIT_MERGED, receipt
    assert platform.merges == [str(slice2["members"]["alpha"]["remote"]), str(slice2["members"]["beta"]["remote"])]
    assert [r["merged_by"] for r in receipt["members"]] == ["platform", "platform"], receipt


def test_a_member_whose_pr_is_not_ready_refuses_before_any_merge(slice2, platform):  # noqa: F811
    platform.prs[str(slice2["members"]["beta"]["remote"])]["draft"] = True
    before = mains(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", no_set=True)
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert platform.merges == [], "nothing may merge while one member's PR is not ready"
    assert mains(slice2) == before
    beta = next(r for r in receipt["members"] if r["key"] == "beta")
    assert beta["refusal"]["code"] == "platform_pr_not_ready", beta


def test_a_commit_pushed_to_the_pr_during_the_land_is_named_as_stranded_and_stops_the_land(slice2, platform):  # noqa: F811
    alpha = slice2["members"]["alpha"]
    late = {}

    def push_late(repo):
        # GitHub merged the pinned head, then the PR's head ref moved: the late commit is not in the merge.
        if repo == str(alpha["remote"]):
            (alpha["repo"] / "late.txt").write_text("pushed during the land\n")
            git(alpha["repo"], "add", "late.txt")
            git(alpha["repo"], "commit", "-m", "late")
            late["sha"] = git(alpha["repo"], "rev-parse", "HEAD")
            git(alpha["repo"], "push", alpha["remote"], "feat")
            platform.prs[repo]["head"] = late["sha"]

    platform.after_merge = push_late
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat", no_set=True)
    assert code == merge_gate.EXIT_PARTIAL, receipt
    assert platform.merges == [str(alpha["remote"])], "no member may land after a stranded one"
    row = next(r for r in receipt["members"] if r["key"] == "alpha")
    assert row["refusal"]["code"] == "platform_head_moved_after_merge", row
    assert row["stranded"] == late["sha"] and late["sha"] in row["refused"], row
    assert row.get("next"), "a stranded commit names the next step"
