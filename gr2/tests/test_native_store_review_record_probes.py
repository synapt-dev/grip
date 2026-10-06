"""Git-active review safety and workspace compatibility acceptance probes.

Active records live in per-worktree Git metadata. The pointer carries the
workspace compatibility coordinate even when that payload is absent. Selected
identity fallback and the all-extant disposable guard remain distinct contracts.
These ordinary-repository cases complement the linked-worktree isolation suite.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from gr2.python_cli.commit import CommitError, _refuse_review_ephemeral_repo
from gr2.python_cli.push import PushError, _refuse_review_ephemeral_repo as refuse_review_ephemeral_push
from gr2.python_cli.review import ReviewError, close_review_lane, open_review_lane
from gr2.python_cli.review_records import (
    lane_paths_for_repo,
    legacy_review_record_path,
    read_review_record,
    read_review_record_at,
    review_record_pointer_path,
    review_record_paths,
)


def _run(cwd: Path, *args: str) -> str:
    p = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, check=True)
    return p.stdout.strip()


def _world(tmp_path: Path) -> dict:
    """A source repo with a seeded PR head, plus the lane destination."""
    origin = tmp_path / "grip.git"
    _run(tmp_path, "init", "--bare", "-b", "main", str(origin))
    source = tmp_path / "grip"
    _run(tmp_path, "clone", "--quiet", str(origin), str(source))
    _run(source, "config", "user.email", "t@t")
    _run(source, "config", "user.name", "t")
    (source / "README.md").write_text("seed\n")
    _run(source, "add", ".")
    _run(source, "commit", "-q", "-m", "initial")
    _run(source, "push", "-q", "origin", "main")
    _run(source, "checkout", "-q", "-b", "pr/7")
    (source / "work.txt").write_text("pr work\n")
    _run(source, "add", ".")
    _run(source, "commit", "-q", "-m", "work")
    head = _run(source, "rev-parse", "HEAD")
    _run(source, "checkout", "-q", "main")
    ws = tmp_path / "ws"
    ws.mkdir()
    lane_root = ws / ".grip" / "state" / "lanes" / "atlas" / "review-7"
    return {"source": source, "head": head, "base": _run(source, "rev-parse", "main"),
            "lane": lane_root / "repos" / "grip", "lane_root": lane_root, "ws": ws}


def _open(w) -> None:
    open_review_lane(
        source_repo_root=w["source"], review_branch="pr/7", expected_head_sha=w["head"],
        base_sha=w["base"], lane_repo_root=w["lane"], workspace_root=w["ws"],
        owner_unit="atlas", lane_name="review-7", member="grip", allow_local=True,
    )


def _close(w) -> None:
    close_review_lane(
        lane_repo_root=w["lane"], review_lane_root=w["lane_root"], workspace_root=w["ws"],
        owner_unit="atlas", lane_name="review-7", member="grip", echo=lambda _l: None,
    )


def _ephemeral_receipt(lane: Path) -> None:
    legacy = legacy_review_record_path(lane)
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"repo": "whatever", "base": "0" * 40, "head": "0" * 40,
                                  "lane_kind": "review-ephemeral"}, indent=2) + "\n")


def _assert_disposable_guards_refuse(lane: Path) -> None:
    with pytest.raises(CommitError, match="is a review-ephemeral review lane"):
        _refuse_review_ephemeral_repo(lane)
    with pytest.raises(PushError, match="is a review-ephemeral review lane"):
        refuse_review_ephemeral_push(lane)


# --------------------------------------------------------------------------- #
# THE ACCEPTANCE CASES
# --------------------------------------------------------------------------- #
def test_D_guard_fires_on_the_project_review_lane_layout(tmp_path):
    """Pre-pointer project layout discovers workspace compatibility evidence."""
    ws = tmp_path / "ws"
    lane = ws / "reviews" / "atlas" / "review-7" / "repos" / "grip"
    lane.mkdir(parents=True)
    _run(lane, "init")
    receipt = ws / ".grip" / "state" / "reviews" / "atlas" / "review-7" / "grip.json"
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"repo": "whatever", "base": "0" * 40, "head": "0" * 40,
                                   "lane_kind": "review-ephemeral"}, indent=2) + "\n")

    assert not legacy_review_record_path(lane).exists()
    assert not review_record_pointer_path(lane).exists()
    # Pre-pointer compatibility is a separate discovery branch.
    by_coord = review_record_paths(ws, "atlas", "review-7", "grip", lane)
    assert read_review_record(by_coord, notice=lambda _m: None) is not None, (
        "fixture: the workspace compatibility receipt must be readable"
    )
    _assert_disposable_guards_refuse(lane)


def test_B_guard_fires_on_a_legacy_shaped_lane(tmp_path):
    """B: the guard, for a lane opened before the receipt moved: receipt in the member .git."""
    w = _world(tmp_path)
    _open(w)
    review_record_pointer_path(w["lane"]).unlink()
    assert not review_record_paths(w["ws"], "atlas", "review-7", "grip", w["lane"]).legacy.exists()
    _ephemeral_receipt(w["lane"])
    _assert_disposable_guards_refuse(w["lane"])


def test_A_close_succeeds_for_a_legacy_only_receipt(tmp_path):
    """A workspace compatibility-only receipt still authorizes owned close."""
    w = _world(tmp_path)
    _open(w)
    paths = review_record_paths(w["ws"], "atlas", "review-7", "grip", w["lane"])

    legacy = paths.legacy
    assert legacy == w["ws"] / ".grip" / "state" / "reviews" / "atlas" / "review-7" / "grip.json"
    assert paths.current == w["lane"] / ".git" / "grip-review.json"
    assert review_record_pointer_path(w["lane"]).read_bytes() == (str(legacy) + "\n").encode()
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_bytes(paths.current.read_bytes())
    paths.current.unlink()
    assert legacy.is_file() and not paths.current.exists(), "fixture: legacy-only state"

    assert read_review_record_at(paths, notice=lambda _m: None)[1] == legacy
    _close(w)  # must not raise

    assert not w["lane"].exists(), "the lane should be gone after a successful close"
    assert not paths.current.exists() and not legacy.exists(), (
        "a receipt outlived the lane it described"
    )


# --------------------------------------------------------------------------- #
# THE TWO CONTROLS -- without these, deleting the guard or neutering close
# would satisfy the three cases above.
# --------------------------------------------------------------------------- #
def test_C_control_the_guard_still_fires_on_a_canonical_lane(tmp_path):
    """CONTROL for B and D: the guard works where the locator can match."""
    ws = tmp_path / "ws"
    lane = ws / ".grip" / "state" / "lanes" / "atlas" / "review-7" / "repos" / "grip"
    lane.mkdir(parents=True)
    _run(lane, "init")
    receipt = review_record_paths(ws, "atlas", "review-7", "grip", lane).current
    receipt.parent.mkdir(parents=True, exist_ok=True)
    receipt.write_text(json.dumps({"repo": "whatever", "base": "0" * 40, "head": "0" * 40,
                                   "lane_kind": "review-ephemeral"}, indent=2) + "\n")
    _assert_disposable_guards_refuse(lane)


def test_control_close_still_works_on_a_canonical_receipt(tmp_path):
    """CONTROL for A: the same lane with a canonical receipt closes cleanly."""
    w = _world(tmp_path)
    _open(w)
    canonical = review_record_paths(w["ws"], "atlas", "review-7", "grip", w["lane"]).current
    assert canonical.is_file(), f"fixture: canonical receipt expected at {canonical}"
    _close(w)
    assert not w["lane"].exists(), (
        "control: close must still remove a lane whose receipt is canonical -- if this "
        "fails, A is not measuring the legacy path"
    )


@pytest.mark.parametrize(
    ("canonical_kind", "legacy_kind"),
    [("review-ephemeral", "materialized"), ("materialized", "review-ephemeral")],
)
def test_any_ephemeral_receipt_refuses_when_receipts_disagree(
        tmp_path, canonical_kind, legacy_kind):
    """Disagreeing active and compatibility evidence refuses before kind selection."""
    w = _world(tmp_path)
    _open(w)
    paths = review_record_paths(w["ws"], "atlas", "review-7", "grip", w["lane"])
    canonical = json.loads(paths.current.read_text())
    canonical["lane_kind"] = canonical_kind
    paths.current.write_text(json.dumps(canonical) + "\n")
    legacy = dict(canonical)
    legacy["lane_kind"] = legacy_kind
    paths.legacy.parent.mkdir(parents=True, exist_ok=True)
    paths.legacy.write_text(json.dumps(legacy) + "\n")
    before = (paths.current.read_bytes(), paths.legacy.read_bytes())
    with pytest.raises(CommitError, match="safety evidence conflict"):
        _refuse_review_ephemeral_repo(w["lane"])
    with pytest.raises(PushError, match="safety evidence conflict"):
        refuse_review_ephemeral_push(w["lane"])
    assert (paths.current.read_bytes(), paths.legacy.read_bytes()) == before


@pytest.mark.parametrize("receipt", [None, "{"])
def test_pointer_without_a_readable_receipt_refuses_commit(tmp_path, receipt):
    """Missing or corrupt compatibility payload with no active Git record refuses."""
    ws = tmp_path / "ws"
    repo = ws / ".grip" / "state" / "lanes" / "atlas" / "review-7" / "repos" / "grip"
    repo.mkdir(parents=True)
    _run(repo, "init", "-q")
    target = ws / ".grip" / "state" / "reviews" / "atlas" / "review-7" / "grip.json"
    paths = review_record_paths(ws, "atlas", "review-7", "grip", repo)
    assert paths.legacy == target and not paths.current.exists()
    target.parent.mkdir(parents=True)
    if receipt is not None:
        target.write_text(receipt)
    review_record_pointer_path(repo).write_text(str(target) + "\n")
    reason = "no readable receipt" if receipt is None else "review receipt cannot be read"
    with pytest.raises(CommitError, match=reason):
        _refuse_review_ephemeral_repo(repo)
    with pytest.raises(PushError, match=reason):
        refuse_review_ephemeral_push(repo)
    target.write_text(json.dumps({"lane_kind": "materialized"}) + "\n")
    _refuse_review_ephemeral_repo(repo)
    refuse_review_ephemeral_push(repo)


def test_pointer_with_unsafe_coordinate_refuses_commit(tmp_path):
    repo = tmp_path / "repo"; _run(tmp_path, "init", "-q", str(repo))
    review_record_pointer_path(repo).write_text(str(tmp_path / "ws" / ".grip" / "state" / "reviews" / ".." / "x.json") + "\n")
    with pytest.raises(CommitError, match="unsafe|safely"):
        _refuse_review_ephemeral_repo(repo)


def test_equal_active_and_workspace_materialized_evidence_allows(tmp_path):
    w = _world(tmp_path)
    _open(w)
    paths = review_record_paths(w["ws"], "atlas", "review-7", "grip", w["lane"])
    paths.legacy.parent.mkdir(parents=True, exist_ok=True)
    paths.legacy.write_bytes(paths.current.read_bytes())
    assert paths.current != paths.legacy
    _refuse_review_ephemeral_repo(w["lane"])
    refuse_review_ephemeral_push(w["lane"])
