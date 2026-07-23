"""TDD failing tests: native `gr2 branch` command (grip#752 slice 1, D3).

Scope decision (2026-07-07 #dev, Opus's steer): gr2's daily verbs default to
cwd-repo scope, not gr1's gripspace-wide default. gr1's own branch/add/commit/push
all operate over filter_repos(...) across the whole manifest by default -- that's
the source of grip#755's surprise (push ran gripspace-wide with no --repo given).
gr2 departs from that deliberately, consistently across all four verbs, so the
CLI has one predictable mental model instead of push being single-repo while
branch is gripspace-wide.

grip#746 (gr1: branch has no --base, forces raw-git fallback) is folded in from
day one rather than deferred: create_branch(repo, name, base=...) resolves the
base ref per the target repo, defaults to current HEAD when omitted (backward
compatible), and raises clearly rather than silently falling back to HEAD when
the given ref doesn't exist -- this is grip#746's explicit acceptance criteria.

The existing-branch-switches-instead-of-erroring behavior mirrors gr1's own
grip#401 fix (re-running branch on a name that already exists should not block
forward progress).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner


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


def _current_branch(path: Path) -> str:
    return _git(["branch", "--show-current"], path).stdout.strip()


def _head_sha(path: Path, ref: str) -> str:
    return _git(["log", "-1", "--format=%H", ref], path).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    repo_path = tmp_path / "repo"
    _init_repo(repo_path)
    return repo_path


class TestCreateBranch:
    def test_creates_and_switches_to_new_branch(self, repo: Path) -> None:
        from gr2.python_cli.branch import create_branch

        create_branch(repo, "feat/x")
        assert _current_branch(repo) == "feat/x"

    def test_existing_branch_switches_instead_of_erroring(self, repo: Path) -> None:
        """grip#401 UX: re-running branch on an existing name switches, doesn't error."""
        from gr2.python_cli.branch import create_branch

        create_branch(repo, "feat/x")
        _git(["checkout", "main"], repo)

        create_branch(repo, "feat/x")  # must not raise

        assert _current_branch(repo) == "feat/x"

    def test_base_ref_creates_branch_off_specified_ref_not_head(self, repo: Path) -> None:
        """grip#746: --base must branch off the given ref, not current HEAD."""
        from gr2.python_cli.branch import create_branch

        # Diverge HEAD from main so "off main" and "off HEAD" would disagree.
        _git(["checkout", "-b", "other"], repo)
        (repo / "other.txt").write_text("other\n")
        _git(["add", "other.txt"], repo)
        _git(["commit", "-m", "other work"], repo)

        create_branch(repo, "feat/based", base="main")

        assert _head_sha(repo, "feat/based") == _head_sha(repo, "main")
        assert _head_sha(repo, "feat/based") != _head_sha(repo, "other")

    def test_missing_base_ref_errors_clearly_no_silent_head_fallback(self, repo: Path) -> None:
        """grip#746 acceptance criteria: missing ref errors clearly, never silently falls back to HEAD."""
        from gr2.python_cli.branch import BranchError, create_branch

        head_before = _head_sha(repo, "HEAD")

        with pytest.raises(BranchError, match="nonexistent-ref"):
            create_branch(repo, "feat/y", base="nonexistent-ref")

        # No branch should have been created off HEAD as a silent fallback.
        result = _git(["branch", "--list", "feat/y"], repo)
        assert result.stdout.strip() == ""
        assert _head_sha(repo, "HEAD") == head_before

    def test_worktree_conflict_gives_helpful_error(self, repo: Path, tmp_path: Path) -> None:
        """Mirrors gr1's own worktree-conflict detection (real, cited team pain: grip#750)."""
        from gr2.python_cli.branch import BranchError, create_branch

        create_branch(repo, "shared-branch")
        _git(["checkout", "main"], repo)

        worktree_path = tmp_path / "worktree"
        _git(["worktree", "add", str(worktree_path), "shared-branch"], repo)

        with pytest.raises(BranchError, match="worktree"):
            create_branch(repo, "shared-branch")


class TestBranchCLI:
    def test_gr2_branch_command_creates_branch(self, repo: Path) -> None:
        from gr2.python_cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["branch", "feat/cli-test", "--repo-path", str(repo)])

        assert result.exit_code == 0, result.output
        assert _current_branch(repo) == "feat/cli-test"

    def test_gr2_branch_supports_base_flag(self, repo: Path) -> None:
        _git(["checkout", "-b", "other"], repo)
        (repo / "other.txt").write_text("other\n")
        _git(["add", "other.txt"], repo)
        _git(["commit", "-m", "other work"], repo)

        from gr2.python_cli.app import app

        runner = CliRunner()
        result = runner.invoke(
            app, ["branch", "feat/cli-based", "--base", "main", "--repo-path", str(repo)]
        )

        assert result.exit_code == 0, result.output
        assert _head_sha(repo, "feat/cli-based") == _head_sha(repo, "main")

    def test_gr2_branch_defaults_to_cwd_repo_scope(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """gr2 branch operates on the current repo by default -- no gripspace-wide
        sweep unless explicitly requested. Departs from gr1's default (which
        operates over filter_repos(...) across the whole manifest); grip#755's
        finding (push ran gripspace-wide unexpectedly) is folded into this as a
        deliberate, consistent design choice across all four verbs, not just push.
        """
        repo_a = tmp_path / "a"
        repo_b = tmp_path / "b"
        _init_repo(repo_a)
        _init_repo(repo_b)
        monkeypatch.chdir(repo_a)

        from gr2.python_cli.app import app

        runner = CliRunner()
        result = runner.invoke(app, ["branch", "feat/scoped"])

        assert result.exit_code == 0, result.output
        assert _current_branch(repo_a) == "feat/scoped"
        assert _current_branch(repo_b) == "main"  # untouched -- no gripspace-wide sweep
