"""review-bind reads base from the recorded fork base.

A project review's per-repo base is the point the lane forked from its
integration branch, recorded at lane create and read through the same resolver
the workspace snapshot uses — never HEAD^, never a live merge-base. The
review-kind encode/decode (create/read_project_review_commit) is unchanged; only
where the pins' base comes from changes."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import commit as commit_ops
from gr2.python_cli import grip
from gr2.python_cli import project_review


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _source_repo(path: Path) -> tuple[Path, str]:
    path.mkdir(parents=True)
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    (path / "tracked.txt").write_text("initial\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return path, _git(path, "rev-parse", "HEAD").stdout.strip()


def _workspace(tmp_path: Path, repos: list[str]) -> Path:
    ws = tmp_path / "ws"
    grip_dir = ws / ".grip"
    grip_dir.mkdir(parents=True)
    _git(grip_dir, "init", "-b", "main")
    _git(grip_dir, "config", "user.name", "Grip")
    _git(grip_dir, "config", "user.email", "grip@example.com")
    _git(grip_dir, "commit", "--allow-empty", "-m", "init grip")
    repo_blocks = "".join(
        f'\n[[repos]]\nname = "{r}"\npath = "repos/{r}"\nurl = "https://example.invalid/{r}.git"\n'
        for r in repos
    )
    (grip_dir / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m"\n{repo_blocks}\n'
        f'[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = {repos!r}\n'.replace("'", '"')
    )
    return ws


def _materialized_lane(
    tmp_path: Path, repos: list[str], lane: str = "feature", *, fork_base: bool = True
) -> Path:
    ws = _workspace(tmp_path, repos)
    tips: dict[str, str] = {}
    for r in repos:
        _, tips[r] = _source_repo(tmp_path / "src" / r)
    branch = ",".join(f"{r}=main" for r in repos)
    ns = argparse.Namespace(
        workspace_root=ws, owner_unit="atlas", lane_name=lane, type="feature",
        repos=",".join(repos), branch=branch, source="test", default_commands=[],
    )
    if fork_base:
        ns.fork_base = {r: {"branch": "main", "sha": tips[r]} for r in repos}
    assert lanes.create_lane(ns) == 0
    lane_root = lanes.lane_dir(ws, "atlas", lane)
    for r in repos:
        _git(lane_root / "repos", "clone", "-q", str(tmp_path / "src" / r), r)
        repo = lane_root / "repos" / r
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.com")
    return ws


def _commit_lane_change(ws: Path, repos: list[str], lane: str = "feature", times: int = 1) -> None:
    lane_root = lanes.lane_dir(ws, "atlas", lane)
    for n in range(times):
        for r in repos:
            (lane_root / "repos" / r / f"new{n}.txt").write_text(f"x{n}\n")
            _git(lane_root / "repos" / r, "add", f"new{n}.txt")
        report = commit_ops.commit_lane(ws, "atlas", f"lane work {n}", lane_name=lane)
        assert not report.any_failed and report.any_committed


def test_review_pins_base_is_the_recorded_fork_base_not_head_parent(tmp_path: Path) -> None:
    # Fork-base proof for the review path: with TWO lane commits, HEAD^ is the first
    # lane commit while the fork base is where the lane began — they differ. Each
    # review pin's base must be the recorded fork base, never HEAD^. A mutation that
    # recomputes base from HEAD^ reds this.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    _commit_lane_change(ws, ["a", "b"], times=2)
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    doc = lanes.load_lane_doc(ws, "atlas", "feature")

    pins = {p.key: p for p in project_review.pins_from_lane(ws, "atlas", "feature")}
    assert set(pins) == {"a", "b"}
    for r in ("a", "b"):
        repo = lane_root / "repos" / r
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        head_parent = _git(repo, "rev-parse", "HEAD^").stdout.strip()
        recorded = doc["fork_base"][r]["sha"]
        assert recorded != head_parent  # fixture sanity: two commits, so they differ
        assert pins[r].head == head
        assert pins[r].base == recorded
        assert pins[r].base != head_parent


def test_review_kind_commit_carries_the_recorded_fork_base(tmp_path: Path) -> None:
    # End-to-end: the pins feed the UNCHANGED review-kind encode/decode, and the
    # base read back from the review gr commit is the recorded fork base.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    _commit_lane_change(ws, ["a", "b"], times=2)
    doc = lanes.load_lane_doc(ws, "atlas", "feature")

    spec = project_review.make_spec(ws, project_review.pins_from_lane(ws, "atlas", "feature"))
    rows = {r["key"]: r for r in grip.read_project_review_commit(ws, spec.grip_commit)}
    for r in ("a", "b"):
        assert rows[r]["base"] == doc["fork_base"][r]["sha"]


def test_review_pins_refuse_a_lane_with_no_recorded_fork_base(tmp_path: Path) -> None:
    # A pre-field lane reads base as unknown and is REFUSED — a review can never be
    # bound against a base the lane did not fork from.
    from gr2.python_cli import workspace_snapshot as ws_snap

    ws = _materialized_lane(tmp_path, ["a", "b"], fork_base=False)
    _commit_lane_change(ws, ["a", "b"])
    with pytest.raises(ws_snap.WorkspaceSnapshotError, match="no recorded fork base"):
        project_review.pins_from_lane(ws, "atlas", "feature")
