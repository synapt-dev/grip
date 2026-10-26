"""`gr2 review close --adopt`: closing a PR-head review lane an older gr2 made without a review record.

An older `review checkout-pr` materialized the lane but never wrote the (repo, base, head, lane_kind)
record, so the plain close refuses it. `--adopt` earns the record from what the lane itself shows (its
origin identity and its HEAD against the PR head the workspace fetched), then runs the normal close.
Plain close stays exactly as strict; a directory that is not that review lane is refused by --adopt.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app
from gr2.python_cli.review_records import review_record_paths


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True).stdout.strip()


@pytest.fixture
def world(tmp_path: Path):
    origin = tmp_path / "grip.git"
    _git(tmp_path, "init", "--bare", "-q", "-b", "main", str(origin))
    source = tmp_path / "grip"
    _git(tmp_path, "clone", "-q", str(origin), str(source))
    for key, value in (("user.email", "t@t"), ("user.name", "t")):
        _git(source, "config", key, value)
    (source / "README.md").write_text("seed\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "initial")
    _git(source, "push", "-q", "origin", "main")
    # The PR head as `review checkout-pr` fetched it: branch pr/7 in the member.
    _git(source, "checkout", "-q", "-b", "pr/7")
    (source / "pr.txt").write_text("pr work\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "pr work")
    head = _git(source, "rev-parse", "HEAD")
    _git(source, "checkout", "-q", "main")
    # The PR is merged: the member's checkout holds its head (a merge commit, so main is not the head).
    _git(source, "merge", "-q", "--no-ff", "-m", "merge pr/7", "pr/7")
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text(f'[[repos]]\nname = "grip"\npath = "{source}"\n')
    lane = ws / ".grip" / "state" / "lanes" / "reviewer" / "review-7" / "repos" / "grip"
    return {"origin": origin, "source": source, "head": head, "ws": ws, "lane": lane}


def _clone_lane(world, at: str) -> Path:
    """A lane as an older build left it: its own clone, origin the member's origin, no review record."""
    lane = world["lane"]
    lane.parent.mkdir(parents=True)
    _git(lane.parent, "clone", "-q", str(world["source"]), str(lane))
    _git(lane, "remote", "set-url", "origin", str(world["origin"]))
    _git(lane, "checkout", "-q", "--detach", at)
    return lane


def _close(world, *extra: str):
    return CliRunner().invoke(app, ["review", "close", str(world["ws"]), "reviewer", "grip", "7", *extra])


def test_plain_close_still_refuses_an_old_build_lane(world):
    lane = _clone_lane(world, world["head"])
    result = _close(world)
    assert result.exit_code != 0
    assert "lacks an owned .git directory or the review record" in str(result.exception)
    assert lane.is_dir()


def test_adopt_closes_an_old_build_lane_through_the_normal_close(world):
    lane = _clone_lane(world, world["head"])
    result = _close(world, "--adopt")
    assert result.exit_code == 0, result.output
    assert "review lane adopted" in result.output and "review lane dropped" in result.output
    assert not lane.exists()
    assert _git(world["source"], "rev-parse", "pr/7") == world["head"]


def test_adopt_refuses_an_ordinary_clone_of_the_same_repo(world):
    lane = _clone_lane(world, "main")
    result = _close(world, "--adopt")
    assert result.exit_code == 2
    assert "not the review head" in result.output
    assert lane.is_dir()


def test_adopt_refuses_a_directory_that_is_not_a_repository(world):
    lane = world["lane"]
    lane.mkdir(parents=True)
    (lane / "notes.txt").write_text("keep me\n")
    result = _close(world, "--adopt")
    assert result.exit_code == 2
    assert "no owned .git directory" in result.output
    assert (lane / "notes.txt").read_text() == "keep me\n"


def _commit(repo: Path, name: str) -> str:
    (repo / name).write_text(f"{name}\n")
    _git(repo, "add", name)
    _git(repo, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def _tracked_edit(lane: Path) -> None:
    (lane / "README.md").write_text("edited in the lane\n")


def _untracked_file(lane: Path) -> None:
    (lane / "scratch.txt").write_text("only here\n")


def _stash(lane: Path) -> None:
    (lane / "README.md").write_text("stashed in the lane\n")
    _git(lane, "-c", "user.email=t@t", "-c", "user.name=t", "stash", "-q")


def _side_branch_commit(lane: Path) -> None:
    head = _git(lane, "rev-parse", "HEAD")
    _git(lane, "checkout", "-q", "-b", "side")
    _commit(lane, "side.txt")
    _git(lane, "checkout", "-q", "--detach", head)


def _reflog_only_commit(lane: Path) -> None:
    head = _git(lane, "rev-parse", "HEAD")
    _commit(lane, "lost.txt")
    _git(lane, "reset", "-q", "--hard", head)


def _assume_unchanged_edit(lane: Path) -> None:
    _git(lane, "update-index", "--assume-unchanged", "README.md")
    (lane / "README.md").write_text("hidden from git status\n")


def _skip_worktree_edit(lane: Path) -> None:
    _git(lane, "update-index", "--skip-worktree", "README.md")
    (lane / "README.md").write_text("hidden from git status\n")


def _stale_tracking_ref(lane: Path) -> None:
    # A branch pushed and later deleted upstream: the lane still has its tracking ref, the origin has no commit.
    head = _git(lane, "rev-parse", "HEAD")
    _git(lane, "checkout", "-q", "-b", "gone")
    sha = _commit(lane, "gone.txt")
    _git(lane, "update-ref", "refs/remotes/origin/gone", sha)
    _git(lane, "checkout", "-q", "--detach", head)
    _git(lane, "branch", "-q", "-D", "gone")


def _linked_worktree(lane: Path) -> None:
    linked = lane.parent.parent / "linked"
    _git(lane, "worktree", "add", "-q", "--detach", str(linked))
    (linked / "README.md").write_text("uncommitted in the linked worktree\n")


def _side_branch_commit_without_reflogs(lane: Path) -> None:
    _git(lane, "config", "core.logAllRefUpdates", "false")
    for log in sorted((lane / ".git" / "logs").rglob("*"), reverse=True):
        log.rmdir() if log.is_dir() else log.unlink()
    _side_branch_commit(lane)


@pytest.mark.parametrize(
    ("make_work", "says"),
    [
        (_tracked_edit, "uncommitted work"),
        (_untracked_file, "uncommitted work"),
        (_stash, "a stash"),
        (_side_branch_commit, "unpublished commits"),
        (_reflog_only_commit, "unpublished commits"),
        (_assume_unchanged_edit, "git status does not show"),
        (_skip_worktree_edit, "git status does not show"),
        (_stale_tracking_ref, "unpublished commits"),
        (_linked_worktree, "linked worktrees"),
        (_side_branch_commit_without_reflogs, "unpublished commits"),
    ],
    ids=[
        "tracked-edit", "untracked-file", "stash", "side-branch-commit", "reflog-only-commit",
        "assume-unchanged", "skip-worktree", "stale-tracking-ref", "linked-worktree", "side-branch-commit-reflogs-off",
    ],
)
def test_adopt_refuses_a_lane_holding_local_work(world, make_work, says):
    lane = _clone_lane(world, world["head"])
    make_work(lane)
    before = sorted(p.relative_to(lane) for p in lane.rglob("*"))
    result = _close(world, "--adopt")
    assert result.exit_code == 2, result.output
    assert says in result.output and "nothing was recorded" in result.output
    assert lane.is_dir() and sorted(p.relative_to(lane) for p in lane.rglob("*")) == before
    paths = review_record_paths(world["ws"], "reviewer", "review-7", "grip", lane)
    assert not paths.current.exists() and not paths.legacy.exists()


def test_adopt_refuses_a_tracking_ref_the_member_holds_only_as_an_unreachable_object(world):
    # The member had the commit on a branch the lane fetched, then dropped the branch: the object stays
    # until the member's gc, but no ref of the member contains it.
    source, head = world["source"], world["head"]
    lane = _clone_lane(world, head)
    tree = _git(source, "rev-parse", f"{head}^{{tree}}")
    sha = _git(source, "-c", "user.email=t@t", "-c", "user.name=t", "commit-tree", tree, "-p", head, "-m", "z")
    _git(source, "update-ref", "refs/heads/z", sha)
    _git(lane, "fetch", "-q", str(source), "z:refs/remotes/origin/z")
    _git(source, "update-ref", "-d", "refs/heads/z")
    assert _git(source, "cat-file", "-t", sha) == "commit"
    assert _git(source, "for-each-ref", "--contains", sha) == ""
    before = sorted(p.relative_to(lane) for p in lane.rglob("*"))
    result = _close(world, "--adopt")
    assert result.exit_code == 2, result.output
    assert "unpublished commits" in result.output and "nothing was recorded" in result.output
    assert lane.is_dir() and sorted(p.relative_to(lane) for p in lane.rglob("*")) == before
    paths = review_record_paths(world["ws"], "reviewer", "review-7", "grip", lane)
    assert not paths.current.exists() and not paths.legacy.exists()


def test_adopt_refuses_a_clone_of_another_repository_at_the_pr_head(world, tmp_path):
    fork = tmp_path / "fork.git"
    _git(tmp_path, "clone", "-q", "--bare", str(world["origin"]), str(fork))
    lane = _clone_lane(world, world["head"])
    _git(lane, "remote", "set-url", "origin", str(fork))
    result = _close(world, "--adopt")
    assert result.exit_code == 2, result.output
    assert "origin identity" in result.output
    assert lane.is_dir()


def _refused_and_kept(world, lane: Path, says: str) -> None:
    result = _close(world, "--adopt")
    assert result.exit_code == 2, result.output
    assert says in result.output
    assert (lane / ".git").is_dir()


def test_adopt_refuses_a_symlink_lane(world):
    real = world["lane"].parent / "elsewhere"
    real.parent.mkdir(parents=True)
    _git(real.parent, "clone", "-q", str(world["source"]), str(real))
    _git(real, "remote", "set-url", "origin", str(world["origin"]))
    _git(real, "checkout", "-q", "--detach", world["head"])
    world["lane"].symlink_to(real)
    _refused_and_kept(world, real, "symlink lane")


def test_adopt_refuses_a_lane_that_resolves_outside_the_lane_root(world, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    world["lane"].parent.parent.mkdir(parents=True)
    world["lane"].parent.symlink_to(outside)
    _git(outside, "clone", "-q", str(world["source"]), "grip")
    lane = outside / "grip"
    _git(lane, "remote", "set-url", "origin", str(world["origin"]))
    _git(lane, "checkout", "-q", "--detach", world["head"])
    _refused_and_kept(world, lane, "not strictly beneath")


def test_adopt_refuses_a_lane_that_already_carries_a_record(world):
    lane = _clone_lane(world, world["head"])
    paths = review_record_paths(world["ws"], "reviewer", "review-7", "grip", lane)
    paths.current.parent.mkdir(parents=True, exist_ok=True)
    paths.current.write_text("{}\n")
    _refused_and_kept(world, lane, "already carries a review record")


def test_adopt_refuses_a_pr_head_with_no_merge_base(world):
    source = world["source"]
    _git(source, "checkout", "-q", "--orphan", "unrelated")
    _git(source, "rm", "-q", "-rf", ".")
    head = _commit(source, "unrelated.txt")
    _git(source, "branch", "-q", "-f", "pr/7", head)
    _git(source, "checkout", "-q", "-f", "main")
    lane = _clone_lane(world, head)
    _refused_and_kept(world, lane, "no merge-base")
    assert not review_record_paths(world["ws"], "reviewer", "review-7", "grip", lane).current.exists()


def test_adopt_refuses_a_pr_head_not_merged(world):
    _git(world["source"], "reset", "-q", "--hard", "HEAD~1")
    lane = _clone_lane(world, world["head"])
    _refused_and_kept(world, lane, "is not merged into")
    assert not review_record_paths(world["ws"], "reviewer", "review-7", "grip", lane).current.exists()


def test_adopt_refuses_a_lane_git_cannot_read(world):
    lane = _clone_lane(world, world["head"])
    (lane / ".git" / "index").write_bytes(b"not an index")
    _refused_and_kept(world, lane, "could not read")


def test_adopt_is_refused_for_a_reconstruction_close(world):
    lane = _clone_lane(world, world["head"])
    result = _close(world, "--adopt", "--workspace", str(world["ws"]))
    assert result.exit_code == 2, result.output
    assert "not a reconstruction lane" in result.output
    assert lane.is_dir() and world["ws"].is_dir()


def test_adopt_reads_the_merge_base_in_the_real_history_through_a_replaced_checkout(world):
    # refs/replace/<main> -> a parentless commit with main's tree: a plain merge-base of the PR head and HEAD finds
    # none, though the real history holds the head (the merge is real), so the adopt must not refuse.
    source = world["source"]
    main = _git(source, "rev-parse", "HEAD")
    fake = _git(source, "commit-tree", _git(source, "rev-parse", "HEAD^{tree}"), "-m", "fake")
    _git(source, "replace", main, fake)
    assert subprocess.run(["git", "merge-base", world["head"], "HEAD"], cwd=source).returncode == 1
    lane = _clone_lane(world, world["head"])
    result = _close(world, "--adopt")
    assert "no merge-base" not in result.output, result.output
    assert result.exit_code == 0, result.output
    assert not lane.exists()


def test_adopt_closes_a_lane_whose_only_extra_commit_is_held_by_a_remote_tag(world):
    """The --adopt close: a clone carries the remote's tags as local tags, and a commit only
    such a tag reaches is the remote's, not the lane's (the read-lane close got the same fix)."""
    source = world["source"]
    _git(source, "checkout", "-q", "-b", "scratch")
    (source / "archived.txt").write_text("archived\n")
    _git(source, "add", ".")
    _git(source, "commit", "-q", "-m", "archived plan")
    archived = _git(source, "rev-parse", "HEAD")
    _git(source, "tag", "archive/old-plan")
    _git(source, "checkout", "-q", "main")
    _git(source, "branch", "-qD", "scratch")
    _git(source, "push", "-q", "origin", "refs/tags/archive/old-plan")
    lane = _clone_lane(world, world["head"])
    assert _git(lane, "rev-parse", "refs/tags/archive/old-plan") == archived
    result = _close(world, "--adopt")
    assert result.exit_code == 0, result.output
    assert not lane.exists()


def test_adopt_still_refuses_a_tag_made_in_the_lane_on_its_own_commit(world):
    lane = _clone_lane(world, world["head"])
    (lane / "mine.txt").write_text("mine\n")
    _git(lane, "add", ".")
    _git(lane, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "mine")
    mine = _git(lane, "rev-parse", "HEAD")
    _git(lane, "tag", "my-mark")
    _git(lane, "checkout", "-q", "--detach", world["head"])
    result = _close(world, "--adopt")
    assert result.exit_code != 0
    assert "unpublished commits" in result.output and mine[:12] in result.output, result.output
    assert lane.is_dir()


def test_adopt_refuses_a_lane_tagged_commit_when_a_remote_is_the_lane_itself(world):
    """A remote that resolves to the lane advertises the lane's own tags; they hold nothing elsewhere."""
    lane = _clone_lane(world, world["head"])
    (lane / "mine.txt").write_text("mine\n")
    _git(lane, "add", ".")
    _git(lane, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "mine")
    mine = _git(lane, "rev-parse", "HEAD")
    _git(lane, "tag", "my-mark")
    _git(lane, "checkout", "-q", "--detach", world["head"])
    _git(lane, "remote", "add", "self", str(lane))
    result = _close(world, "--adopt")
    assert result.exit_code != 0
    assert "unpublished commits" in result.output and mine[:12] in result.output, result.output
    assert lane.is_dir()


def test_adopt_refuses_when_a_remote_is_the_lane_spelled_in_another_case(world):
    lane = _clone_lane(world, world["head"])
    upper = Path(str(lane).upper())
    if not (upper.exists() and os.path.samefile(upper, lane)):
        pytest.skip("this filesystem is case-sensitive")
    (lane / "mine.txt").write_text("mine\n")
    _git(lane, "add", ".")
    _git(lane, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-q", "-m", "mine")
    mine = _git(lane, "rev-parse", "HEAD")
    _git(lane, "tag", "my-mark")
    _git(lane, "checkout", "-q", "--detach", world["head"])
    _git(lane, "remote", "add", "self", str(upper))
    assert mine in _git(lane, "ls-remote", "--tags", "self")
    result = _close(world, "--adopt")
    assert result.exit_code != 0
    assert "unpublished commits" in result.output and mine[:12] in result.output, result.output
    assert lane.is_dir()
