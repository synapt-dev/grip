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
        remote = _git(lane, "remote", "get-url", "origin")
        assert _git(Path(remote), "for-each-ref", "--contains", commit) == ""
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
