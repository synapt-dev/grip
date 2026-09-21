"""TDD: gr2 branch / add / commit refuse in one sentence when run outside a
repository (alpha-2 finding 2).

Measured on 2.0.0a1 (APOLLO-1945 fixture runs), from a directory ABOVE the
repos (a materialized unit home): branch and add print git's own
"fatal: not a git repository (or any of the parent directories)", and commit
prints git's full usage dump ("unknown option cached" among ~40 lines) —
raw git text, no sentence naming what the verb needs. The fix: each verb
refuses at entry with ONE sentence that names what it needs (run it inside
one repository of the workspace) and lists the repos found under the cwd, so
the stranger's next command is obvious.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app


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


runner = CliRunner()


def test_branch_outside_repo_refuses_and_lists_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path / "alpha")
    _init_repo(tmp_path / "beta")
    monkeypatch.chdir(tmp_path)  # the test process cwd is a git repo; the refusal is about THIS cwd

    result = runner.invoke(app, ["branch", "feat/x"], catch_exceptions=False)
    assert result.exit_code == 1, result.output
    assert "not a git repository" in result.output, (
        f"the refusal must say the cwd is not a repository:\n{result.output}"
    )
    assert "inside one repository" in result.output, (
        f"the refusal must name what the verb needs:\n{result.output}"
    )
    assert "alpha" in result.output and "beta" in result.output, (
        f"the refusal must list the repos found under the cwd:\n{result.output}"
    )
    # No raw git text: the failure is gr2's sentence, not git's.
    assert "fatal:" not in result.output, (
        f"a raw git error leaked:\n{result.output}"
    )


def test_add_outside_repo_refuses_and_lists_repos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path / "alpha")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["add", "."], catch_exceptions=False)
    assert result.exit_code == 1, result.output
    assert "inside one repository" in result.output, result.output
    assert "alpha" in result.output, result.output
    assert "fatal:" not in result.output, result.output


def test_commit_outside_repo_refuses_no_usage_dump(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _init_repo(tmp_path / "alpha")
    monkeypatch.chdir(tmp_path)

    result = runner.invoke(app, ["commit", "-m", "x"], catch_exceptions=False)
    assert result.exit_code == 1, result.output
    assert "inside one repository" in result.output, result.output
    assert "alpha" in result.output, result.output
    # The measured failure: git's usage dump with "unknown option cached".
    assert "unknown option" not in result.output, (
        f"git's usage dump leaked:\n{result.output}"
    )
    assert "Options:" not in result.output, (
        f"git's usage dump leaked:\n{result.output}"
    )


def test_verbs_still_work_inside_a_repo(tmp_path: Path) -> None:
    """The refusal must not fire for a legitimate single-repo run."""
    _init_repo(tmp_path / "alpha")

    inside = runner.invoke(
        app, ["branch", "feat/slice", "--repo-path", str(tmp_path / "alpha")]
    )
    assert inside.exit_code == 0, inside.output
    assert "Switched to branch 'feat/slice'" in inside.output