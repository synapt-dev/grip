"""TDD: the stranger's F2/F3 lane range (grip, from the alpha-2 stranger pass).

The discriminating pair (2026-09-21, dev 4c074c4) settled F2 the clean way:
the lane-aware commit looks in the right place (the lane's own clones under
``.grip/state/lanes/<unit>/<lane>/repos/<repo>``) and commits there. The
defects are upstream of it:

1. No verb tells the actor where a lane's repos are: ``lane create`` printed
   only the lane.toml path, ``lane enter`` printed only the current-lane
   pointer, and the enter receipt JSON carried repo NAMES never a directory.
2. A lane-aware commit whose every repo is skipped (work staged somewhere
   else, e.g. the unit home) exited 0 with no sentence about where it looked.

F3 (same range): ``repo status`` on a single-repo path tracebacked
FileNotFoundError instead of refusing in one sentence.
"""

from __future__ import annotations

import argparse
import json
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import commit as commit_ops  # noqa: F401  (seam under test)
from gr2.python_cli.app import app
from gr2.prototypes import lane_workspace_prototype as lanes

from .test_commit import _init_repo, _materialized_lane, _stage_change

runner = CliRunner()


def _git(repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=False
    )


def _make_workspace_units_home(tmp: Path) -> Path:
    """A workspace whose unit declares the ``home``-shaped path the stranger's
    unit repos actually live under, so the staged-elsewhere probe has the real
    layout to look at. Mirrors what a real ``workspace init`` writes
    (units.path = "agents/<unit>/home")."""
    ws = tmp / "ws"
    (ws / ".grip").mkdir(parents=True)
    (ws / "agents" / "atlas" / "home").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        'schema_version = 1\nworkspace_name = "m"\n'
        '[[repos]]\nname = "a"\npath = "repos/a"\nurl = "https://example.invalid/a.git"\n'
        '[[repos]]\nname = "b"\npath = "repos/b"\nurl = "https://example.invalid/b.git"\n'
        '[[units]]\nname = "atlas"\npath = "agents/atlas/home"\nrepos = ["a", "b"]\n'
    )
    return ws


def _create_materialized_lane_ws(tmp: Path) -> tuple[Path, Path]:
    """(ws, lane_root) with a two-repo materialized lane whose repos/<r> dirs
    are real git repos (the same shape test_commit._materialized_lane builds)."""
    ws = _make_workspace_units_home(tmp)
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    branch = "a=main,b=main"
    assert lanes.create_lane(
        argparse.Namespace(
            workspace_root=ws, owner_unit="atlas", lane_name="feature",
            type="feature", repos="a,b", branch=branch, source="test",
            default_commands=[],
        )
    ) == 0
    for r in ("a", "b"):
        _init_repo(lane_root / "repos" / r)
    return ws, lane_root


def _unit_home_with_staged_change(ws: Path, repo: str) -> Path:
    """A real git checkout at the unit's declared home path with one staged
    change — the stranger's layout, where materialize put the repos and where
    ``add --repo-path`` pointed."""
    home_repo = ws / "agents" / "atlas" / "home" / repo
    home_repo.parent.mkdir(parents=True, exist_ok=True)
    _init_repo(home_repo)
    (home_repo / "new.txt").write_text("x\n")
    _git(home_repo, "add", "new.txt")
    return home_repo


# --- (i) lane create / lane enter name the working directory per repo -------


def test_lane_create_human_line_names_repo_dirs(tmp_path: Path, capsys) -> None:
    ws = _make_workspace_units_home(tmp_path)
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    assert lanes.create_lane(
        argparse.Namespace(
            workspace_root=ws, owner_unit="atlas", lane_name="feature",
            type="feature", repos="a,b", branch="a=main,b=main", source="test",
            default_commands=[],
        )
    ) == 0
    out = capsys.readouterr().out
    # The metadata line exists and each repo's WORKING directory is named,
    # absolute, one per repo.
    assert str(lane_root / "lane.toml") in out
    assert str(lane_root / "repos" / "a") in out
    assert str(lane_root / "repos" / "b") in out


def test_lane_enter_receipt_json_carries_absolute_repo_paths(tmp_path: Path) -> None:
    ws = _materialized_lane(tmp_path, ["a", "b"])
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    lanes.enter_lane(
        argparse.Namespace(
            workspace_root=ws, owner_unit="atlas", lane_name="feature",
            actor="test", notify_channel=False, recall=False,
        )
    )
    receipt = json.loads(lanes.current_lane_file(ws, "atlas").read_text())
    repo_paths = receipt["current"]["repo_paths"]
    assert set(repo_paths) == {"a", "b"}, repo_paths
    for repo, path in repo_paths.items():
        assert Path(path).is_absolute(), path
        assert Path(path) == lane_root / "repos" / repo, path
        assert Path(path).is_dir(), path


def test_lane_enter_human_line_names_repo_paths(tmp_path: Path) -> None:
    ws = _materialized_lane(tmp_path, ["a", "b"])
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    result = runner.invoke(
        app,
        ["lane", "enter", str(ws), "atlas", "feature", "--actor", "test"],
    )
    assert result.exit_code == 0, result.output
    payload = json.loads(result.output)
    assert payload["repo_paths"] == {
        "a": str(lane_root / "repos" / "a"),
        "b": str(lane_root / "repos" / "b"),
    }, payload


# --- (ii) an all-skipped lane commit is a failure with a sentence -----------


def test_all_skipped_commit_exits_nonzero_and_names_lane_dir(tmp_path: Path) -> None:
    ws, lane_root = _create_materialized_lane_ws(tmp_path)
    result = runner.invoke(
        app,
        ["commit", "-m", "m", "--workspace-root", str(ws),
         "--owner-unit", "atlas", "--lane", "feature"],
    )
    assert result.exit_code != 0, result.output
    assert "committed nothing" in result.output, result.output
    assert str(lane_root / "repos") in result.output, result.output


def test_all_skipped_commit_names_staged_changes_in_unit_home(tmp_path: Path) -> None:
    ws, lane_root = _create_materialized_lane_ws(tmp_path)
    staged = _unit_home_with_staged_change(ws, "a")

    result = runner.invoke(
        app,
        ["commit", "-m", "m", "--workspace-root", str(ws),
         "--owner-unit", "atlas", "--lane", "feature"],
    )
    assert result.exit_code != 0, result.output
    # The sentence names the lane dir AND the unit-home path with the staged
    # change, and says it is not the lane's repo.
    assert str(lane_root / "repos") in result.output, result.output
    assert "staged changes found in" in result.output, result.output
    assert str(staged) in result.output, result.output


def test_all_skipped_commit_names_staged_changes_in_workspace_root_copy(tmp_path: Path) -> None:
    # The third place a first-time user might work: the workspace-root copy at
    # the spec repo path (ws/repos/a here). The all-skipped sentence must name
    # it, not only the unit-home copy.
    ws, lane_root = _create_materialized_lane_ws(tmp_path)
    root_repo = ws / "repos" / "a"
    root_repo.parent.mkdir(parents=True, exist_ok=True)
    _init_repo(root_repo)
    (root_repo / "new.txt").write_text("x\n")
    _git(root_repo, "add", "new.txt")

    result = runner.invoke(
        app,
        ["commit", "-m", "m", "--workspace-root", str(ws),
         "--owner-unit", "atlas", "--lane", "feature"],
    )
    assert result.exit_code != 0, result.output
    assert str(lane_root / "repos") in result.output, result.output
    assert "staged changes found in" in result.output, result.output
    assert str(root_repo) in result.output, result.output


def test_partial_skip_stays_exit_zero_and_names_skipped(tmp_path: Path) -> None:
    # Control, unchanged contract: one committed, one skipped → exit 0, the
    # skipped repo still named, no "committed nothing" sentence.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    lane_root = lanes.lane_dir(ws, "atlas", "feature")
    _stage_change(lane_root / "repos" / "a")  # b left empty

    result = runner.invoke(
        app,
        ["commit", "-m", "msg", "--workspace-root", str(ws),
         "--owner-unit", "atlas", "--lane", "feature"],
    )
    assert result.exit_code == 0, result.output
    assert "a: committed" in result.output
    assert "b: skipped (empty index)" in result.output
    assert "committed nothing" not in result.output


# --- (iii) repo status refuses a non-workspace path in one sentence ---------


def test_repo_status_on_repo_path_refuses_one_sentence_no_traceback(tmp_path: Path) -> None:
    repo = _init_repo(tmp_path / "solo-repo")

    result = runner.invoke(app, ["repo", "status", str(repo)])
    assert result.exit_code != 0, result.output
    assert "Traceback" not in result.output, result.output
    assert "FileNotFoundError" not in result.output, result.output
    assert "workspace root" in result.output, result.output
    assert ".grip/workspace_spec.toml" in result.output, result.output


def test_repo_status_on_workspace_root_still_works(tmp_path: Path) -> None:
    # Control: the workspace root path still runs.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    result = runner.invoke(app, ["repo", "status", str(ws)])
    assert result.exit_code == 0, result.output
    assert "Traceback" not in result.output
