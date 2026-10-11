"""Two review-run gaps a stranger hit on the released build.

A failed venv create printed an empty reason and left a half-built `<lane>/.venv` that the next run reused
(`install_failed ... No module named pip`) until the lane was closed and reopened: the reason a failing
`python -m venv` gives can be on stdout, and the directory it half-built is this call's own to remove.

A refused run's receipt carried no `gr_commit`, so the copy `close` keeps was named `unknown-<timestamp>`; the lane's
marker knows the commit, so the refusal receipt now carries it.
"""
from __future__ import annotations

import json
import stat
from pathlib import Path

import pytest

from gr2.python_cli import open_gr_review as ogr
from gr2.python_cli import review_run as rr
from gr2.python_cli import git_review
from tests.test_git_review import PASS_BODY, _pkg_repo as _git_pkg_repo
from tests.test_review_run import PASS_TEST, _git, _offline_install, _pkg_repo, _write_marker

REASON = "The virtual environment was not created successfully because ensurepip is not available."


def _interpreter(tmp_path: Path, body: str) -> str:
    stub = tmp_path / "stub-python"
    stub.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    stub.chmod(stub.stat().st_mode | stat.S_IEXEC)
    return str(stub)


def _lane(tmp_path: Path) -> Path:
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    return repo


def test_a_venv_failure_names_the_reason_even_when_it_is_on_stdout(tmp_path):
    repo = _lane(tmp_path)
    python = _interpreter(tmp_path, f'echo "{REASON}"\nexit 1\n')   # nothing on stderr, as ensurepip's failure is
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"], python=python)
    assert exc.value.code == "venv_failed"
    assert "ensurepip is not available" in exc.value.detail, exc.value.detail


def test_a_venv_failure_with_no_output_still_says_how_it_failed(tmp_path):
    repo = _lane(tmp_path)
    python = _interpreter(tmp_path, "exit 3\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"], python=python)
    assert exc.value.code == "venv_failed"
    assert not exc.value.detail.rstrip().endswith("venv create failed:"), exc.value.detail
    assert "3" in exc.value.detail, exc.value.detail   # the exit status, since there is no text


def test_a_failed_venv_create_leaves_no_half_built_venv_and_the_retry_builds_one(tmp_path):
    repo = _lane(tmp_path)
    # the stub half-builds the directory it was asked to create (its last argument), then fails
    python = _interpreter(tmp_path, 'for last; do :; done\nmkdir -p "$last/bin"\necho half-built >&2\nexit 1\n')
    with pytest.raises(rr.ReviewRunRefused):
        rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"], python=python)
    assert not (repo / rr._VENV_DIRNAME).exists(), "the half-built venv was left for the retry to reuse"
    again = rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"], install=_offline_install(repo))
    assert again["result"] == "green"


def test_a_venv_that_was_already_there_is_not_removed_by_a_failed_create(tmp_path):
    """Only what this call created is removed: a venv that existed before it is the owner's."""
    repo = _lane(tmp_path)
    venv = repo / rr._VENV_DIRNAME
    (venv / "bin").mkdir(parents=True)
    (venv / "marker").write_text("keep", encoding="utf-8")
    python = _interpreter(tmp_path, "echo no >&2\nexit 1\n")
    with pytest.raises(rr.ReviewRunRefused):
        rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"], python=python)
    assert (venv / "marker").read_text(encoding="utf-8") == "keep"


def _refuse_with_marker(repo: Path, head_tree: str) -> None:
    marker = {"kind": "open-gr-reconstruct", "gr_commit": "deadbeef" * 5,
              "repos": [{"key": "a", "bound_head": "x", "bound_head_tree": head_tree},
                        {"key": "b", "bound_head": "y", "bound_head_tree": head_tree}]}
    (repo / rr._MARKER_NAME).write_text(json.dumps(marker) + "\n")
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"])
    assert exc.value.code == "member_flags_ambiguous"


def test_a_refusal_receipt_carries_the_lane_gr_commit(tmp_path):
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _refuse_with_marker(repo, head_tree)
    receipt = json.loads((repo / rr._RECEIPT_NAME).read_text())
    assert receipt["result"] == "refused"
    assert receipt["gr_commit"] == "deadbeef" * 5


def test_the_receipt_close_keeps_for_a_refused_run_is_named_by_the_commit_not_unknown(tmp_path):
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _refuse_with_marker(repo, head_tree)
    kept = ogr._preserve_run_artifacts(repo, archive_root=tmp_path / "kept")
    name = Path(kept["dir"]).name
    assert name.startswith("deadbeefdead-"), name
    assert not name.startswith("unknown-")


def test_the_single_repo_review_run_names_the_venv_reason_and_leaves_no_half_built_venv(tmp_path):
    """The second venv site, `git_review`: the same two behaviours for a plain git repo's review."""
    repo = _git_pkg_repo(tmp_path, test_body=PASS_BODY)
    git_review.open_review(repo)
    python = _interpreter(tmp_path, f'for last; do :; done\nmkdir -p "$last/bin"\necho "{REASON}"\nexit 1\n')
    with pytest.raises(rr.ReviewRunRefused) as exc:
        git_review.run_review(repo, python=python)
    assert exc.value.code == "venv_failed" and "ensurepip is not available" in exc.value.detail, exc.value.detail
    assert not (repo / ".git" / "grip" / git_review.VENV_DIRNAME).exists()


def test_a_half_built_venv_that_cannot_be_removed_is_named_in_the_refusal(tmp_path, monkeypatch):
    """The cleanup goes through the shared removal helper, which verifies the tree is gone; when it is not, say so."""
    from gr2.python_cli import clone_exec

    def _stuck(path):
        raise clone_exec.IncompleteRemoval(f"{path} could not be fully removed; 1 entry beneath it remains")

    monkeypatch.setattr(clone_exec, "rmtree_or_refuse", _stuck)
    repo = _lane(tmp_path)
    python = _interpreter(tmp_path, 'for last; do :; done\nmkdir -p "$last/bin"\necho half-built >&2\nexit 1\n')
    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"], python=python)
    assert exc.value.code == "venv_failed"
    assert "half-built" in exc.value.detail and "could not be fully removed" in exc.value.detail, exc.value.detail
