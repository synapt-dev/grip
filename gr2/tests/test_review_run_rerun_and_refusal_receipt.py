"""The two review-run doors found while dogfooding a gr2 review.

Door a (a lane runs once): `review run` writes `.grip-review-run.log` into the lane,
but that name is not in `_UNTRACKED_ALLOW_NAMES`, so a SECOND `review run` on the same
lane refuses `untracked_drift` on the first run's own log. A lane could be run exactly
once; a re-run (after a fix, or to reproduce a red) was impossible.

Door b (a refused run keeps no receipt): every `ReviewRunRefused` is raised before the
receipt is written, so a run that refuses (tree drift, import failure, missing pytest,
zero collected) leaves nothing on disk. close-gr reclaims the lane and carries out the
receipt/log; a refusal therefore vanished, and `review run --json` emitted nothing on a
refusal. A refused run must leave a receipt saying WHY it refused.
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from gr2.python_cli import review_run as rr

# Reuse the real lane-building helpers from the sibling suite.
from tests.test_review_run import (  # type: ignore
    _git,
    _offline_install,
    _pkg_repo,
    _write_marker,
    PASS_TEST,
)


# ---------------------------------------------------------------- door a: re-run
def test_a_lane_can_be_run_twice(tmp_path: Path):
    """A lane run once (green) can be run AGAIN. Before the fix the second run refuses
    `untracked_drift` on the first run's own `.grip-review-run.log`."""
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    _write_marker(repo, _git(repo, "rev-parse", "HEAD"), head_tree)
    first = rr.run_review_lane(
        repo, package="demo_pkg", pytest_args=["-q"], install=_offline_install(repo)
    )
    assert first["result"] == "green"
    assert (repo / rr._OUTPUT_LOG_NAME).is_file()  # the log the re-run must tolerate

    # The second run must not refuse because the run's OWN artifacts are on disk.
    second = rr.run_review_lane(
        repo, package="demo_pkg", pytest_args=["-q"], install=_offline_install(repo)
    )
    assert second["result"] == "green"


def test_the_run_log_is_allowlisted_untracked():
    """The output log is a run-created artifact, exactly like the receipt and marker,
    so the drift check must allowlist it by name."""
    assert rr._is_allowlisted_untracked(rr._OUTPUT_LOG_NAME)


# ------------------------------------------------------------ door b: refusal receipt
def test_a_refused_run_leaves_a_receipt_naming_the_reason(tmp_path: Path):
    """A real lane whose run refuses must leave `.grip-review-run.json` with
    result=refused and the refusal code, so close-gr carries the refusal out and
    `review run --json` has something to print."""
    repo, head_tree = _pkg_repo(tmp_path, test_body=PASS_TEST)
    # A two-repo marker is a deterministic refusal (`member_flags_ambiguous`) that needs no
    # venv or network, and it happens AFTER the marker is confirmed -- i.e. this IS a
    # lane, which is the case door b is about.
    marker = {
        "kind": "open-gr-reconstruct",
        "gr_commit": "deadbeef" * 5,
        "repos": [
            {"key": "a", "bound_head": "x", "bound_head_tree": head_tree},
            {"key": "b", "bound_head": "y", "bound_head_tree": head_tree},
        ],
    }
    (repo / rr._MARKER_NAME).write_text(json.dumps(marker) + "\n")

    with pytest.raises(rr.ReviewRunRefused) as exc:
        rr.run_review_lane(repo, package="demo_pkg", pytest_args=["-q"])
    assert exc.value.code == "member_flags_ambiguous"

    receipt_path = repo / rr._RECEIPT_NAME
    assert receipt_path.is_file(), "a refused run must leave a receipt on disk"
    receipt = json.loads(receipt_path.read_text())
    assert receipt["result"] == "refused"
    assert receipt["refusal_code"] == "member_flags_ambiguous"
    assert "member_flags_ambiguous" in receipt["refusal_detail"] or receipt["refusal_detail"]


def test_a_non_lane_dir_gets_no_refusal_receipt(tmp_path: Path):
    """`no_marker`/`not_open_gr` mean the dir is not a lane at all; do not litter it
    with a receipt. Only a confirmed lane's refusal is recorded."""
    d = tmp_path / "not_a_lane"
    d.mkdir()
    with pytest.raises(rr.ReviewRunRefused, match="no_marker"):
        rr.run_review_lane(d, package="demo_pkg", pytest_args=[])
    assert not (d / rr._RECEIPT_NAME).exists()


def test_cli_review_run_json_emits_the_refusal(tmp_path: Path):
    """`review run --json` on a refusal prints machine-readable refusal JSON (not only
    the stderr line) and still exits 2, so an automated caller reading --json is not
    left with empty stdout on a refused run."""
    from typer.testing import CliRunner

    from gr2.python_cli.app import app

    d = tmp_path / "not_a_lane"
    d.mkdir()
    result = CliRunner().invoke(app, ["review", "run", str(d), "--json"])
    assert result.exit_code == 2, result.output
    # the JSON block is present and names the refusal
    payload = json.loads(result.output[result.output.index("{"):result.output.rindex("}") + 1])
    assert payload["result"] == "refused"
    assert payload["refusal_code"] == "no_marker"
