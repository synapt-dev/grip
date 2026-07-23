"""Tests: native `gr2 push` command (grip#752 slice 1, D3).

This is the verb that started the scope-default investigation: `gr push`
(gr1) operates gripspace-wide by default with no --repo scoping (grip#755),
which is what led to Opus's steer -- and from there, to making cwd-repo
scope the consistent default across all four verbs, not just push.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_bare_remote(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "--bare", "-b", "main"], path)


def _init_repo_with_remote(path: Path, remote: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    _git(["config", "user.name", "Test"], path)
    _git(["config", "user.email", "test@example.com"], path)
    _git(["remote", "add", "origin", str(remote)], path)
    (path / "README.md").write_text("# test\n")
    _git(["add", "README.md"], path)
    _git(["commit", "-m", "initial"], path)


def _remote_log(remote: Path, ref: str = "main") -> str:
    return _git(["log", "-1", "--format=%H", ref], remote).stdout.strip()


def _local_log(repo: Path, ref: str = "HEAD") -> str:
    return _git(["log", "-1", "--format=%H", ref], repo).stdout.strip()


@pytest.fixture
def remote(tmp_path: Path) -> Path:
    remote_path = tmp_path / "remote.git"
    _init_bare_remote(remote_path)
    return remote_path


@pytest.fixture
def repo(tmp_path: Path, remote: Path) -> Path:
    repo_path = tmp_path / "repo"
    _init_repo_with_remote(repo_path, remote)
    return repo_path


class TestPush:
    def test_first_push_on_new_branch_succeeds(self, repo: Path, remote: Path) -> None:
        from gr2.python_cli.push import push

        result = push(repo, set_upstream=True)

        assert result.pushed is True
        assert _remote_log(remote) == _local_log(repo)

    def test_nothing_to_push_is_a_clean_skip_not_an_error(self, repo: Path, remote: Path) -> None:
        from gr2.python_cli.push import push

        push(repo, set_upstream=True)
        result = push(repo)  # already up to date

        assert result.pushed is False
        assert result.reason == "nothing to push"

    def test_new_commits_get_pushed(self, repo: Path, remote: Path) -> None:
        from gr2.python_cli.push import push

        push(repo, set_upstream=True)
        (repo / "more.txt").write_text("more\n")
        _git(["add", "more.txt"], repo)
        _git(["commit", "-m", "more work"], repo)

        result = push(repo)

        assert result.pushed is True
        assert _remote_log(remote) == _local_log(repo)

    def test_diverged_history_fails_without_force(self, repo: Path, remote: Path) -> None:
        from gr2.python_cli.push import PushError, push

        push(repo, set_upstream=True)
        # Rewrite local history so it diverges from what's on the remote.
        _git(["commit", "--amend", "-m", "rewritten"], repo)

        with pytest.raises(PushError):
            push(repo)

        assert _remote_log(remote) != _local_log(repo)

    def test_force_push_overwrites_diverged_remote(self, repo: Path, remote: Path) -> None:
        from gr2.python_cli.push import push

        push(repo, set_upstream=True)
        _git(["commit", "--amend", "-m", "rewritten"], repo)

        result = push(repo, force=True)

        assert result.pushed is True
        assert _remote_log(remote) == _local_log(repo)


class TestPushCLI:
    def test_gr2_push_via_cli(self, repo: Path, remote: Path) -> None:
        from typer.testing import CliRunner

        from gr2.python_cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["push", "--set-upstream", "--repo-path", str(repo)])

        assert result.exit_code == 0, result.output
        assert _remote_log(remote) == _local_log(repo)

    def test_gr2_push_defaults_to_cwd_repo_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The finding that started it all (grip#755), now an executable
        negative: gr2 push touches only the cwd repo, never a second one
        sitting right next to it."""
        from typer.testing import CliRunner

        from gr2.python_cli.app import app

        remote_a = tmp_path / "remote-a.git"
        remote_b = tmp_path / "remote-b.git"
        _init_bare_remote(remote_a)
        _init_bare_remote(remote_b)
        repo_a = tmp_path / "a"
        repo_b = tmp_path / "b"
        _init_repo_with_remote(repo_a, remote_a)
        _init_repo_with_remote(repo_b, remote_b)
        monkeypatch.chdir(repo_a)

        runner = CliRunner()
        result = runner.invoke(app, ["push", "--set-upstream"])

        assert result.exit_code == 0, result.output
        assert _remote_log(remote_a) == _local_log(repo_a)
        with pytest.raises(subprocess.CalledProcessError):
            _remote_log(remote_b)  # empty bare repo -- "main" ref never pushed, never existed
