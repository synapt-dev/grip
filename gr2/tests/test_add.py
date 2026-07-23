"""Tests: native `gr2 add` command (grip#752 slice 1, D3).

grip#754's root cause, revisited: gr1's `gr add` stages files individually
via multi-repo path attribution (a gripspace-relative path has to be matched
to the right repo before `git add` ever runs), and blanket-treats any git
stderr containing "did not match any files" as "not in this repo" -- which
fired on a genuinely valid, real, untracked file (confirmed reproduced twice,
different sessions). gr2's cwd-repo-scope-by-default (grip#755's finding,
now product canon) removes the attribution step entirely: there is exactly
one target repo, so "is this path in this repo" is answered by checking the
path resolves under repo root, not by parsing git's stderr text. A path that
genuinely doesn't exist gets a precise, checked message; anything else that
makes `git add` fail is a real error, not a silent skip.
"""

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


def _staged_files(path: Path) -> list[str]:
    result = _git(["diff", "--cached", "--name-only"], path)
    return [line for line in result.stdout.splitlines() if line]


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    _init_repo(repo_path)
    return repo_path


class TestStageFiles:
    def test_stages_a_real_untracked_file(self, repo: Path) -> None:
        from gr2.python_cli.add import stage_files

        (repo / "new.txt").write_text("content\n")

        result = stage_files(repo, ["new.txt"])

        assert result.staged == ["new.txt"]
        assert result.missing == []
        assert _staged_files(repo) == ["new.txt"]

    def test_stages_dot_adds_all_changes_including_deletions(self, repo: Path) -> None:
        from gr2.python_cli.add import stage_files

        (repo / "new.txt").write_text("content\n")
        (repo / "README.md").unlink()

        result = stage_files(repo, ["."])

        assert set(result.staged) == {"new.txt", "README.md"}
        assert result.missing == []

    def test_genuinely_missing_path_is_reported_precisely_not_silently(self, repo: Path) -> None:
        """The grip#754 regression case, inverted: a path that really doesn't
        exist gets reported as missing -- checked directly, not inferred
        from git's stderr text."""
        from gr2.python_cli.add import stage_files

        result = stage_files(repo, ["does-not-exist.txt"])

        assert result.staged == []
        assert result.missing == ["does-not-exist.txt"]
        assert _staged_files(repo) == []

    def test_one_missing_path_does_not_block_the_rest(self, repo: Path) -> None:
        from gr2.python_cli.add import stage_files

        (repo / "real.txt").write_text("content\n")

        result = stage_files(repo, ["real.txt", "ghost.txt"])

        assert result.staged == ["real.txt"]
        assert result.missing == ["ghost.txt"]
        assert _staged_files(repo) == ["real.txt"]

    def test_real_file_that_git_still_rejects_raises_not_silently_skips(
        self, repo: Path, tmp_path: Path
    ) -> None:
        """A path that exists on disk but is invalid as a git pathspec (e.g.
        outside the repo entirely) is a real error, never a silent
        "not in this repo" skip -- that conflation is exactly grip#754."""
        from gr2.python_cli.add import AddError, stage_files

        outside = tmp_path / "outside.txt"
        outside.write_text("content\n")

        with pytest.raises(AddError):
            stage_files(repo, [str(outside)])


class TestAddCLI:
    def test_gr2_add_stages_via_cli(self, repo: Path) -> None:
        from typer.testing import CliRunner

        from gr2.python_cli.app import app

        (repo / "new.txt").write_text("content\n")
        runner = CliRunner()
        result = runner.invoke(app, ["add", "new.txt", "--repo-path", str(repo)])

        assert result.exit_code == 0, result.output
        assert _staged_files(repo) == ["new.txt"]

    def test_gr2_add_defaults_to_cwd_repo_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from typer.testing import CliRunner

        from gr2.python_cli.app import app

        repo_a = tmp_path / "a"
        repo_b = tmp_path / "b"
        _init_repo(repo_a)
        _init_repo(repo_b)
        (repo_a / "new.txt").write_text("content\n")
        (repo_b / "new.txt").write_text("content\n")
        monkeypatch.chdir(repo_a)

        runner = CliRunner()
        result = runner.invoke(app, ["add", "new.txt"])

        assert result.exit_code == 0, result.output
        assert _staged_files(repo_a) == ["new.txt"]
        assert _staged_files(repo_b) == []  # untouched -- no gripspace-wide sweep
