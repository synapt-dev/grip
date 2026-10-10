"""A temporary lane must be able to end, and ending it must never destroy unlanded work.

Measured on dev 4ada547e: gr2 has `lane create`, `enter`, `exit`, `show`, `resolve`, `bind` and `lease`, and nothing
removes a lane. `lane exit` only leaves it, so every lane's clones stay on disk for good.
`lane remove` ends a lane, and refuses while any of its repos holds work that
exists nowhere else. First row: a local commit that was never pushed.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

from gr2.prototypes import lane_workspace_prototype as lane_proto
from gr2.python_cli import app as gr2_app
from tests.conftest import make_cli_runner
from tests.test_lane_enter_hook_warnings import _workspace_with_enter_hook

runner = make_cli_runner()


def _git(cwd: Path, *a: str) -> str:
    return subprocess.run(["git", *a], cwd=cwd, text=True, capture_output=True, check=True).stdout.strip()


def _exited_lane_with_a_commit(tmp_path: Path) -> tuple[Path, Path, str]:
    """A one-repo lane, entered, one commit made in it, then exited (a clean tree). Returns (ws, lane repo, commit)."""
    ws, _, _ = _workspace_with_enter_hook(tmp_path, command="exit 0")
    for argv in (["lane", "create", str(ws), "atlas", "x", "--repos", "app", "--branch", "app=feat/x"],
                 ["lane", "enter", str(ws), "atlas", "x", "--actor", "agent:s"]):
        result = runner.invoke(gr2_app.app, argv)
        assert result.exit_code == 0, result.output
    repo = gr2_app._lane_repo_root(ws, "atlas", "x", "app")
    _git(repo, "config", "user.email", "t@e.invalid")
    _git(repo, "config", "user.name", "t")
    (repo / "f.txt").write_text("work that exists only here\n")
    _git(repo, "commit", "-q", "-am", "lane work")
    commit = _git(repo, "rev-parse", "HEAD")
    result = runner.invoke(gr2_app.app, ["lane", "exit", str(ws), "atlas", "--actor", "agent:s"])
    assert result.exit_code == 0, result.output
    assert _git(repo, "status", "--porcelain") == ""
    return ws, repo, commit


def _remove(ws: Path):
    return runner.invoke(gr2_app.app, ["lane", "remove", str(ws), "atlas", "x"])


def test_a_lane_holding_an_unpushed_commit_is_not_removed(tmp_path: Path) -> None:
    ws, repo, commit = _exited_lane_with_a_commit(tmp_path)
    assert _git(repo, "log", "HEAD", "--not", "--remotes", "--format=%H") == commit, "the fixture's commit is unpushed"

    result = _remove(ws)

    assert result.exit_code != 0, result.output
    assert "app" in result.output and commit[:12] in result.output, result.output
    assert repo.is_dir() and _git(repo, "rev-parse", "HEAD") == commit, "the lane and its commit are untouched"
    assert lane_proto.lane_file(ws, "atlas", "x").is_file(), "the lane record is untouched"


def test_the_same_lane_once_its_commit_is_pushed_is_removed(tmp_path: Path) -> None:
    ws, repo, commit = _exited_lane_with_a_commit(tmp_path)
    _git(repo, "push", "-q", "origin", "HEAD:refs/heads/feat/x")
    assert _git(repo, "log", "HEAD", "--not", "--remotes", "--format=%H") == "", "control: nothing unpushed"
    member = ws / "repos" / "app"
    member_head = _git(member, "rev-parse", "HEAD")

    result = _remove(ws)

    assert result.exit_code == 0, result.output
    assert not repo.exists(), "the lane's clone is gone"
    assert not lane_proto.lane_file(ws, "atlas", "x").exists(), "the lane record is gone"
    assert _git(member, "rev-parse", "HEAD") == member_head, "the workspace's own clone is untouched"
