"""Tests: native `gr2 commit` command (grip#752 slice 1, D3)."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.name", "Test"], path)
    _git(["config", "user.email", "test@example.com"], path)
    (path / "README.md").write_text("# test\n")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "initial"], path)


def _log_message(path: Path, ref: str = "HEAD") -> str:
    return _git(["log", "-1", "--format=%s", ref], path).stdout.strip()


def _commit_count(path: Path) -> int:
    return int(_git(["rev-list", "--count", "HEAD"], path).stdout.strip())


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    _init_repo(repo_path)
    return repo_path


class TestCreateCommit:
    def test_commits_staged_changes_and_returns_hash(self, repo: Path) -> None:
        from gr2.python_cli.commit import create_commit

        (repo / "new.txt").write_text("content\n")
        _git(["add", "new.txt"], repo)

        commit_hash = create_commit(repo, "add new file")

        assert commit_hash
        assert _log_message(repo) == "add new file"
        assert _git(["rev-parse", "HEAD"], repo).stdout.strip() == commit_hash

    def test_nothing_staged_raises_clearly(self, repo: Path) -> None:
        from gr2.python_cli.commit import CommitError, create_commit

        with pytest.raises(CommitError, match="[Nn]othing"):
            create_commit(repo, "empty commit attempt")

        assert _commit_count(repo) == 1  # unchanged

    def test_amend_updates_message_keeps_single_commit(self, repo: Path) -> None:
        from gr2.python_cli.commit import create_commit

        (repo / "README.md").write_text("# test\nmore\n")
        _git(["add", "README.md"], repo)

        create_commit(repo, "amended message", amend=True)

        assert _log_message(repo) == "amended message"
        assert _commit_count(repo) == 1


class TestCommitCLI:
    def test_gr2_commit_via_cli(self, repo: Path) -> None:
        from typer.testing import CliRunner

        from gr2.python_cli.app import app

        (repo / "new.txt").write_text("content\n")
        _git(["add", "new.txt"], repo)

        runner = CliRunner()
        result = runner.invoke(app, ["commit", "-m", "cli commit", "--repo-path", str(repo)])

        assert result.exit_code == 0, result.output
        assert _log_message(repo) == "cli commit"

    def test_gr2_commit_nothing_staged_exits_nonzero_with_clear_message(self, repo: Path) -> None:
        from typer.testing import CliRunner

        from gr2.python_cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["commit", "-m", "nothing", "--repo-path", str(repo)])

        assert result.exit_code != 0
        assert "nothing" in result.output.lower()

    def test_gr2_commit_defaults_to_cwd_repo_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from gr2.python_cli.app import app

        repo_a = tmp_path / "a"
        repo_b = tmp_path / "b"
        _init_repo(repo_a)
        _init_repo(repo_b)
        (repo_a / "new.txt").write_text("content\n")
        _git(["add", "new.txt"], repo_a)
        (repo_b / "new.txt").write_text("content\n")
        _git(["add", "new.txt"], repo_b)
        monkeypatch.chdir(repo_a)

        runner = CliRunner()
        result = runner.invoke(app, ["commit", "-m", "scoped commit"])

        assert result.exit_code == 0, result.output
        assert _commit_count(repo_a) == 2
        assert _commit_count(repo_b) == 1  # untouched -- no gripspace-wide sweep
