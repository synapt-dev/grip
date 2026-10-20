"""`review open-project` right after `create-project`, before the lane head is
pushed, must refuse with the engine's reason, not exit 1 with nothing printed.

The engine raises ``OpenGrReviewError`` ("review mirror ... does not carry head
...: publish the pinned head"); the CLI glue let it escape, so the reader saw
an empty exit 1 and never learned to publish the head.
"""
from __future__ import annotations

from tests.native_root_helper import native_root

import argparse
from pathlib import Path

import gr2
from typer.testing import CliRunner

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import app as gr2_app
from gr2.python_cli import commit as commit_ops

from .test_review_bind_fork_base import _git, _source_repo
from .test_review_create_project_cli import _gr_sha

runner = CliRunner()

OWNER = "owner"


def _materialized_lane(tmp_path: Path, repos: list[str], lane: str = "feature") -> Path:
    ws = tmp_path / "ws"
    grip_dir = ws / ".grip"
    grip_dir.mkdir(parents=True)
    native_root(ws)
    repo_blocks = "".join(
        f'\n[[repos]]\nname = "{r}"\npath = "repos/{r}"\nurl = "https://example.invalid/{r}.git"\n'
        for r in repos
    )
    (grip_dir / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m"\n{repo_blocks}\n'
        f'[[units]]\nname = "{OWNER}"\npath = "agents/{OWNER}"\nrepos = {repos!r}\n'.replace("'", '"')
    )
    tips: dict[str, str] = {}
    for r in repos:
        _, tips[r] = _source_repo(tmp_path / "src" / r)
    ns = argparse.Namespace(
        workspace_root=ws, owner_unit=OWNER, lane_name=lane, type="feature",
        repos=",".join(repos), branch=",".join(f"{r}=main" for r in repos),
        source="test", default_commands=[],
    )
    ns.fork_base = {r: {"branch": "main", "sha": tips[r]} for r in repos}
    assert lanes.create_lane(ns) == 0
    lane_root = ws / "agents" / OWNER / "lanes" / lane
    (lane_root / "repos").mkdir(parents=True)
    for r in repos:
        _git(lane_root / "repos", "clone", "-q", str(tmp_path / "src" / r), r)
        repo = lane_root / "repos" / r
        _git(repo, "config", "user.name", "Test")
        _git(repo, "config", "user.email", "test@example.com")
    return ws


def _commit_lane_change(ws: Path, repos: list[str], lane: str = "feature") -> None:
    lane_root = ws / "agents" / OWNER / "lanes" / lane
    for r in repos:
        (lane_root / "repos" / r / "new0.txt").write_text("x0\n")
        _git(lane_root / "repos" / r, "add", "new0.txt")
    report = commit_ops.commit_lane(ws, OWNER, "lane work 0", lane_name=lane)
    assert not report.any_failed and report.any_committed


def test_open_project_on_an_unpublished_head_prints_the_refusal(tmp_path: Path) -> None:
    print("gr2.__path__", list(gr2.__path__))
    ws = _materialized_lane(tmp_path, ["a"])
    # The recorded remote is the real source repo: it holds the base, but the lane
    # commit below is never pushed to it.
    spec = ws / ".grip" / "workspace_spec.toml"
    spec.write_text(spec.read_text().replace("https://example.invalid/a.git", str(tmp_path / "src" / "a")))
    _commit_lane_change(ws, ["a"])
    created = runner.invoke(gr2_app.app, ["review", "create-project", str(ws), OWNER, "feature"])
    assert created.exit_code == 0, created.output
    sha = _gr_sha(created.output)
    result = runner.invoke(
        gr2_app.app,
        ["review", "open-project", str(ws), "gr:" + sha, OWNER, "rv", "--enter", "--allow-local"],
    )
    print("rc", result.exit_code, "output", repr(result.output), "exc", repr(result.exception))
    assert result.exit_code == 2, result.output
    assert "does not carry head" in result.output
    assert "publish the pinned head" in result.output
