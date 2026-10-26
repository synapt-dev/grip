"""A read lane's close counts what its remote's TAGS hold as published.

A read lane is cloned from the host's review mirror, and git keeps no remote-tag refs, so every mirror tag
arrives as a local tag. Before this change, a commit only a mirror tag reaches read as lane work and close
refused a clean lane ("unpublished commits ... exist nowhere else"; 26 of them, all in the mirror).
The rows: a commit the remote holds under a tag closes; a commit made in the lane, and a tag made in the lane,
still refuse; a remote that cannot be asked refuses with its reason, never closes.
"""
from __future__ import annotations

import os
from pathlib import Path

import pytest

from gr2.python_cli import app as gr2_app
from tests.test_review_close_gr import _git
from tests.test_review_close_keeps_local_work import _text, reconstruction  # noqa: F401 (the fixture)


def _origin(lane: Path) -> Path:
    return Path(_git(lane, "remote", "get-url", "origin").removeprefix("file://"))


def _commit_off_every_branch(lane: Path, name: str) -> str:
    """A commit on no branch of the lane: made on a scratch branch, which is then deleted."""
    head = _git(lane, "rev-parse", "HEAD")
    _git(lane, "checkout", "-qb", f"scratch-{name}")
    (lane / f"{name}.txt").write_text(f"{name}\n")
    _git(lane, "add", f"{name}.txt")
    _git(lane, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid", "commit", "-qm", name)
    commit = _git(lane, "rev-parse", "HEAD")
    _git(lane, "checkout", "-q", "--detach", head)
    _git(lane, "branch", "-qD", f"scratch-{name}")
    return commit


def test_a_commit_only_a_remote_tag_holds_does_not_block_close(reconstruction):
    runner, lane, allocation, _ = reconstruction
    commit = _commit_off_every_branch(lane, "archived")
    _git(lane, "tag", "archive/old-plan", commit)
    # The remote holds that commit under the same tag, and under no branch: the mirror shape.
    _git(lane, "push", "-q", "origin", "refs/tags/archive/old-plan")
    assert commit not in _git(_origin(lane), "rev-list", "--branches").splitlines()
    assert _git(_origin(lane), "rev-parse", "refs/tags/archive/old-plan") == commit
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, _text(result)
    assert not lane.exists() and not allocation.exists()


def test_a_tag_made_in_the_lane_on_a_new_commit_still_refuses(reconstruction):
    runner, lane, allocation, _ = reconstruction
    commit = _commit_off_every_branch(lane, "mine")
    _git(lane, "tag", "my-mark", commit)
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0, _text(result)
    assert "unpublished commits" in _text(result) and commit[:12] in _text(result), _text(result)
    assert lane.is_dir() and allocation.exists()
    assert _git(lane, "rev-parse", "refs/tags/my-mark") == commit


def test_a_remote_that_cannot_be_asked_refuses_the_close_with_its_reason(reconstruction):
    runner, lane, allocation, _ = reconstruction
    _git(lane, "remote", "set-url", "origin", f"file://{lane.parent / 'no-such-mirror.git'}")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0, _text(result)
    assert "refused" in _text(result).lower() and "cannot list origin's tags" in _text(result), _text(result)
    assert lane.is_dir() and allocation.exists()


def test_an_annotated_remote_tag_closes_like_a_lightweight_one(reconstruction):
    """The remote advertises a tag object and its peeled commit; the commit is held either way."""
    runner, lane, allocation, _ = reconstruction
    commit = _commit_off_every_branch(lane, "annotated")
    _git(lane, "-c", "user.name=Fixture", "-c", "user.email=fixture@example.invalid",
         "tag", "-a", "-m", "release", "archive/annotated", commit)
    _git(lane, "push", "-q", "origin", "refs/tags/archive/annotated")
    assert _git(_origin(lane), "cat-file", "-t", "refs/tags/archive/annotated") == "tag"
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, _text(result)
    assert not lane.exists() and not allocation.exists()


def _refuses_with_a_self_remote(reconstruction, url_of) -> None:
    """A remote whose URL resolves into the lane holds only the lane's own tags, so a lane-made tagged commit
    still reads as lane work and close refuses; nothing is removed."""
    runner, lane, allocation, _ = reconstruction
    commit = _commit_off_every_branch(lane, "mine")
    _git(lane, "tag", "my-mark", commit)
    _git(lane, "remote", "add", "self", url_of(lane))
    assert commit in _git(lane, "ls-remote", "--tags", "self")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0, _text(result)
    assert "unpublished commits" in _text(result) and commit[:12] in _text(result), _text(result)
    assert lane.is_dir() and allocation.exists()


def test_a_remote_that_is_the_lane_itself_does_not_hold_its_tags(reconstruction):
    _refuses_with_a_self_remote(reconstruction, lambda lane: str(lane))


def test_a_file_url_into_the_lane_does_not_hold_its_tags(reconstruction):
    _refuses_with_a_self_remote(reconstruction, lambda lane: f"file://{lane / '.git'}")


def test_a_relative_path_to_the_lane_does_not_hold_its_tags(reconstruction):
    _refuses_with_a_self_remote(reconstruction, lambda lane: ".")


def _folds_case(path: Path) -> bool:
    other = Path(str(path).upper())
    return other.exists() and os.path.samefile(other, path)


def test_the_lane_path_in_another_case_does_not_hold_its_tags(reconstruction):
    """On a case-insensitive filesystem the upper-case spelling reaches the lane; the skip compares identity."""
    lane = reconstruction[1]
    if not _folds_case(lane):
        pytest.skip("this filesystem is case-sensitive")
    _refuses_with_a_self_remote(reconstruction, lambda lane: str(lane).upper())
