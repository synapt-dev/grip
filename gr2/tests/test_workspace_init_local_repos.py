"""TDD: workspace init on repos with no remote (alpha-2 finding 1), and bare
repos the scanner skipped.

Measured on 2.0.0a1 (APOLLO-1945 fixture runs): `gr2 workspace init` over two
local repos with no remotes SUCCEEDS, prints the repos with url "-", and
materialize then refuses that same spec — three screens of silence between the
verb that wrote the bad spec and the verb that refuses it. The fix keeps the
spec (the stranger's scan is not discarded) and makes init SAY IT: per repo
without an origin, the output names that materialize will refuse until a url
is set, with the one command that fixes it. A stranger with two local repos
gets the furthest this way: refusing at init would throw away the scan and
force a re-run, while the written spec plus `git remote add origin` is a
mechanical, local fix (the decision Stromus asked to be stated in the PR body).

Bare repos: the scanner's `is_git_repo` answers only for work trees, so a bare
dir (exactly what a stranger uses as a local upstream) was silently invisible.
The call made here: DETECT bare repos and scan them like any other repo —
they materialize fine from their own path.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner

from gr2.python_cli.app import app


def _git(args: list[str], cwd: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True)


def _init_repo(path: Path, *, with_commit: bool = True) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(["init", "-b", "main"], path)
    if with_commit:
        _git(["config", "user.name", "Test"], path)
        _git(["config", "user.email", "test@example.com"], path)
        (path / "README.md").write_text("# test\n")
        _git(["add", "README.md"], path)
        _git(["commit", "-m", "initial"], path)


runner = CliRunner()


def test_init_without_remotes_succeeds_and_names_the_missing_url(tmp_path: Path) -> None:
    """Two local repos, no remotes: init still writes the spec, and its output
    names, per repo, that materialize will refuse until a url is set — plus the
    command that fixes it."""
    _init_repo(tmp_path / "alpha")
    _init_repo(tmp_path / "beta")

    result = runner.invoke(app, ["workspace", "init", str(tmp_path)])
    assert result.exit_code == 0, result.output

    assert "no origin" in result.output, f"init must name the missing origin:\n{result.output}"
    assert "alpha" in result.output and "beta" in result.output
    assert "materialize" in result.output, (
        f"the note must name the verb that will refuse:\n{result.output}"
    )
    assert "remote add origin" in result.output, (
        f"the note must give the fix:\n{result.output}"
    )
    # The spec is still written: the stranger's scan survives.
    assert (tmp_path / ".grip" / "workspace_spec.toml").is_file()

    # The JSON payload carries the same fact for a caller that machines this.
    payload_result = runner.invoke(app, ["workspace", "init", str(tmp_path), "--json"])
    assert payload_result.exit_code == 0
    assert '"alpha"' in payload_result.output
    assert "repos_without_url" in payload_result.output, (
        f"JSON must name the repos without a url:\n{payload_result.output}"
    )


def test_init_detects_bare_repo_as_a_repo(tmp_path: Path) -> None:
    """A bare repo dir (the local-upstream shape) is scanned, not skipped."""
    _init_repo(tmp_path / "work")
    _git(["clone", "--bare", str(tmp_path / "work"), str(tmp_path / "upstream.git")], tmp_path)

    result = runner.invoke(app, ["workspace", "init", str(tmp_path)])
    assert result.exit_code == 0, result.output
    assert "upstream.git" in result.output, (
        f"bare repo must appear in the scan output:\n{result.output}"
    )