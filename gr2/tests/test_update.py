"""`gr2 update --into <branch>`: bring the pushed branch you are on up to a moved integration branch, no pin needed.

Real git, a bare local remote, no network. The clone path has a space in it.
"""
from __future__ import annotations

import json
import os
import subprocess

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app

runner = CliRunner()


def git(root, *args, check=True):
    p = subprocess.run(["git", *map(str, args)], cwd=root, capture_output=True, text=True)
    assert p.returncode == 0 or not check, p.stderr
    return p.stdout.strip()


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k, v in (("GIT_AUTHOR_NAME", "Fixture"), ("GIT_AUTHOR_EMAIL", "fixture@example.invalid"),
                 ("GIT_COMMITTER_NAME", "Fixture"), ("GIT_COMMITTER_EMAIL", "fixture@example.invalid"),
                 ("GIT_CONFIG_NOSYSTEM", "1"), ("GIT_TERMINAL_PROMPT", "0")):
        monkeypatch.setenv(k, v)


def commit(repo, name, text):
    (repo / name).write_text(text)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", f"edit {name}")


@pytest.fixture
def lane(tmp_path):
    """A clone on a pushed feature branch, with dev moved two commits past its fork point by someone else."""
    remote, clone, other = tmp_path / "r.git", tmp_path / "my lane", tmp_path / "other"
    git(tmp_path, "init", "-q", "--bare", "-b", "dev", remote)
    git(tmp_path, "clone", "-q", remote, other)
    git(other, "checkout", "-q", "-b", "dev")
    commit(other, "shared.txt", "one\n")
    git(other, "push", "-q", "origin", "dev")
    git(tmp_path, "clone", "-q", "-b", "dev", remote, clone)
    git(clone, "checkout", "-q", "-b", "feat")
    commit(clone, "mine.txt", "mine\n")
    git(clone, "push", "-q", "-u", "origin", "feat")
    commit(other, "a.txt", "a\n")
    commit(other, "b.txt", "b\n")
    git(other, "push", "-q", "origin", "dev")
    return {"clone": clone, "other": other, "remote": remote, "dev": git(other, "rev-parse", "HEAD"),
            "head": git(clone, "rev-parse", "HEAD")}


def update(clone, *extra):
    return runner.invoke(app, ["update", "--into", "dev", "--repo-path", str(clone), *extra])


def test_lane_two_commits_behind_dev_comes_level_with_one_merge(lane):
    clone = lane["clone"]
    res = update(clone)
    assert res.exit_code == 0, res.output
    receipt = json.loads(res.stdout)
    assert receipt["state"] == "merged" and receipt["into"] == "dev" and receipt["base"] == lane["dev"]
    head = git(clone, "rev-parse", "HEAD")
    assert receipt["head"] == head
    # A merge, not a rebase: two parents, the old head first, dev's tip second; nothing pushed.
    assert git(clone, "rev-parse", "HEAD^1", "HEAD^2").split() == [lane["head"], lane["dev"]]
    assert git(clone, "rev-list", "--count", "HEAD..origin/dev") == "0"
    assert git(lane["remote"], "rev-parse", "refs/heads/feat") == lane["head"]
    assert git(clone, "status", "--porcelain") == ""


def test_already_level_is_done_and_makes_no_commit(lane):
    clone = lane["clone"]
    assert update(clone).exit_code == 0
    git(clone, "push", "-q", "origin", "feat")
    head = git(clone, "rev-parse", "HEAD")
    res = update(clone)
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["state"] == "up_to_date"
    assert git(clone, "rev-parse", "HEAD") == head


def test_conflict_refuses_and_leaves_the_tree_as_it_was(lane):
    clone, other = lane["clone"], lane["other"]
    commit(clone, "shared.txt", "mine\n")
    git(clone, "push", "-q", "origin", "feat")
    commit(other, "shared.txt", "theirs\n")
    git(other, "push", "-q", "origin", "dev")
    head = git(clone, "rev-parse", "HEAD")
    res = update(clone)
    assert res.exit_code == 3, res.output
    receipt = json.loads(res.stdout)
    assert receipt["refused"].startswith("conflict") and receipt["conflicts"] == ["shared.txt"]
    assert git(clone, "rev-parse", "HEAD") == head
    assert git(clone, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) == ""
    assert git(clone, "status", "--porcelain") == ""
    assert (clone / "shared.txt").read_text() == "mine\n"


def test_dirty_tree_refuses_before_anything_moves(lane):
    clone = lane["clone"]
    (clone / "mine.txt").write_text("half done\n")
    res = update(clone)
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["refused"].startswith("dirty")
    assert git(clone, "rev-parse", "HEAD") == lane["head"]
    assert (clone / "mine.txt").read_text() == "half done\n"


def test_unpushed_commits_refuse(lane):
    clone = lane["clone"]
    commit(clone, "late.txt", "late\n")
    head = git(clone, "rev-parse", "HEAD")
    res = update(clone)
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout)["refused"].startswith("unpushed")
    assert git(clone, "rev-parse", "HEAD") == head


def refused(res):
    assert res.exit_code == 2, res.output
    return json.loads(res.stdout)["refused"]


def test_behind_remote_refuses_when_someone_else_pushed_to_the_branch(lane):
    clone, other = lane["clone"], lane["other"]
    git(other, "fetch", "-q", "origin", "feat")
    git(other, "checkout", "-q", "-b", "feat", "FETCH_HEAD")
    commit(other, "theirs.txt", "theirs\n")
    git(other, "push", "-q", "origin", "feat")
    assert refused(update(clone)).startswith("behind_remote")
    assert git(clone, "rev-parse", "HEAD") == lane["head"]
    assert git(clone, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) == ""


def test_same_branch_refuses(lane):
    clone = lane["clone"]
    git(clone, "checkout", "-q", "dev")
    head = git(clone, "rev-parse", "HEAD")
    assert refused(update(clone)).startswith("same_branch")
    assert git(clone, "rev-parse", "HEAD") == head


def test_detached_head_refuses(lane):
    clone = lane["clone"]
    git(clone, "checkout", "-q", "--detach")
    assert refused(update(clone)).startswith("detached_head")
    assert git(clone, "rev-parse", "HEAD") == lane["head"]


def test_merge_in_progress_refuses(lane):
    clone, other = lane["clone"], lane["other"]
    commit(clone, "shared.txt", "mine\n")
    git(clone, "push", "-q", "origin", "feat")
    commit(other, "shared.txt", "theirs\n")
    git(other, "push", "-q", "origin", "dev")
    git(clone, "fetch", "-q", "origin")
    git(clone, "merge", "-q", "origin/dev", check=False)
    merging = git(clone, "rev-parse", "MERGE_HEAD")
    assert refused(update(clone)).startswith("unresolved_merge")
    assert git(clone, "rev-parse", "MERGE_HEAD") == merging


def test_untracked_file_the_merge_would_overwrite_refuses(lane):
    clone = lane["clone"]
    (clone / "a.txt").write_text("my own a\n")
    assert refused(update(clone)).startswith("untracked_in_the_way")
    assert git(clone, "rev-parse", "HEAD") == lane["head"]
    assert (clone / "a.txt").read_text() == "my own a\n"


def test_missing_integration_branch_refuses(lane):
    clone = lane["clone"]
    res = runner.invoke(app, ["update", "--into", "no-such-branch", "--repo-path", str(clone)])
    assert refused(res).startswith("fetch_failed")
    assert git(clone, "rev-parse", "HEAD") == lane["head"]


def test_pushed_means_the_branch_of_the_same_name_not_the_upstream(lane):
    # Renamed locally, still tracking the pushed branch: no branch of this name is on the remote, so it refuses.
    clone = lane["clone"]
    git(clone, "branch", "-q", "-m", "work")
    assert git(clone, "rev-parse", "--abbrev-ref", "@{upstream}") == "origin/feat"
    assert refused(update(clone)).startswith("unpushed: origin work")
    assert git(clone, "rev-parse", "HEAD") == lane["head"]


def test_help_says_which_remote_branch_counts_as_pushed():
    import typer.main

    text = " ".join(typer.main.get_command(app).commands["update"].help.split())
    assert "the branch of the same name on --remote" in text


def test_merges_even_when_git_is_set_to_fast_forward_only(lane):
    clone = lane["clone"]
    git(clone, "config", "merge.ff", "only")
    res = update(clone)
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["state"] == "merged"
    assert git(clone, "rev-parse", "HEAD^1", "HEAD^2").split() == [lane["head"], lane["dev"]]


def test_a_branch_dev_already_contains_gets_a_merge_not_a_fast_forward(lane):
    clone, other = lane["clone"], lane["other"]
    git(other, "fetch", "-q", "origin", "feat")
    git(other, "merge", "-q", "--no-edit", "FETCH_HEAD")
    git(other, "push", "-q", "origin", "dev")
    dev = git(other, "rev-parse", "HEAD")
    res = update(clone)
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["state"] == "merged"
    assert git(clone, "rev-parse", "HEAD") != dev
    assert git(clone, "rev-parse", "HEAD^1", "HEAD^2").split() == [lane["head"], dev]


def test_a_merge_git_cannot_commit_is_merge_failed_and_leaves_the_branch(lane, monkeypatch):
    # No committer identity anywhere: git refuses to write the merge commit.
    clone = lane["clone"]
    for k in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.delenv(k)
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", os.devnull)
    git(clone, "config", "user.useConfigOnly", "true")
    assert refused(update(clone)).startswith("merge_failed")
    assert git(clone, "rev-parse", "HEAD") == lane["head"]
    assert git(clone, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) == ""
    assert git(clone, "status", "--porcelain") == ""


def test_help_and_docstring_name_every_refusal():
    import typer.main

    from gr2.python_cli import branch_update

    help_text = " ".join(typer.main.get_command(app).commands["update"].help.split())
    doc = " ".join(branch_update.__doc__.split())
    for name in ("detached_head", "same_branch", "unresolved_merge", "dirty", "unpushed", "behind_remote",
                 "fetch_failed", "untracked_in_the_way", "merge_failed", "bad_into", "bad_remote"):
        assert name in help_text and name in doc, name


def refs(clone):
    return git(clone, "for-each-ref", "--format=%(refname) %(objectname)")


@pytest.mark.parametrize("into", ["dev:refs/heads/keep", "dev:keep", "+dev:keep"])
def test_an_into_with_a_write_side_refuses_and_writes_no_ref(lane, into):
    # `keep` sits at the fork point, so a fetch into it would fast-forward and move a branch nobody named.
    clone = lane["clone"]
    git(clone, "branch", "keep", "HEAD~1")
    before = refs(clone)
    res = runner.invoke(app, ["update", "--into", into, "--repo-path", str(clone)])
    assert res.exit_code == 2, res.output
    assert refused(res).startswith("bad_into")
    assert refs(clone) == before
    assert git(clone, "rev-parse", "HEAD") == lane["head"]


@pytest.mark.parametrize("into", ["-dev", "--upload-pack=touch x", "+dev", "dev*", "de^v", "de~v", "de?v", "de[v",
                                  "de\\v", "de v", "de\tv", "de..v", "dev@{1}", "@{-1}", "dev.lock", "dev/", ""])
def test_an_into_that_is_not_a_plain_branch_name_refuses_before_any_git_call(lane, into, monkeypatch):
    from gr2.python_cli import gitops

    calls = []
    real = gitops.run
    monkeypatch.setattr(gitops, "run", lambda *a, **k: calls.append(a) or real(*a, **k))
    clone = lane["clone"]
    before = refs(clone)
    res = runner.invoke(app, ["update", "--into", into, "--repo-path", str(clone)])
    assert res.exit_code == 2, res.output
    assert refused(res).startswith("bad_into")
    assert all(a[1] == "check-ref-format" for a in calls), calls
    assert refs(clone) == before


def test_a_slashed_branch_name_is_allowed(lane):
    clone, other = lane["clone"], lane["other"]
    git(other, "push", "-q", "origin", "dev:refs/heads/release/1.x")
    res = runner.invoke(app, ["update", "--into", "release/1.x", "--repo-path", str(clone)])
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["state"] == "merged"
    assert git(clone, "rev-parse", "HEAD^1", "HEAD^2").split() == [lane["head"], lane["dev"]]


def test_a_configured_fetch_refspec_onto_a_local_branch_moves_nothing(lane):
    # A plain --into is not enough: git's opportunistic refmap would write `keep` from remote.origin.fetch.
    clone = lane["clone"]
    git(clone, "branch", "keep", "HEAD~1")
    keep = git(clone, "rev-parse", "keep")
    git(clone, "config", "--add", "remote.origin.fetch", "+refs/heads/dev:refs/heads/keep")
    res = update(clone)
    assert res.exit_code == 0, res.output
    assert git(clone, "rev-parse", "keep") == keep


def test_a_refusal_after_the_fetch_writes_no_ref(lane):
    clone, other = lane["clone"], lane["other"]
    git(other, "fetch", "-q", "origin", "feat")
    git(other, "checkout", "-q", "-b", "feat", "FETCH_HEAD")
    commit(other, "theirs.txt", "theirs\n")
    git(other, "push", "-q", "origin", "feat")
    before = refs(clone)
    assert refused(update(clone)).startswith("behind_remote")
    assert refs(clone) == before


def test_a_configured_glob_refspec_onto_local_branches_moves_nothing(lane):
    # Without --refmap= this refspec would have the fetch write the checked-out branch and local dev.
    clone = lane["clone"]
    git(clone, "config", "--add", "remote.origin.fetch", "+refs/heads/*:refs/heads/*")
    dev = git(clone, "rev-parse", "refs/heads/dev")
    res = update(clone)
    assert res.exit_code == 0, res.output
    assert json.loads(res.stdout)["state"] == "merged"
    assert git(clone, "rev-parse", "refs/heads/dev") == dev
    assert git(clone, "rev-parse", "HEAD^1", "HEAD^2").split() == [lane["head"], lane["dev"]]


@pytest.mark.parametrize("into", ["-dev", "dev.lock", "de v", "dev:keep", "de..v"])
def test_the_name_check_refuses_on_its_own_without_check_ref_format(lane, into, monkeypatch):
    # check-ref-format refuses these too; each layer must stand alone, so here it accepts everything.
    from gr2.python_cli import gitops

    real = gitops.run
    ok = subprocess.CompletedProcess([], 0, "", "")
    monkeypatch.setattr(gitops, "run", lambda repo, *a, **k: ok if a[0] == "check-ref-format" else real(repo, *a, **k))
    clone = lane["clone"]
    before = refs(clone)
    res = runner.invoke(app, ["update", "--into", into, "--repo-path", str(clone)])
    assert res.exit_code == 2, res.output
    assert refused(res).startswith("bad_into")
    assert refs(clone) == before


def test_remote_tag_opt_tags_writes_no_tag(lane):
    clone = lane["clone"]
    git(lane["other"], "tag", "v-new")
    git(lane["other"], "push", "-q", "origin", "v-new")
    git(clone, "config", "remote.origin.tagOpt", "--tags")
    before = refs(clone)
    res = update(clone)
    assert res.exit_code == 0, res.output
    assert git(clone, "tag", "--list") == ""
    assert [r for r in refs(clone).splitlines() if not r.startswith("refs/heads/feat ")] == \
        [r for r in before.splitlines() if not r.startswith("refs/heads/feat ")]


@pytest.mark.parametrize("remote", [".", "path-of-origin", "--upload-pack=true"])
def test_a_remote_that_is_not_configured_refuses(lane, remote):
    # `.` would read this repository's own branch as the pushed one: an unpushed commit would pass.
    clone = lane["clone"]
    commit(clone, "unpushed.txt", "u\n")
    head = git(clone, "rev-parse", "HEAD")
    res = update(clone, "--remote", str(lane["remote"]) if remote == "path-of-origin" else remote)
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout.splitlines()[0])["refused"].startswith("bad_remote:")
    assert git(clone, "rev-parse", "HEAD") == head


def test_a_tag_named_like_the_branch_still_counts_as_pushed(lane):
    clone = lane["clone"]
    git(clone, "tag", "feat")
    res = update(clone)
    assert res.exit_code == 0, res.output
    assert git(clone, "merge-base", "--is-ancestor", lane["dev"], "HEAD", check=False) == ""


@pytest.mark.parametrize("hook", ["pre-merge-commit", "commit-msg"])
def test_a_refusing_merge_hook_is_merge_failed_not_a_conflict(lane, hook):
    clone = lane["clone"]
    h = clone / ".git" / "hooks" / hook
    h.write_text("#!/bin/sh\nexit 1\n")
    h.chmod(0o755)
    res = update(clone)
    assert res.exit_code == 2, res.output
    assert json.loads(res.stdout.splitlines()[0])["refused"].startswith("merge_failed")
    assert git(clone, "rev-parse", "HEAD") == lane["head"]
    assert git(clone, "rev-parse", "-q", "--verify", "MERGE_HEAD", check=False) == ""
    assert git(clone, "status", "--porcelain") == ""
