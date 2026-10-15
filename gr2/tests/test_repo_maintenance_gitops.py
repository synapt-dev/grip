"""The repo-maintenance prototype starts git only through gitops.

`repo status` and `workspace convert-clone` read their repos through this module. A git it launches itself skips
the no-auto-maintenance flags and the bound every other gr2 git call carries, so a status read could start a gc in
the member it is reporting on. The first row is structural: no spawn of any kind may reappear in the module. The
other two run each verb through a `git` on PATH that records its argv and then runs the real git: every recorded
call carries the flags, and the verb's output is the output it printed before the move.
"""
from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.prototypes import repo_maintenance_prototype as repo_proto
from gr2.python_cli.app import app
from tests.test_gitops_shared import spawns

FLAGS = "-c maintenance.auto=false -c gc.auto=0 "
REAL_GIT = shutil.which("git")
runner = CliRunner()


def _git(cwd: Path, *args: str) -> None:
    subprocess.run([REAL_GIT, *args], cwd=cwd, check=True, capture_output=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "dev@example.invalid")
    _git(path, "config", "user.name", "Dev")
    (path / "README.md").write_text("a\n")
    _git(path, "add", ".")
    _git(path, "commit", "-qm", "init")


@pytest.fixture
def recorded(tmp_path, monkeypatch):
    """Put a recording `git` first on PATH; return a reader of the argv lines it saw."""
    log = tmp_path / "argv.log"
    log.write_text("")
    fake = tmp_path / "bin" / "git"
    fake.parent.mkdir()
    fake.write_text(f'#!/bin/sh\nprintf "%s\\n" "$*" >> "{log}"\nexec "{REAL_GIT}" "$@"\n')
    fake.chmod(fake.stat().st_mode | stat.S_IXUSR)
    monkeypatch.setenv("PATH", f"{fake.parent}{os.pathsep}{os.environ['PATH']}")
    return lambda: log.read_text().splitlines()


def test_the_prototype_starts_no_process_of_its_own():
    found = list(spawns(Path(repo_proto.__file__)))
    assert not found, "run git through gitops (git, run, clone):\n" + "\n".join(map(str, found))


def test_repo_status_output_is_unchanged_and_every_git_call_carries_the_flags(tmp_path, recorded):
    ws = tmp_path / "ws"
    _init_repo(ws / "repos" / "app")
    (ws / ".grip").mkdir(parents=True)
    (ws / ".grip" / "workspace_spec.toml").write_text(
        'workspace_name = "ws"\n'
        '[[repos]]\nname = "app"\npath = "repos/app"\nurl = "https://example.invalid/app.git"\n'
        '[[repos]]\nname = "gone"\npath = "repos/gone"\nurl = "https://example.invalid/gone.git"\n'
    )

    result = runner.invoke(app, ["repo", "status", str(ws), "--json"])
    assert result.exit_code == 0, result.output
    rows = [{k: row[k] for k in ("scope", "target", "repo", "action", "reason")}
            for row in json.loads(result.output)]
    assert rows == [
        {"scope": "shared", "target": "app", "repo": "app", "action": "manual_sync",
         "reason": "repo has no upstream tracking branch"},
        {"scope": "shared", "target": "gone", "repo": "gone", "action": "clone_missing",
         "reason": "repo path is absent"},
    ]
    lines = recorded()
    assert lines, "status ran no git at all"
    assert all(line.startswith(FLAGS) for line in lines), lines


def test_convert_clone_output_is_unchanged_and_every_git_call_carries_the_flags(tmp_path, recorded):
    canonical = tmp_path / "canonical"
    _init_repo(canonical)
    linked = tmp_path / "linked"
    _git(canonical, "worktree", "add", "-q", "-b", "side", str(linked))
    head = subprocess.run([REAL_GIT, "rev-parse", "HEAD"], cwd=linked, check=True, capture_output=True,
                          text=True).stdout.strip()
    recorded_before = len(recorded())

    result = runner.invoke(app, ["workspace", "convert-clone", str(linked)])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [
        f"Converted {linked.resolve()} from a linked worktree to an own clone.",
        "  branch: side",
        f"  head:   {head}",
        f"  was linked to: {(canonical / '.git').resolve()}",
        f"  WARNING: {linked.resolve()} carries 1 commit(s) on no remote. After conversion this clone is their "
        "only copy -- push before relying on it.",
    ]
    assert (linked / ".git").is_dir()
    lines = recorded()[recorded_before:]
    assert any(" clone " in f" {line} " for line in lines), lines
    assert all(line.startswith(FLAGS) for line in lines), lines
