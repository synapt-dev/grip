"""kind=workspace gr commit: a materialized lane's resolved heads captured as
one gr commit and read back (the section-5 workspace kind).

Base is the RECORDED fork base, never derived from HEAD^: the lane
records where its work forked from its integration branch at create time, and the
snapshot reads that coordinate. A lane with no recorded fork base is refused."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

import pytest

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import commit as commit_ops
from gr2.python_cli import grip
from gr2.python_cli import workspace_snapshot as ws_snap


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=repo, check=check, capture_output=True, text=True)


def _source_repo(path: Path) -> tuple[Path, str]:
    """A source repo with integration branch 'main' at a known tip.

    Models the integration branch a lane forks from: its tip is resolvable BEFORE
    the lane's own clone is materialized, which is why the fork base can be
    recorded at lane create. Returns (path, tip_sha).
    """
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
    """A materialized lane whose repos are clones of a source integration branch.

    When ``fork_base`` is True the lane records, per repo, the source tip it forked
    from (the coordinate the snapshot reads as base). When False the lane carries no
    fork base — a pre-field lane the snapshot must refuse.
    """
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


def test_snapshot_lane_round_trips_resolved_heads(tmp_path: Path) -> None:
    ws = _materialized_lane(tmp_path, ["a", "b"])
    _commit_lane_change(ws, ["a", "b"])
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    doc = lanes.load_lane_doc(ws, "atlas", "feature")

    commit = ws_snap.snapshot_lane(ws, "atlas", "feature")
    rows = {r["key"]: r for r in ws_snap.read_snapshot(ws, commit)}

    assert set(rows) == {"a", "b"}
    for r in ("a", "b"):
        repo = lane_root / "repos" / r
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        assert rows[r]["commit"] == head
        assert rows[r]["base"] == doc["fork_base"][r]["sha"]  # the recorded fork base
        assert rows[r]["remote"] == f"https://example.invalid/{r}.git"
        assert rows[r]["path"] == f"repos/{r}"
    # The read-back kind is strictly workspace.
    assert _git(ws / ".grip", "show", f"{commit}:.grip/kind").stdout.strip() == "workspace"


def test_snapshot_base_is_the_recorded_fork_base_not_head_parent(tmp_path: Path) -> None:
    # Fork-base proof: with TWO lane commits, HEAD^ is the first lane commit while
    # the fork base is where the lane began — they differ. base must be the recorded
    # fork base, never HEAD^. A mutation that recomputes base from HEAD^ reds this.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    _commit_lane_change(ws, ["a", "b"], times=2)
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    doc = lanes.load_lane_doc(ws, "atlas", "feature")

    commit = ws_snap.snapshot_lane(ws, "atlas", "feature")
    rows = {r["key"]: r for r in ws_snap.read_snapshot(ws, commit)}
    for r in ("a", "b"):
        repo = lane_root / "repos" / r
        head = _git(repo, "rev-parse", "HEAD").stdout.strip()
        head_parent = _git(repo, "rev-parse", "HEAD^").stdout.strip()
        recorded = doc["fork_base"][r]["sha"]
        assert recorded != head_parent  # fixture sanity: two commits, so they differ
        assert rows[r]["commit"] == head
        assert rows[r]["base"] == recorded
        assert rows[r]["base"] != head_parent


def test_snapshot_refuses_a_lane_with_no_recorded_fork_base(tmp_path: Path) -> None:
    # A pre-field lane reads base as unknown and is REFUSED — never HEAD^.
    ws = _materialized_lane(tmp_path, ["a", "b"], fork_base=False)
    _commit_lane_change(ws, ["a", "b"])
    doc = lanes.load_lane_doc(ws, "atlas", "feature")
    assert "fork_base" not in doc  # genuinely pre-field
    with pytest.raises(ws_snap.WorkspaceSnapshotError, match="no recorded fork base"):
        ws_snap.snapshot_lane(ws, "atlas", "feature")


def test_snapshot_refuses_a_fork_base_not_ancestor_of_head(tmp_path: Path) -> None:
    # A recorded fork base must be a real commit AND an ancestor of HEAD, or it is
    # not a coordinate HEAD builds on. A bogus 40-hex sha is refused, not recorded.
    ws = _materialized_lane(tmp_path, ["a", "b"], fork_base=False)
    _commit_lane_change(ws, ["a", "b"])
    lane_file = lanes.lane_file(ws, "atlas", "feature")
    bogus = "f" * 40
    lane_file.write_text(
        lane_file.read_text()
        + f'\n[fork_base.a]\nbranch = "main"\nsha = "{bogus}"\n'
        + f'[fork_base.b]\nbranch = "main"\nsha = "{bogus}"\n'
    )
    with pytest.raises(ws_snap.WorkspaceSnapshotError, match="not a commit in this repo"):
        ws_snap.snapshot_lane(ws, "atlas", "feature")


def test_read_workspace_commit_reports_every_repo_in_the_tree(tmp_path: Path) -> None:
    # Mutation target: a create that drops one repo from the tree reds this.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    _commit_lane_change(ws, ["a", "b"])
    commit = ws_snap.snapshot_lane(ws, "atlas", "feature")
    assert {r["key"] for r in ws_snap.read_snapshot(ws, commit)} == {"a", "b"}


def test_snapshot_refuses_a_dirty_repo(tmp_path: Path) -> None:
    # Control: an uncommitted repo is refused, not silently recorded — a
    # workspace commit must reproduce the author's actual state.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    _commit_lane_change(ws, ["a", "b"])
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    (lane_root / "repos" / "b" / "dirty.txt").write_text("uncommitted\n")  # untracked = dirty
    with pytest.raises(ws_snap.WorkspaceSnapshotError, match="uncommitted changes"):
        ws_snap.snapshot_lane(ws, "atlas", "feature")


def test_read_workspace_commit_rejects_a_non_workspace_kind(tmp_path: Path) -> None:
    # Control: the review kind is a different kind; read_workspace_commit refuses it.
    # NOTE: the review kind ALSO carries a different schema, so this case is caught
    # by the schema gate before the kind gate ever runs — see the wrong-kind witness
    # below, which isolates the kind gate specifically.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    _commit_lane_change(ws, ["a", "b"])
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    # A project-review-kind commit IN THE ALPHA STORE this reader reads. `create_project_review_commit`
    # now writes only to a native root's own .git, so the fixture builds the commit by hand.
    review_commit = _write_grip_commit_with_kind(
        ws, _repo_fields(ws, ["a", "b"]), "review", schema=grip._PROJECT_REVIEW_SCHEMA)
    with pytest.raises(grip.GripCorruptError):
        ws_snap.read_snapshot(ws, review_commit)


def _repo_fields(ws: Path, repos: list[str], lane: str = "feature") -> list[dict[str, str]]:
    """Section-5 field dicts (remote/path/commit/base) drawn from a real lane."""
    lane_root = lanes.lane_dir(ws, "atlas", lane)
    return [
        {"key": r, "remote": f"https://example.invalid/{r}.git", "path": f"repos/{r}",
         "commit": _git(lane_root / "repos" / r, "rev-parse", "HEAD").stdout.strip(),
         "base": _git(lane_root / "repos" / r, "rev-parse", "HEAD^").stdout.strip()}
        for r in repos
    ]


def _write_grip_commit_with_kind(
    ws: Path, repos: list[dict[str, str]], kind: str, schema: str | None = None,
) -> str:
    """Build a gr commit through the SAME _mktree/_hash_blob seam create_workspace_commit
    uses, but with the workspace SCHEMA and a parameterized kind blob.

    kind="workspace" is a valid snapshot; any other kind produces a tree whose ONLY
    defect is the kind field — the schema gate passes, so read_workspace_commit's kind
    gate is the sole thing that can reject it. This is the witness the review-kind
    control above cannot be (its schema differs, so the schema gate fires first).
    """
    entries: list[str] = []
    for repo in sorted(repos, key=lambda item: item["key"]):
        fields = [
            f"100644 blob {grip._hash_blob(ws, repo[name])}\t{name}"
            for name in ("remote", "path", "commit", "base")
        ]
        entries.append(f"040000 tree {grip._mktree(ws, fields)}\t{repo['key']}")
    repos_tree = grip._mktree(ws, entries)
    meta_tree = grip._mktree(ws, [
        f"100644 blob {grip._hash_blob(ws, schema or grip._WORKSPACE_SCHEMA)}\tschema",
        f"100644 blob {grip._hash_blob(ws, kind)}\tkind",
    ])
    root_tree = grip._mktree(ws, [f"040000 tree {meta_tree}\t.grip", f"040000 tree {repos_tree}\trepos"])
    commit = grip._commit_tree(ws, root_tree, parent=grip._current_head(ws), message="test kind fixture")
    grip._grip_git(ws, "update-ref", "HEAD", commit)
    return commit


def test_read_workspace_commit_rejects_correct_schema_wrong_kind(tmp_path: Path) -> None:
    # WITNESS for the kind gate (grip.read_workspace_commit lines 132-133): a tree
    # with the correct workspace schema but kind="review". Deleting the kind gate
    # reds this test; the review-kind control above stays green regardless, so
    # without this witness the kind gate does nothing measurable.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    _commit_lane_change(ws, ["a", "b"])
    fields = _repo_fields(ws, ["a", "b"])

    wrong = _write_grip_commit_with_kind(ws, fields, "review")
    with pytest.raises(grip.GripCorruptError, match="wrong kind"):
        ws_snap.read_snapshot(ws, wrong)


def test_write_grip_commit_with_kind_workspace_is_readable(tmp_path: Path) -> None:
    # Positive control proving the builder is otherwise sound: the SAME builder with
    # kind="workspace" round-trips. This is what makes the wrong-kind witness above
    # non-vacuous — the rejection there is the kind field, not a malformed tree.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    _commit_lane_change(ws, ["a", "b"])
    fields = _repo_fields(ws, ["a", "b"])

    good = _write_grip_commit_with_kind(ws, fields, "workspace")
    rows = {r["key"]: r for r in ws_snap.read_snapshot(ws, good)}
    assert set(rows) == {"a", "b"}
    for r in ("a", "b"):
        assert rows[r]["remote"] == f"https://example.invalid/{r}.git"
def test_snapshot_refuses_a_real_fork_base_that_is_not_an_ancestor_of_head(tmp_path: Path) -> None:
    # Witness for the ANCESTOR check specifically: a real in-repo commit (rev-parse
    # --verify passes) that is NOT an ancestor of HEAD. The not-a-commit check
    # cannot pre-empt this (a bogus ffff..40 is caught by that earlier check
    # instead), so only the ancestor check can refuse this — removing it reds this.
    ws = _materialized_lane(tmp_path, ["a", "b"], fork_base=False)
    _commit_lane_change(ws, ["a", "b"])
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    repo_a = lane_root / "repos" / "a"
    # A real commit that DESCENDS from HEAD (a child): a reachable object, but not
    # an ancestor of HEAD.
    _git(repo_a, "checkout", "-b", "side")
    (repo_a / "side.txt").write_text("side\n")
    _git(repo_a, "add", "side.txt")
    _git(repo_a, "commit", "-m", "side commit")
    side = _git(repo_a, "rev-parse", "HEAD").stdout.strip()
    _git(repo_a, "checkout", "-")  # back to the lane branch; HEAD is side's parent
    # b gets a VALID ancestor fork base, so a's ancestor check is the sole
    # discriminator: with it removed the snapshot succeeds (DID NOT RAISE), not a
    # different repo's unknown-refusal.
    b_base = _git(lane_root / "repos" / "b", "rev-parse", "HEAD^").stdout.strip()
    lane_file = lanes.lane_file(ws, "atlas", "feature")
    lane_file.write_text(
        lane_file.read_text()
        + f'\n[fork_base.a]\nbranch = "main"\nsha = "{side}"\n'
        + f'[fork_base.b]\nbranch = "main"\nsha = "{b_base}"\n'
    )
    with pytest.raises(ws_snap.WorkspaceSnapshotError, match="not an ancestor"):
        ws_snap.snapshot_lane(ws, "atlas", "feature")
