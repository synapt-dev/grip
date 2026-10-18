"""A remote named like an option reaches `gr2 push` and `gr2 lane push` as a name, never as an option.

git accepts a remote named "--receive-pack=<script>" or "--upload-pack=<script>" (`git remote add -- <name>`).
The push verbs pass the selected remote to `git push`, `git ls-remote` and `git fetch` on argv; unfenced,
git reads the name as that option and runs the script. Each row names the site it reaches; each site puts
"--" before the remote, so the name is looked up as a remote and the push lands at its url.
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli import app as gr2_app
from tests.test_lane_create_fork_base_cli import runner
from tests.test_lane_push import BRANCH, _gamma_off_branch_behind_the_remote, _lane, _push, _remote_sha
from tests.test_push import _bare, _git, _init_repo


def _script(tmp_path: Path) -> tuple[Path, Path]:
    marker = tmp_path / "ran"
    script = tmp_path / "opt.sh"
    script.write_text(f"#!/bin/sh\necho \"$@\" >> '{marker}'\nexit 1\n")
    script.chmod(0o755)
    return script, marker


@pytest.mark.parametrize("chosen", ["sole", "explicit", "pushRemote"])
@pytest.mark.parametrize("option", ["--receive-pack", "--upload-pack"])
def test_gr2_push_passes_an_option_named_remote_to_git_as_a_name(tmp_path: Path, option: str, chosen: str) -> None:
    """Sites: `git push` (--receive-pack runs there) and the receipt's `git ls-remote` (--upload-pack runs there).
    The remote is reached as the sole remote, by --remote, and by branch.<b>.pushRemote."""
    repo = _init_repo(tmp_path / "repo")
    script, marker = _script(tmp_path)
    name = f"{option}={script}"
    bare = _bare(tmp_path / "remote.git")
    _git(repo, "remote", "add", "--", name, str(bare))
    extra: list[str] = []
    if chosen != "sole":
        _git(repo, "remote", "add", "other", str(_bare(tmp_path / "other.git")))
    if chosen == "explicit":
        extra = [f"--remote={name}"]
    if chosen == "pushRemote":
        _git(repo, "config", "branch.main.pushRemote", name)

    result = runner.invoke(gr2_app.app, ["push", "--repo-path", str(repo), *extra])

    ran = marker.read_text() if marker.exists() else ""
    assert not ran, f"git ran the remote's name as {option}: {ran}"
    assert result.exit_code == 0, result.output
    assert _git(bare, "rev-parse", "refs/heads/main").stdout.strip() == _git(repo, "rev-parse", "HEAD").stdout.strip()


def _dash_gamma(tmp_path: Path, repos: dict[str, Path]) -> Path:
    script, marker = _script(tmp_path)
    _git(repos["gamma"], "remote", "rename", "--", "origin", f"--upload-pack={script}")
    return marker


def test_lane_push_asks_coverage_of_an_option_named_remote_by_name(tmp_path: Path) -> None:
    """Site: the coverage `git ls-remote` for a repo pushed from a branch other than the lane's."""
    ws, repos = _lane(tmp_path)
    marker = _dash_gamma(tmp_path, repos)
    _git(repos["gamma"], "checkout", "-q", "-b", "feat/other")

    result = _push(ws, "-u")

    ran = marker.read_text() if marker.exists() else ""
    assert not ran, f"git ran the remote's name as --upload-pack: {ran}"
    assert result.exit_code == 3, result.output
    assert "push it first" in result.output and "cannot tell" not in result.output, result.output
    assert _remote_sha(tmp_path, "gamma", "feat/other") == _git(repos["gamma"], "rev-parse", "HEAD").stdout.strip()


def test_lane_push_fetches_an_unheld_lane_tip_from_an_option_named_remote_by_name(tmp_path: Path) -> None:
    """Site: the coverage `git fetch` of a lane tip this clone does not hold."""
    ws, repos = _lane(tmp_path)
    head, theirs = _gamma_off_branch_behind_the_remote(tmp_path, ws, repos)
    marker = _dash_gamma(tmp_path, repos)

    result = _push(ws, "-u")

    ran = marker.read_text() if marker.exists() else ""
    assert not ran, f"git ran the remote's name as --upload-pack: {ran}"
    assert result.exit_code == 0, result.output
    assert "gamma head" not in result.output and "cannot tell" not in result.output, result.output
    assert _remote_sha(tmp_path, "gamma", "feat/other") == head
    assert _remote_sha(tmp_path, "gamma") == theirs
