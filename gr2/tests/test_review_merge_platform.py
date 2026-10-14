"""`review merge` lands a member through its hosting platform when the platform has a PR whose head is the pinned head.

Measured on GitHub stacks (two private scratch repos, merge-async on the stack's top PR): a member can be refused
before anything merges, and a commit pushed to a PR's branch while the land runs is left out of the merge under a PR
that reads "merged". The fake platform here merges on the bare remote the way that measurement did: one merge
commit of the target and the pinned head.
"""
from __future__ import annotations

import pytest

from gr2.python_cli import merge_gate
from tests.test_review_merge_gate import git, mains, record_set, slice2  # noqa: F401  (slice2 is a fixture)


class FakePlatform:
    """One open PR per member remote, head = the member's pinned head, base = main. Merges like merge-async."""

    def __init__(self, members):
        self.prs = {str(m["remote"]): {"number": 2, "state": "open", "draft": False, "head": m["head"], "base": "main"}
                    for m in members.values()}
        self.remotes = {str(m["remote"]): m for m in members.values()}
        self.merges: list[str] = []
        self.after_merge = None  # test hook: what happens on the platform right after a merge
        self.also_at_head: dict[str, list[dict]] = {}  # test hook: more open PRs whose head is the same commit
        self.below: dict[tuple[str, str], list[dict]] = {}  # test hook: (repo, branch) -> open PRs with that head branch

    def pr_for_head(self, repo, sha):
        pr = self.prs[repo]
        mine = [dict(pr)] if pr["head"] == sha and pr["state"] == "open" else []
        return mine + [dict(p) for p in self.also_at_head.get(repo, [])]

    def pr_for_branch(self, repo, branch):
        return [dict(p) for p in self.below.get((repo, branch), [])]  # default: one-PR stacks

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
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_MERGED, receipt
    assert platform.merges == [str(slice2["members"]["alpha"]["remote"]), str(slice2["members"]["beta"]["remote"])]
    assert [r["merged_by"] for r in receipt["members"]] == ["platform", "platform"], receipt


def test_a_member_whose_pr_is_not_ready_refuses_before_any_merge(slice2, platform):  # noqa: F811
    platform.prs[str(slice2["members"]["beta"]["remote"])]["draft"] = True
    before = mains(slice2)
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
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
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_PARTIAL, receipt
    assert platform.merges == [str(alpha["remote"])], "no member may land after a stranded one"
    row = next(r for r in receipt["members"] if r["key"] == "alpha")
    assert row["refusal"]["code"] == "platform_head_moved_after_merge", row
    assert row["stranded"] == late["sha"] and late["sha"] in row["refused"], row
    assert row.get("next"), "a stranded commit names the next step"


def test_a_version_2_adapter_reads_its_own_urls_when_selecting_a_platform_member(monkeypatch):
    from gr2.python_cli import platform as platform_ops

    class V2(FakePlatform):
        platform_adapter_api_version = 2

        def __init__(self):
            self.read = []

        def resolve_target(self, remote):
            self.read.append(remote)
            return platform_ops.RemoteTarget(raw=remote, host="git.example", org="acme", project=None, repo="api")

    adapter = V2()
    monkeypatch.setattr(platform_ops, "get_platform_adapter", lambda name: adapter)
    assert merge_gate.platform_for_remote("https://git.example/acme/api.git") == (adapter, "acme/api")
    assert adapter.read == ["https://git.example/acme/api.git"], "the adapter, not gr2, read the URL"


def test_a_refused_platform_merge_with_a_moved_head_is_not_called_stranded(slice2, platform):  # noqa: F811
    # GitHub may refuse a merge whose head moved; then nothing landed and nothing was stranded by a merge.
    alpha = str(slice2["members"]["alpha"]["remote"])
    before = mains(slice2)

    def refuse(repo, number, sha, method="merge"):
        platform.prs[repo]["head"] = "f" * 40
        return {"status": "failed", "sha": None, "message": "Head branch was modified"}

    platform.merge_pr_async = refuse
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert mains(slice2) == before
    row = next(r for r in receipt["members"] if r["key"] == "alpha")
    assert row["refusal"]["code"] == "platform_merge_not_confirmed", row
    assert not row.get("stranded") and "holds the pinned head" not in row["refused"], row
    assert "f" * 40 in row["refused"], "the moved head is still named"
    assert code == merge_gate.EXIT_REFUSED, receipt


def test_two_open_prs_at_the_pinned_head_refuse_as_ambiguous_before_any_merge(slice2, platform):  # noqa: F811
    beta = str(slice2["members"]["beta"]["remote"])
    platform.also_at_head[beta] = [{"number": 7, "state": "open", "draft": False, "head": platform.prs[beta]["head"],
                                    "base": "main"}]
    before = mains(slice2)
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert platform.merges == [] and mains(slice2) == before
    row = next(r for r in receipt["members"] if r["key"] == "beta")
    assert row["refusal"]["code"] == "platform_pr_ambiguous", row
    assert "#2" in row["refused"] and "#7" in row["refused"], row


def test_a_draft_lower_in_the_stack_refuses_before_any_merge(slice2, platform):  # noqa: F811
    # beta's PR #2 targets feat-base, whose own PR #3 targets main and is a draft: the stack is followed down to main.
    beta = str(slice2["members"]["beta"]["remote"])
    platform.prs[beta]["base"] = "feat-base"
    platform.below[(beta, "feat-base")] = [{"number": 3, "state": "open", "draft": True, "head": "e" * 40, "base": "main"}]
    before = mains(slice2)
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert platform.merges == [] and mains(slice2) == before
    row = next(r for r in receipt["members"] if r["key"] == "beta")
    assert row["refusal"]["code"] == "platform_pr_not_ready" and "#3" in row["refused"], row


def test_a_stack_that_does_not_reach_the_target_refuses_before_any_merge(slice2, platform):  # noqa: F811
    beta = str(slice2["members"]["beta"]["remote"])
    platform.prs[beta]["base"] = "feat-base"  # and no open PR has feat-base as its head
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert platform.merges == []
    row = next(r for r in receipt["members"] if r["key"] == "beta")
    assert row["refusal"]["code"] == "platform_stack_unresolved" and "feat-base" in row["refused"], row


def test_a_stranded_commit_on_the_last_member_is_partial_although_every_member_reads_merged(slice2, platform):  # noqa: F811
    beta = slice2["members"]["beta"]

    def push_late(repo):
        if repo == str(beta["remote"]):
            (beta["repo"] / "late.txt").write_text("pushed during the land\n")
            git(beta["repo"], "add", "late.txt")
            git(beta["repo"], "commit", "-m", "late")
            git(beta["repo"], "push", beta["remote"], "feat")
            platform.prs[repo]["head"] = git(beta["repo"], "rev-parse", "HEAD")

    platform.after_merge = push_late
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert len(platform.merges) == 2, "both members landed; the late commit came after beta's merge"
    row = next(r for r in receipt["members"] if r["key"] == "beta")
    assert row["refusal"]["code"] == "platform_head_moved_after_merge", row
    assert code == merge_gate.EXIT_PARTIAL, receipt


def test_a_platform_read_that_raises_refuses_unmeasurable_and_merges_nothing(slice2, platform):  # noqa: F811
    """A failed platform read must refuse, never fall through to a push past an open PR."""
    def boom(repo, sha):
        raise RuntimeError("gh: not authenticated")
    platform.pr_for_head = boom
    before = mains(slice2)
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert platform.merges == []
    assert mains(slice2) == before
    codes = {r["key"]: (r.get("refusal") or {}).get("code") for r in receipt["members"]}
    assert set(codes.values()) == {"platform_unmeasurable"}, codes


def _gh_absent(monkeypatch):
    """No `gh` on this machine: the real GitHubAdapter() raises, exactly as it does when gh is not installed."""
    from gr2.python_cli import platform as platform_ops
    real_which = platform_ops.shutil.which
    monkeypatch.setattr(platform_ops.shutil, "which", lambda name, *a, **k: None if name == "gh" else real_which(name, *a, **k))


@pytest.mark.parametrize("url", ["https://github.com/o/r.git", "git@github.com:o/r.git", "ssh://git@github.com/o/r"])
def test_a_github_url_with_gh_absent_is_unavailable_not_a_plain_git_member(monkeypatch, url):
    _gh_absent(monkeypatch)
    with pytest.raises(merge_gate.PlatformUnavailable, match="no GitHub adapter"):
        merge_gate.platform_for_remote(url)
    assert merge_gate.platform_for_remote("/srv/git/r.git") is None  # control: a plain-Git remote stays plain Git


def test_gh_absent_on_a_github_member_refuses_unmeasurable_names_gh_and_merges_nothing(slice2, monkeypatch):  # noqa: F811
    """With gh absent, a member on GitHub must not be merged by push past its PR stack; whether its PRs are honoured must not depend on what is installed."""
    _gh_absent(monkeypatch)
    real = merge_gate.platform_for_remote
    alpha = str(slice2["members"]["alpha"]["remote"])
    monkeypatch.setattr(merge_gate, "platform_for_remote",
                        lambda remote: real("https://github.com/o/alpha.git" if remote == alpha else remote))
    before = mains(slice2)
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == before
    row = next(r for r in receipt["members"] if r["key"] == "alpha")
    assert row["refusal"]["code"] == "platform_unmeasurable", row
    assert "gh" in row["next"] and "auth" in row["next"], row


def test_control_gh_absent_on_plain_git_members_still_merges_by_push(slice2, monkeypatch):  # noqa: F811
    _gh_absent(monkeypatch)
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_MERGED, receipt
    assert all(m["merged_by"] != "platform" for m in receipt["members"]), receipt


# GitHub URL spellings that gr2's own reader (review.canonical_source_identity) calls GitHub, beyond the plain three.
_GITHUB_SPELLINGS = ["https://x-access-token:TOK@github.com/o/alpha.git", "https://user@github.com/o/alpha",
                     "http://github.com/o/alpha.git"]


@pytest.mark.parametrize("url", _GITHUB_SPELLINGS)
def test_gh_absent_on_a_github_member_with_credentials_a_user_or_http_refuses_and_merges_nothing(slice2, monkeypatch, url):  # noqa: F811
    _gh_absent(monkeypatch)
    real = merge_gate.platform_for_remote
    alpha = str(slice2["members"]["alpha"]["remote"])
    monkeypatch.setattr(merge_gate, "platform_for_remote", lambda remote: real(url if remote == alpha else remote))
    before = mains(slice2)
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_REFUSED, receipt
    assert mains(slice2) == before
    row = next(r for r in receipt["members"] if r["key"] == "alpha")
    assert row["refusal"]["code"] == "platform_unmeasurable", row


@pytest.mark.parametrize("url", _GITHUB_SPELLINGS)
def test_gh_present_a_github_member_with_credentials_a_user_or_http_lands_through_its_pr(slice2, monkeypatch, url):  # noqa: F811
    """With a working adapter, every GitHub spelling lands through the PR at its head; none is pushed past it."""
    from gr2.python_cli import platform as platform_ops
    alpha = str(slice2["members"]["alpha"]["remote"])
    fake = FakePlatform(slice2["members"])
    fake.prs["o/alpha"], fake.remotes["o/alpha"] = fake.prs[alpha], fake.remotes[alpha]  # the PR, keyed as GitHub names it
    monkeypatch.setattr(platform_ops, "get_platform_adapter", lambda name: fake)
    real = merge_gate.platform_for_remote
    monkeypatch.setattr(merge_gate, "platform_for_remote", lambda remote: real(url if remote == alpha else remote))
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert code == merge_gate.EXIT_MERGED, receipt
    assert fake.merges == ["o/alpha"], fake.merges
    by = {m["key"]: m["merged_by"] for m in receipt["members"]}
    assert by["alpha"] == "platform" and by["beta"] != "platform", by


def test_a_pr_head_read_that_fails_after_the_last_merge_is_partial_and_strands_nothing(slice2, platform):  # noqa: F811
    """The merge holds the pinned head, but whether a commit was pushed during the land is unknown: exit 4, its own
    code, and no commit named as stranded."""
    beta = str(slice2["members"]["beta"]["remote"])
    read = platform.pr_head

    def flaky(repo, number):
        if repo == beta and platform.prs[repo]["state"] == "merged":
            raise RuntimeError("gh: HTTP 502")
        return read(repo, number)

    platform.pr_head = flaky
    record_set(slice2)
    code, receipt = merge_gate.review_merge(slice2["author"], slice2["review"], feature="feat")
    assert len(platform.merges) == 2, platform.merges
    assert code == merge_gate.EXIT_PARTIAL, receipt
    row = next(r for r in receipt["members"] if r["key"] == "beta")
    assert row["refusal"]["code"] == "platform_head_unmeasured_after_merge", row
    assert not row.get("stranded"), row
    assert "next" in row and "review merge" in row["next"], row
