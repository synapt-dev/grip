"""gitops names its repository with -C, so an inherited GIT_DIR or GIT_WORK_TREE must not aim git elsewhere.

A shell that exported GIT_DIR (or a hook git started, which inherits it) runs `gr2 commit --repo-path own` and the
commit lands in the OTHER repository, exit 0. gitops drops each inherited locating variable before it starts git. A
value the caller names itself (a temporary GIT_INDEX_FILE) is kept, and the other GIT_* variables (config, ssh,
author) pass through untouched.
"""
from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

from typer.testing import CliRunner

from gr2.python_cli import gitops
from gr2.python_cli.app import app

REAL_GIT = shutil.which("git")
runner = CliRunner()


def _git(cwd: Path, *args: str, env: dict | None = None) -> str:
    done = subprocess.run([REAL_GIT, *args], cwd=cwd, check=True, capture_output=True, text=True, env=env)
    return done.stdout.strip()


def _repo(path: Path, name: str) -> Path:
    path.mkdir(parents=True)
    _git(path, "init", "-q", "-b", "main")
    _git(path, "config", "user.email", "dev@example.invalid")
    _git(path, "config", "user.name", "Dev")
    (path / "README.md").write_text(f"{name}\n")
    _git(path, "add", ".")
    _git(path, "commit", "-q", "-m", f"{name} root")
    return path


def _pair(tmp_path: Path) -> tuple[Path, Path]:
    return _repo(tmp_path / "own", "own"), _repo(tmp_path / "other", "other")


def _head(repo: Path) -> str:
    env = {k: v for k, v in os.environ.items() if k not in gitops.LOCATING_ENV}
    return _git(repo, "rev-parse", "HEAD", env=env)


def test_r1_commit_lands_in_the_named_repo_under_an_inherited_git_dir(tmp_path, monkeypatch):
    own, other = _pair(tmp_path)
    for repo in (own, other):
        (repo / "staged.txt").write_text("x\n")
        _git(repo, "add", "staged.txt")
    own_before, other_before = _head(own), _head(other)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))

    result = runner.invoke(app, ["commit", "--repo-path", str(own), "-m", "meant for own"])

    assert result.exit_code == 0, result.output
    assert _head(other) == other_before
    assert _head(own) != own_before
    monkeypatch.delenv("GIT_DIR")
    assert _git(own, "log", "-1", "--format=%s") == "meant for own"


def test_r2_start_reads_the_named_repo_under_an_inherited_git_dir(tmp_path, monkeypatch):
    own, other = _pair(tmp_path)
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))

    proc = gitops.start(own, "cat-file", "--batch-check")
    out, _ = proc.communicate(b"HEAD^{commit}\n")

    assert out.decode().split()[0] == _head(own)


def test_r3_run_reads_the_named_worktree_under_an_inherited_git_work_tree(tmp_path, monkeypatch):
    own, other = _pair(tmp_path)
    (other / "README.md").write_text("edited in other\n")
    monkeypatch.setenv("GIT_WORK_TREE", str(other))

    status = gitops.run(own, "status", "--porcelain")

    assert status.returncode == 0, status.stderr
    assert status.stdout == ""


def test_r4_a_named_index_is_the_index_git_reads(tmp_path, monkeypatch):
    own, _other = _pair(tmp_path)
    monkeypatch.setenv("GIT_INDEX_FILE", str(own / ".git" / "index"))
    index = str(tmp_path / "scratch-index")
    env = {**os.environ, "GIT_INDEX_FILE": index}

    gitops.run(own, "read-tree", "--empty", env=env)
    tree = gitops.run(own, "write-tree", env=env)

    assert tree.stdout.strip() == "4b825dc642cb6eb9a060e54bf8d69288fbee4904"  # the empty tree
    assert Path(index).is_file()


def test_r5_other_git_variables_still_reach_git_while_locating_ones_are_dropped(tmp_path, monkeypatch):
    own, other = _pair(tmp_path)
    config = tmp_path / "global.gitconfig"
    config.write_text("[gr2probe]\n\tkey = from-global\n")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", str(config))
    monkeypatch.setenv("GIT_DIR", str(other / ".git"))  # so the filter runs; only the locating names may go

    read = gitops.run(own, "config", "--get", "gr2probe.key")

    assert read.stdout.strip() == "from-global"
