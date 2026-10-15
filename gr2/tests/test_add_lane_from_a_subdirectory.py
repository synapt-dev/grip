"""`add --lane PATHS` stages each path at the root of EVERY lane repo. Typed from a subdirectory of one lane repo,
a relative path means something else to the person typing it: `n.py` in `pkg/` is `pkg/n.py`. Read at the repo
root it named a different file (a root-level `n.py` was staged and the verb exited 0) or no file at all. From a
subdirectory the verb now refuses before staging anything, and names the two forms that are unambiguous.

Goes red if the subdirectory check in `_add_in_lane` is removed."""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app

runner = CliRunner()
GIT = ["git", "-c", "user.name=t", "-c", "user.email=t@example.com"]


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run([*GIT, *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture
def lane_repo(tmp_path, monkeypatch) -> Path:
    """An entered lane `L` whose one repo `alpha` has a `pkg/` subdirectory; returns the lane's checkout."""
    monkeypatch.setenv("GR2_ACTOR", "agent:test")
    bare = tmp_path / "alpha.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(bare)], check=True)
    root = tmp_path / "ws"
    root.mkdir()
    subprocess.run(["git", "clone", "-q", str(bare), str(root / "alpha")], check=True, capture_output=True)
    (root / "alpha" / "pkg").mkdir()
    (root / "alpha" / "pkg" / "m.py").write_text("x = 1\n")
    _git(root / "alpha", "add", ".")
    _git(root / "alpha", "commit", "-q", "-m", "init")
    _git(root / "alpha", "push", "-q", "origin", "main")
    monkeypatch.chdir(root)
    for args in (["workspace", "init", str(root)],
                 ["lane", "create", "default", "L", "--repos", "alpha", "--branch", "f/x"],
                 ["lane", "enter", "default", "L"]):
        done = runner.invoke(app, args)
        assert done.exit_code == 0, done.output
    repo = root / "agents" / "default" / "lanes" / "L" / "repos" / "alpha"
    assert (repo / "pkg").is_dir()
    return repo


def _staged(repo: Path) -> list[str]:
    return _git(repo, "diff", "--cached", "--name-only").split()


def test_add_lane_from_a_subdirectory_does_not_stage_the_root_level_twin(lane_repo, monkeypatch) -> None:
    (lane_repo / "pkg" / "n.py").write_text("meant\n")
    (lane_repo / "n.py").write_text("not meant\n")
    monkeypatch.chdir(lane_repo / "pkg")
    done = runner.invoke(app, ["add", "--lane", "n.py"])
    assert done.exit_code == 2, done.output
    assert _staged(lane_repo) == []
    assert "subdirectory" in done.output and "--repo-path" in done.output


def test_add_lane_from_a_subdirectory_refuses_by_name_when_there_is_no_twin(lane_repo, monkeypatch) -> None:
    (lane_repo / "pkg" / "n.py").write_text("meant\n")
    monkeypatch.chdir(lane_repo / "pkg")
    done = runner.invoke(app, ["add", "--lane", "n.py"])
    assert done.exit_code == 2, done.output
    assert "did not match" not in done.output
    assert _staged(lane_repo) == []


def test_add_lane_from_the_lane_repo_root_still_stages(lane_repo, monkeypatch) -> None:
    (lane_repo / "n.py").write_text("meant\n")
    monkeypatch.chdir(lane_repo)
    done = runner.invoke(app, ["add", "--lane", "n.py"])
    assert done.exit_code == 0, done.output
    assert _staged(lane_repo) == ["n.py"]


def test_plain_add_from_the_subdirectory_still_reads_the_path_from_there(lane_repo, monkeypatch) -> None:
    (lane_repo / "pkg" / "n.py").write_text("meant\n")
    (lane_repo / "n.py").write_text("not meant\n")
    monkeypatch.chdir(lane_repo / "pkg")
    done = runner.invoke(app, ["add", "n.py"])
    assert done.exit_code == 0, done.output
    assert _staged(lane_repo) == ["pkg/n.py"]
