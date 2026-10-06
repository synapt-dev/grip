"""Regression probes for coexisting review receipts during close."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from test_review_open_lane import review_world, _new_record, _open, _run  # noqa: F401
from gr2.python_cli.commit import CommitError, _refuse_review_ephemeral_repo
from gr2.python_cli.push import PushError, _refuse_review_ephemeral_repo as refuse_push
from gr2.python_cli.review import close_review_lane
from gr2.python_cli.review_records import (
    legacy_review_record_path,
    review_record_pointer_path,
    ReviewRecordLocationError,
)


def _compat_record(world):
    return (world["workspace_root"] / ".grip/state/reviews/atlas/review-7/grip.json")


def _plant_legacy(world, kind):
    path = _compat_record(world)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = json.loads(_new_record(world).read_bytes())
    path.write_text(json.dumps({**record, "lane_kind": kind}, indent=2) + "\n")


def test_control_git_ephemeral_alone_refuses(review_world):
    _open(review_world, ephemeral=True)
    assert json.loads(_new_record(review_world).read_text())["lane_kind"] == "review-ephemeral"
    assert not _compat_record(review_world).exists()
    with pytest.raises(CommitError, match="review-ephemeral"):
        _refuse_review_ephemeral_repo(review_world["lane"])


def test_conflicting_legacy_reopen_preserves_both_receipts(review_world):
    _open(review_world, ephemeral=True)
    _plant_legacy(review_world, "materialized")
    before = {p: p.read_bytes() for p in (_new_record(review_world), _compat_record(review_world))}
    with pytest.raises(ReviewRecordLocationError, match="safety evidence conflict"):
        _open(review_world, ephemeral=True)
    assert {p: p.read_bytes() for p in before} == before
    with pytest.raises(CommitError, match="safety evidence conflict"):
        _refuse_review_ephemeral_repo(review_world["lane"])


def test_conflicting_legacy_push_refuses_without_changing_receipts(review_world):
    _open(review_world, ephemeral=True)
    _plant_legacy(review_world, "materialized")
    before = {p: p.read_bytes() for p in (_new_record(review_world), _compat_record(review_world))}
    with pytest.raises(PushError, match="safety evidence conflict"):
        refuse_push(review_world["lane"])
    assert {p: p.read_bytes() for p in before} == before


def _close(world):
    close_review_lane(
        lane_repo_root=world["lane"], review_lane_root=world["lane"].parent.parent,
        workspace_root=world["workspace_root"], owner_unit="atlas",
        lane_name="review-7", member="grip", echo=lambda _m: None,
    )


def test_control_close_single_receipt_leaves_no_receipt(review_world):
    _open(review_world)
    _close(review_world)
    assert not review_world["lane"].exists()
    assert not _new_record(review_world).exists(), "canonical receipt orphaned by close (control)"


def test_close_with_coexisting_receipts_leaves_no_receipt(review_world):
    _open(review_world)
    record = json.loads(_new_record(review_world).read_text())
    _compat_record(review_world).parent.mkdir(parents=True, exist_ok=True)
    _compat_record(review_world).write_text(json.dumps(record) + "\n")
    record["lane_kind"] = "review-ephemeral"
    _new_record(review_world).write_text(json.dumps(record) + "\n")
    _compat_record(review_world).write_text(json.dumps(record) + "\n")
    _close(review_world)
    assert not review_world["lane"].exists()
    assert not _new_record(review_world).exists(), "Git safety receipt orphaned by close"
    assert not _compat_record(review_world).exists(), "workspace compatibility receipt orphaned by close"


def test_closed_receipt_does_not_block_a_later_work_lane_of_the_same_name(review_world):
    _open(review_world)
    record = json.loads(_new_record(review_world).read_text())
    _compat_record(review_world).parent.mkdir(parents=True, exist_ok=True)
    _compat_record(review_world).write_text(json.dumps(record) + "\n")
    record["lane_kind"] = "review-ephemeral"
    _new_record(review_world).write_text(json.dumps(record) + "\n")
    _compat_record(review_world).write_text(json.dumps(record) + "\n")
    _close(review_world)
    assert not _new_record(review_world).exists(), "fixture: close must remove canonical receipt"
    work = (review_world["workspace_root"] / ".grip" / "state" / "lanes" / "atlas"
            / "review-7" / "repos" / "grip")
    work.mkdir(parents=True)
    _run(work, "init", "-q", ".")
    assert not review_record_pointer_path(work).exists()
    assert not legacy_review_record_path(work).exists()
    _refuse_review_ephemeral_repo(work)
