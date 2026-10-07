"""Reconstruction close must keep user work and its recovery evidence.

Both the public close verb and its close-gr compatibility route own the same
disposal boundary. A review's marker and run artifacts are tool-owned, so they
must not turn the clean-lane control into a dirty-work refusal.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from gr2.python_cli import app as gr2_app
from gr2.python_cli import open_gr_review, review_allocation, review_run
from tests.conftest import make_cli_runner
from tests.test_review_close_gr import _git, _open_gr_lane


@pytest.fixture
def reconstruction(tmp_path: Path):
    runner = make_cli_runner()
    lane, review_id = _open_gr_lane(tmp_path, runner)
    workspace = tmp_path / "ws"
    allocation = review_allocation.allocation_path(workspace, lane)
    marker = lane / open_gr_review._OPEN_GR_MARKER
    assert allocation.is_file() and marker.is_file()
    # These are the exact artifact names/schema written by review run. The
    # failing result is evidence to retain, not uncommitted user work.
    receipt = lane / review_run._RECEIPT_NAME
    log = lane / review_run._OUTPUT_LOG_NAME
    log.write_text("FAILED tests/example.py::test_example - boundary witness\n")
    receipt.write_text(json.dumps({
        "kind": "review-run", "created": "2026-10-07T00:00:00+00:00",
        "gr_commit": review_id, "result": "red", "failed": 1,
        "failed_ids": ["tests/example.py::test_example"],
        "output_log": review_run._OUTPUT_LOG_NAME,
    }) + "\n")
    # No tracked work is dirty before the witness changes it.
    assert _git(lane, "diff", "--name-only") == ""
    assert _git(lane, "diff", "--cached", "--name-only") == ""
    return runner, lane, allocation, (marker, receipt, log)


def _put_local_work(lane: Path, kind: str) -> tuple[Path, str | None]:
    file = lane / ("new-user-file.txt" if kind == "untracked" else "f.txt")
    file.write_text("user work must survive close\n")
    if kind in {"staged", "unpublished"}:
        _git(lane, "add", file.name)
    if kind == "unpublished":
        reviewed_head = _git(lane, "rev-parse", "HEAD")
        _git(lane, "checkout", "-qb", "local-review-work")
        _git(lane, "-c", "user.name=Fixture", "-c",
             "user.email=fixture@example.invalid", "commit", "-qm", "local work")
        commit = _git(lane, "rev-parse", "HEAD")
        assert _git(lane, "branch", "-r", "--contains", commit) == ""
        # Query the real local bare origin as well as cached remote-tracking
        # refs: this commit is absent from every advertised remote branch.
        assert _git(lane, "remote") == "origin"
        remote = _git(lane, "remote", "get-url", "origin")
        assert commit not in _git(Path(remote), "rev-list", "--all").splitlines()
        _git(lane, "checkout", "-q", "--detach", reviewed_head)
        assert _git(lane, "rev-parse", "HEAD") == reviewed_head
        assert _git(lane, "diff", "--name-only") == ""
        assert _git(lane, "diff", "--cached", "--name-only") == ""
        assert _git(lane, "rev-parse", "refs/heads/local-review-work") == commit
        return file, commit
    status = _git(lane, "status", "--porcelain", "--", file.name)
    assert status == {"staged": "M  f.txt", "unstaged": "M f.txt",
                      "untracked": "?? new-user-file.txt"}[kind]
    return file, None


@pytest.mark.parametrize("verb", ["close", "close-gr"])
@pytest.mark.parametrize("kind", ["staged", "unstaged", "untracked", "unpublished"])
def test_reconstruction_close_refuses_local_work_and_keeps_evidence(reconstruction, verb, kind):
    runner, lane, allocation, evidence = reconstruction
    file, commit = _put_local_work(lane, kind)
    before = {path: path.read_bytes() for path in (allocation, *evidence)}
    status = _git(lane, "status", "--porcelain")
    head = _git(lane, "rev-parse", "HEAD")
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])

    assert result.exit_code != 0, (
        f"unsafe {verb} accepted {kind} user work: rc={result.exit_code}, "
        f"lane_kept={lane.exists()}, allocation_kept={allocation.exists()}, "
        f"run_evidence_kept={all(p.exists() for p in evidence[1:])}; {result.output}"
    )
    text = result.output.lower()
    assert "refused" in text and str(lane) in result.output, result.output
    assert ("unpublished" in text or "unpushed" in text) if commit else (
        "uncommitted" in text or "dirty" in text
    ), result.output
    assert lane.is_dir() and (lane / ".git").is_dir()
    assert {path: path.read_bytes() for path in before} == before
    assert _git(lane, "status", "--porcelain") == status
    assert _git(lane, "rev-parse", "HEAD") == head
    assert _git(lane, "stash", "list") == ""
    if commit:
        assert _git(lane, "rev-parse", "refs/heads/local-review-work") == commit
        assert _git(lane, "show", f"{commit}:f.txt") == "user work must survive close"
    else:
        assert file.read_text() == "user work must survive close\n"


@pytest.mark.parametrize("verb", ["close", "close-gr"])
def test_reconstruction_close_clean_control_preserves_run_evidence(reconstruction, verb):
    runner, lane, allocation, evidence = reconstruction
    receipt_before, log_before = evidence[1].read_bytes(), evidence[2].read_bytes()
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])
    assert result.exit_code == 0, result.output
    assert not lane.exists() and not allocation.exists()
    payload = json.loads(result.stdout)
    preserved = payload["preserved_run"]
    assert Path(preserved["original_receipt"]).read_bytes() == receipt_before
    assert Path(preserved["log"]).read_bytes() == log_before
    usable = json.loads(Path(preserved["receipt"]).read_text())
    assert usable["failed_ids"] == ["tests/example.py::test_example"]
    assert Path(usable["output_log"]) == Path(preserved["log"])


@pytest.mark.parametrize("verb", ["close", "close-gr"])
def test_reconstruction_close_refuses_a_lane_holding_a_stash(reconstruction, verb):
    """A stash is work too: a clean tree with a stash must not be removed with it."""
    runner, lane, allocation, _ = reconstruction
    (lane / "f.txt").write_text("stashed user work\n")
    _git(lane, "stash", "push", "-q", "-m", "user stash")
    assert _git(lane, "status", "--porcelain", "--", "f.txt") == ""
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])
    assert result.exit_code != 0, result.output
    assert "stash" in result.output.lower(), result.output
    assert lane.is_dir() and allocation.exists()
    assert _git(lane, "show", "stash@{0}:f.txt") == "stashed user work"


@pytest.mark.parametrize("verb", ["close", "close-gr"])
def test_reconstruction_close_refuses_when_git_cannot_read_the_lane(reconstruction, verb):
    """A git call that fails must not read as a clean lane: a corrupt index makes status fail."""
    runner, lane, allocation, _ = reconstruction
    (lane / "new-user-file.txt").write_text("user work\n")
    (lane / ".git" / "index").write_bytes(b"not an index")
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])
    assert result.exit_code != 0, result.output
    assert "could not read" in result.output, result.output
    assert (lane / "new-user-file.txt").read_text() == "user work\n" and allocation.exists()


@pytest.mark.parametrize("verb", ["close", "close-gr"])
def test_reconstruction_close_refuses_a_commit_on_the_reviewed_head(reconstruction, verb):
    runner, lane, allocation, _ = reconstruction
    (lane / "f.txt").write_text("committed on the reconstruction\n")
    _git(lane, "-c", "user.name=F", "-c", "user.email=f@x.invalid", "commit", "-qam", "user commit")
    commit = _git(lane, "rev-parse", "HEAD")
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])
    assert result.exit_code != 0, result.output
    assert lane.is_dir() and allocation.exists() and _git(lane, "rev-parse", "HEAD") == commit


@pytest.mark.parametrize("verb", ["close", "close-gr"])
def test_reconstruction_close_refuses_a_local_only_tag(reconstruction, verb):
    runner, lane, allocation, _ = reconstruction
    head = _git(lane, "rev-parse", "HEAD")
    (lane / "f.txt").write_text("tagged work\n")
    _git(lane, "-c", "user.name=F", "-c", "user.email=f@x.invalid", "commit", "-qam", "tagged")
    _git(lane, "tag", "kept-work")
    _git(lane, "checkout", "-q", "--detach", head)
    result = runner.invoke(gr2_app.app, ["review", verb, str(lane), "--json"])
    assert result.exit_code != 0 and "unpublished" in result.output.lower(), result.output
    assert lane.is_dir() and allocation.exists()


def _multi_member_lane(tmp_path: Path) -> Path:
    from tests.test_review_run_multi_repo import _lane
    lane = _lane(tmp_path)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    review_allocation.record_created_allocation(
        workspace, lane, "workspace", "review", [lane / "demo-core", lane / "demo-web"], disposable=True)
    marker_path = lane / review_run._MARKER_NAME
    marker = json.loads(marker_path.read_text())
    marker["workspace_root"] = str(workspace)
    marker_path.write_text(json.dumps(marker))
    return lane


def test_close_refuses_a_user_file_at_the_root_of_a_multi_member_lane(tmp_path: Path):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    (lane / "notes.txt").write_text("user notes\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "lane root" in result.output, result.output
    assert (lane / "notes.txt").read_text() == "user notes\n"


def test_a_clean_multi_member_lane_still_closes(tmp_path: Path):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    (lane / ".venv").mkdir()
    (lane / f"demo-core{review_run._OUTPUT_LOG_NAME}").write_text("log\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, result.output
    assert not lane.exists()


@pytest.mark.parametrize("name", ["mine.grip-review-run.log", ".grip-review-run.log.bak"])
def test_a_root_file_that_only_looks_like_a_run_log_is_work(tmp_path: Path, name: str):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    (lane / name).write_text("user file\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and name in result.output, result.output
    assert (lane / name).read_text() == "user file\n"


def test_the_exact_run_log_names_at_the_root_are_the_reviews_own(tmp_path: Path):
    runner = make_cli_runner()
    lane = _multi_member_lane(tmp_path)
    for name in (review_run._OUTPUT_LOG_NAME, f"demo-web{review_run._OUTPUT_LOG_NAME}",
                 f"{review_run._OUTPUT_LOG_NAME}.demo-core", *review_run._LEGACY_MARKERS):
        (lane / name).write_text("tool output\n")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code == 0, result.output


def test_a_stash_is_named_once_and_not_as_an_unpublished_commit(reconstruction):
    runner, lane, _, _ = reconstruction
    (lane / "f.txt").write_text("stashed\n")
    _git(lane, "stash", "push", "-q")
    result = runner.invoke(gr2_app.app, ["review", "close", str(lane), "--json"])
    assert result.exit_code != 0 and "a stash" in result.output, result.output
    assert "unpublished" not in result.output.lower(), result.output
