"""The CLI `lane create` records a fork base, so `review create-project` works for a
stranger.

`create_lane` records only the fork base the caller supplies (record, do not
derive). The CLI `lane create` clones each repo AFTER the doc is
written, so nothing supplied a fork base and every CLI-created lane had none — which
made `review create-project` refuse with "no recorded fork base" for anyone who did
not set it by hand (Fathom, driving the R2 producer verb from help text). The CLI now
records the materialization point (the branch each repo forked from and the sha it
started at) after cloning. These tests drive the real CLI, not create_lane directly.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import app as gr2_app
from gr2.python_cli import grip

runner = CliRunner()


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _source_repo(tmp_path: Path, ws: Path, name: str) -> tuple[str, str]:
    """A source checkout at ws/repos/<name> whose origin is a local bare repo (a
    cloneable, filesystem-identity source — the materialize path clones the origin).
    Returns (bare_url, main_sha)."""
    origin = tmp_path / f"{name}.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    src = ws / "repos" / name
    src.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", str(origin), str(src))
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "f.txt").write_text("base\n")
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "base")
    _git(src, "push", "-q", "origin", "main")
    return str(origin), _git(src, "rev-parse", "HEAD")


def _workspace(tmp_path: Path, repos: list[str]) -> tuple[Path, dict[str, str]]:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    # the grip object store (create-project writes the review-kind commit here)
    _git(ws / ".grip", "init", "-q", "-b", "main")
    _git(ws / ".grip", "config", "user.email", "g@e.invalid")
    _git(ws / ".grip", "config", "user.name", "g")
    _git(ws / ".grip", "commit", "-q", "--allow-empty", "-m", "init grip")
    tips: dict[str, str] = {}
    urls: dict[str, str] = {}
    for r in repos:
        urls[r], tips[r] = _source_repo(tmp_path, ws, r)
    blocks = "".join(
        f'\n[[repos]]\nname = "{r}"\npath = "repos/{r}"\nurl = "{urls[r]}"\n'
        for r in repos
    )
    (ws / ".grip" / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m"\n{blocks}\n'
        f'[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = {repos!r}\n'.replace("'", '"')
    )
    return ws, tips


def _workspace_with_blocked_projection(tmp_path: Path) -> tuple[Path, str]:
    """A one-repo workspace whose repo carries a `.gr2/hooks.toml` link projection
    whose source does not exist. `lane create` materializes the checkout, then the
    projection blocks (HookRuntimeError -> exit 1). Returns (ws, materialization sha)."""
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    _git(ws / ".grip", "init", "-q", "-b", "main")
    _git(ws / ".grip", "config", "user.email", "g@e.invalid")
    _git(ws / ".grip", "config", "user.name", "g")
    _git(ws / ".grip", "commit", "-q", "--allow-empty", "-m", "init grip")

    origin = tmp_path / "app.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    src = ws / "repos" / "app"
    src.parent.mkdir(parents=True, exist_ok=True)
    _git(tmp_path, "clone", "-q", str(origin), str(src))
    _git(src, "config", "user.email", "t@e.invalid")
    _git(src, "config", "user.name", "t")
    (src / "f.txt").write_text("base\n")
    (src / ".gr2").mkdir()
    # a link projection whose source is absent in the materialized checkout -> blocked
    (src / ".gr2" / "hooks.toml").write_text(
        '[[files.link]]\nsrc = "does/not/exist.md"\ndest = "PROJECTED.md"\n'
    )
    _git(src, "add", ".")
    _git(src, "commit", "-q", "-m", "base with a blocked projection hook")
    _git(src, "push", "-q", "origin", "main")
    tip = _git(src, "rev-parse", "HEAD")
    (ws / ".grip" / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m"\n'
        f'[[repos]]\nname = "app"\npath = "repos/app"\nurl = "{origin}"\n'
        f'[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = ["app"]\n'
    )
    return ws, tip


def test_cli_lane_create_records_fork_base_before_a_blocked_hook_exits(tmp_path: Path) -> None:
    # fork-base-before-hooks: a projection hook that blocks (source missing) exits 1
    # AFTER the checkout exists. The fork base must still be recorded, so the lane is
    # not a half lane that `review create-project` refuses for a missing fork base.
    ws, tip = _workspace_with_blocked_projection(tmp_path)
    res = runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "feature",
                                      "--repos", "app", "--branch", "main"])
    # the hook block still surfaces as a non-zero exit (kept, not swallowed)
    assert res.exit_code == 1, res.output
    # ...but the fork base was recorded as soon as the checkout existed
    doc = lanes.load_lane_doc(ws, "atlas", "feature")
    assert "fork_base" in doc, "a blocked hook must not leave a lane without a fork base"
    assert doc["fork_base"]["app"]["branch"] == "main"
    assert doc["fork_base"]["app"]["sha"] == tip
    # ...and the R2 producer verb succeeds on the recovered lane rather than refusing
    created = runner.invoke(gr2_app.app, ["review", "create-project", str(ws), "atlas", "feature"])
    assert created.exit_code == 0, created.output


def test_cli_lane_create_records_fork_base_for_each_repo(tmp_path: Path) -> None:
    ws, tips = _workspace(tmp_path, ["app", "lib"])
    res = runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "feature",
                                      "--repos", "app,lib", "--branch", "main"])
    assert res.exit_code == 0, res.output
    doc = lanes.load_lane_doc(ws, "atlas", "feature")
    assert "fork_base" in doc, "CLI-created lane must record a fork base"
    for r in ("app", "lib"):
        assert doc["fork_base"][r]["branch"] == "main"
        assert doc["fork_base"][r]["sha"] == tips[r]  # the materialization point


def test_cli_created_lane_then_create_project_succeeds(tmp_path: Path) -> None:
    # The stranger path end to end: create a lane through the CLI, then the R2 producer
    # verb pins base..head instead of refusing on a missing fork base.
    ws, _ = _workspace(tmp_path, ["app", "lib"])
    assert runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "feature",
                                       "--repos", "app,lib", "--branch", "main"]).exit_code == 0
    res = runner.invoke(gr2_app.app, ["review", "create-project", str(ws), "atlas", "feature"])
    assert res.exit_code == 0, res.output
    sha = next(l for l in res.output.splitlines() if l.startswith("gr:"))[3:].strip()
    rows = {r["key"]: r for r in grip.read_project_review_commit(ws, sha)}
    assert set(rows) == {"app", "lib"}


def test_cli_create_project_carry_range_records_the_range(tmp_path: Path) -> None:
    # `create-project --carry-range` captures each repo's base..head range from the
    # lane and records it INSIDE the gr commit (self-describing), so a reviewer with
    # neither the pre-push head nor the lane can reconstruct it. Without the flag the
    # commit carries no range.
    ws, _tips = _workspace(tmp_path, ["app"])
    assert runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "feature",
                                       "--repos", "app", "--branch", "main"]).exit_code == 0
    lane_repo = lanes.lane_dir(ws, "atlas", "feature") / "repos" / "app"
    (lane_repo / "change.txt").write_text("lane change\n")
    _git(lane_repo, "config", "user.email", "t@e.invalid")
    _git(lane_repo, "config", "user.name", "t")
    _git(lane_repo, "add", ".")
    _git(lane_repo, "commit", "-q", "-m", "lane change")

    res = runner.invoke(gr2_app.app, ["review", "create-project", str(ws), "atlas", "feature", "--carry-range"])
    assert res.exit_code == 0, res.output
    sha = next(l for l in res.output.splitlines() if l.startswith("gr:"))[3:].strip()
    assert "app" in grip.project_review_carried_keys(ws, sha)

    # the plain (no --carry-range) commit carries no range object.
    plain = runner.invoke(gr2_app.app, ["review", "create-project", str(ws), "atlas", "feature"])
    assert plain.exit_code == 0, plain.output
    plain_sha = next(l for l in plain.output.splitlines() if l.startswith("gr:"))[3:].strip()
    assert grip.project_review_carried_keys(ws, plain_sha) == set()


def test_cli_carry_range_reconstructs_the_exact_pinned_sha(tmp_path: Path) -> None:
    # Row 2 through the CLI producer: `create-project --carry-range` also captures each
    # commit's committer identity+date, so reconstruction is SHA-faithful -- the rebuilt
    # head equals the pinned pre-push head, not merely its tree. (Range-1 behavior, with
    # the committer re-stamped, would give a different sha.)
    ws, _tips = _workspace(tmp_path, ["app"])
    assert runner.invoke(gr2_app.app, ["lane", "create", str(ws), "atlas", "feature",
                                       "--repos", "app", "--branch", "main"]).exit_code == 0
    lane_repo = lanes.lane_dir(ws, "atlas", "feature") / "repos" / "app"
    _git(lane_repo, "config", "user.email", "dev@layne.pro")
    _git(lane_repo, "config", "user.name", "Layne Penney")
    (lane_repo / "change.txt").write_text("lane change\n")
    _git(lane_repo, "add", ".")
    _git(lane_repo, "commit", "-q", "-m", "lane change")
    pinned_head = _git(lane_repo, "rev-parse", "HEAD")

    res = runner.invoke(gr2_app.app, ["review", "create-project", str(ws), "atlas", "feature", "--carry-range"])
    assert res.exit_code == 0, res.output
    sha = next(l for l in res.output.splitlines() if l.startswith("gr:"))[3:].strip()
    # the objects subtree carries committer metadata, and the pin's remote holds base.
    obj_names = grip._grip_git(ws, "ls-tree", "--name-only", f"{sha}:objects/app").stdout.split()
    assert "committers" in obj_names

    result = grip.reconstruct_project_review_lane(ws, sha, "app", tmp_path / "recon" / "app")
    assert result["reconstructed_head"] == pinned_head  # SHA-faithful, not merely tree-equal
    assert result["bound_head"] == pinned_head
