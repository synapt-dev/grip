"""`gr2 change`: one change across members in one command.

c1 is the happy path at N=5; c3 rows are phase-0 refusals, which must leave
every member byte-equal (branch, HEAD, index, worktree) and run no write.
Git calls are counted with a PATH shim, per member.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app

N = 5
WRITES = ("checkout", "add", "commit", "update-ref", "reset", "branch")


def _git(path: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=path, check=True, capture_output=True, text=True).stdout


def _member(path: Path) -> None:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    (path / "README.md").write_text("# m\n")
    _git(path, "add", "README.md")
    _git(path, "commit", "-q", "-m", "initial")


def _edit(path: Path) -> None:
    (path / "README.md").write_text(f"# {path.name} edited\n")
    (path / "new.txt").write_text("new\n")


def _state(path: Path) -> tuple[str, ...]:
    return (
        _git(path, "symbolic-ref", "-q", "HEAD").strip(),
        _git(path, "rev-parse", "HEAD").strip(),
        _git(path, "ls-files", "-s"),
        _git(path, "status", "--porcelain=v1", "-uall"),
        (path / "README.md").read_text(),
        _git(path, "for-each-ref", "refs/heads"),
    )


@pytest.fixture
def members(tmp_path: Path) -> list[Path]:
    paths = [tmp_path / f"m{i}" for i in range(1, N + 1)]
    for path in paths:
        _member(path)
        _edit(path)
    return paths


@pytest.fixture
def git_log(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    real = shutil.which("git")
    shim_dir = tmp_path / "shim"
    shim_dir.mkdir()
    log = tmp_path / "git.log"
    shim = shim_dir / "git"
    shim.write_text(f'#!/bin/sh\nprintf "%s\\t%s\\n" "$(basename "$PWD")" "$*" >> "{log}"\nexec "{real}" "$@"\n')
    shim.chmod(0o755)
    monkeypatch.setenv("PATH", f"{shim_dir}{os.pathsep}{os.environ['PATH']}")
    log.write_text("")
    return log


def _calls(log: Path) -> list[tuple[str, str]]:
    return [tuple(line.split("\t", 1)) for line in log.read_text().splitlines()]


def _run(members: list[Path], *extra: str):
    args = ["change", "feat", "-m", "one change", *extra]
    for path in members:
        args += ["--repo-path", str(path)]
    return CliRunner().invoke(app, args)


def test_c1_one_change_commits_every_member(members: list[Path], git_log: Path) -> None:
    bases = [_git(p, "rev-parse", "HEAD").strip() for p in members]
    git_log.write_text("")
    result = _run(members)
    calls = _calls(git_log)
    assert result.exit_code == 0, result.output
    for path, base in zip(members, bases):
        assert _git(path, "branch", "--show-current").strip() == "feat"
        assert _git(path, "rev-parse", "HEAD~1").strip() == base
        assert _git(path, "show", "HEAD:README.md") == f"# {path.name} edited\n"
        assert _git(path, "show", "HEAD:new.txt") == "new\n"
        assert _git(path, "status", "--porcelain") == ""
        assert f"{path.name}: feat " in result.output
    per_member = {p.name: sum(1 for m, _ in calls if m == p.name) for p in members}
    print(f"git calls per member: {per_member} total {sum(per_member.values())}")
    assert all(n <= 6 for n in per_member.values()), per_member


@pytest.mark.parametrize(
    "reason, spoil",
    [
        ("staged_index", lambda p: _git(p, "add", "README.md")),
        ("branch_exists", lambda p: _git(p, "branch", "feat", "HEAD")),
    ],
)
def test_c3_refusal_writes_nothing_in_any_member(members: list[Path], git_log: Path, reason, spoil) -> None:
    spoil(members[2])
    before = [_state(p) for p in members]
    git_log.write_text("")
    result = _run(members)
    assert result.exit_code == 2, result.output
    assert f"refused: {reason}: m3:" in result.output
    assert "nothing was written" in result.output
    writes = [(m, a) for m, a in _calls(git_log) if set(a.split(" ")) & set(WRITES)]
    assert writes == []
    assert [_state(p) for p in members] == before
