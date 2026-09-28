"""R2 acceptance suite for the native-store review-record move.

Five cases, and they are deliberately split so a green means something.

THREE OF THEM FAIL ON THE PRE-FIX RANGE and are the acceptance bar for v3:

  D  the review-ephemeral commit guard, on the layout project_review.py:286
     actually creates -- the receipt is written canonically to
     .grip/state/reviews/<owner>/<lane>/<member>.json while the locator
     walks the repo's own path, and the walk cannot match that layout.
  B  the same guard on a legacy-shaped lane whose receipt is at the
     member-.git location.
  A  close on a lane whose receipt is ONLY at the legacy member-.git location,
     which today refuses because cleanup unlinks `paths.current` -- a path that
     does not exist in that state.

TWO OF THEM PASS ON BOTH SIDES and exist to stop the three above from being
satisfiable the lazy way:

  C  on a CANONICAL lane the guard DOES refuse, so B and D are failures of
     location, not of a guard that never worked.
  A-control  the same lane with a canonical receipt closes cleanly, so A is a
     failure of the legacy path, not of close.

Without C and A-control, deleting the guard outright -- or making close always
succeed -- would turn the suite green while destroying the behaviour it is
supposed to protect. That is the whole reason they are here.

F2 writes a one-line pointer in the member `.git` naming the workspace
coordinate, keeping the canonical receipt and restoring a locator that cannot
drift from it; F1 unlinks the path it actually reads.
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
    lane_root = tmp_path / "lane"
    ws = tmp_path / "ws"
    ws.mkdir()
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


def _guard_refused(lane: Path) -> bool:
    try:
        _refuse_review_ephemeral_repo(lane)
        return False
    except CommitError:
        return True


# --------------------------------------------------------------------------- #
# THE ACCEPTANCE CASES
# --------------------------------------------------------------------------- #
def test_D_guard_fires_on_the_project_review_lane_layout(tmp_path):
    """F2, as project_review.py:286/:293 actually build a lane.

    The receipt is written to the CANONICAL coordinate while the lane lives
    under <ws>/reviews/<owner>/<lane>/repos/<key>, so a locator that walks the
    repo path finds nothing. Reachable by default: open_gr_review.py sets
    ephemeral=True when sources is None.
    """
    ws = tmp_path / "ws"
    lane = ws / "reviews" / "atlas" / "review-7" / "repos" / "grip"
    lane.mkdir(parents=True)
    receipt = review_record_paths(ws, "atlas", "review-7", "grip", lane).current
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"repo": "whatever", "base": "0" * 40, "head": "0" * 40,
                                   "lane_kind": "review-ephemeral"}, indent=2) + "\n")

    # Precondition, so a failure below cannot be blamed on a missing receipt.
    by_coord = review_record_paths(ws, "atlas", "review-7", "grip", lane)
    assert read_review_record(by_coord, notice=lambda _m: None) is not None, (
        "fixture: the receipt must be readable by its canonical coordinate"
    )
    assert _guard_refused(lane), (
        f"the review-ephemeral guard did not fire on the project-review layout "
        f"(lane_paths_for_repo -> {lane_paths_for_repo(lane)}); a read-only disposable "
        "review lane is open to commit and push"
    )


def test_B_guard_fires_on_a_legacy_shaped_lane(tmp_path):
    """F2, for a lane opened before the receipt moved: receipt in the member .git."""
    w = _world(tmp_path)
    _open(w)
    _ephemeral_receipt(w["lane"])
    assert _guard_refused(w["lane"]), (
        "the review-ephemeral guard did not fire although the legacy receipt says "
        "review-ephemeral"
    )


def test_A_close_succeeds_for_a_legacy_only_receipt(tmp_path):
    """F1: a lane whose receipt is only at the legacy location must still close.

    Today this refuses: read_review_record falls back to the legacy path and
    returns the record, then cleanup unlinks `paths.current`, which does not
    exist, so the OSError branch refuses to delete. The code's own stated
    concern is that no receipt outlives its lane, so after a successful close
    neither location may hold one.
    """
    w = _world(tmp_path)
    _open(w)
    paths = review_record_paths(w["ws"], "atlas", "review-7", "grip", w["lane"])

    legacy = legacy_review_record_path(w["lane"])
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(paths.current.read_text())
    paths.current.unlink()
    assert legacy.is_file() and not paths.current.exists(), "fixture: legacy-only state"

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
    receipt = review_record_paths(ws, "atlas", "review-7", "grip", lane).current
    receipt.parent.mkdir(parents=True)
    receipt.write_text(json.dumps({"repo": "whatever", "base": "0" * 40, "head": "0" * 40,
                                   "lane_kind": "review-ephemeral"}, indent=2) + "\n")
    assert _guard_refused(lane), (
        "control: on a canonical lane the guard is expected to fire -- if this fails, "
        "B and D are not measuring a locator defect"
    )


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
    """A receipt preference selects cleanup identity, never the safety verdict.

    Both directions matter. Preferring either canonical or legacy would leave one
    row open to commit and push. Restoring either preference instead of the union
    makes the corresponding row red.
    """
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
    chosen = read_review_record_at(paths, notice=lambda _m: None)
    assert chosen is not None and chosen[1] == paths.legacy, (
        "the legacy receipt remains the selected identity and cleanup path"
    )

    with pytest.raises(CommitError, match="review-ephemeral"):
        _refuse_review_ephemeral_repo(w["lane"])
    with pytest.raises(PushError, match="review-ephemeral"):
        refuse_review_ephemeral_push(w["lane"])


@pytest.mark.parametrize("receipt", [None, "{"])
def test_pointer_without_a_readable_receipt_refuses_commit(tmp_path, receipt):
    """A pointer naming the canonical coordinate with NO receipt there, and with a
    CORRUPT one, must both refuse.

    The target is DERIVED, not spelled. A literal coordinate silently changes what
    this row proves the moment the receipts move: the pointer would then be rejected
    as a non-canonical marker rather than as a missing or corrupt receipt, and the
    row would keep PASSING for the wrong reason -- a degraded witness.
    """
    repo = tmp_path / "repo"; _run(tmp_path, "init", "-q", str(repo))
    target = review_record_paths(tmp_path / "ws", "atlas", "review-7", "grip", repo).current
    target.parent.mkdir(parents=True)
    if receipt is not None:
        target.write_text(receipt)
    review_record_pointer_path(repo).write_text(str(target) + "\n")
    with pytest.raises(CommitError, match="pointer|safely"):
        _refuse_review_ephemeral_repo(repo)


def test_pointer_with_unsafe_coordinate_refuses_commit(tmp_path):
    repo = tmp_path / "repo"; _run(tmp_path, "init", "-q", str(repo))
    review_record_pointer_path(repo).write_text(str(tmp_path / "ws" / ".grip" / "state" / "reviews" / ".." / "x.json") + "\n")
    with pytest.raises(CommitError, match="unsafe|safely"):
        _refuse_review_ephemeral_repo(repo)
