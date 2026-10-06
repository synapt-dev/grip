"""Executable contract for the native single-repository ``gr2 commit`` verb."""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from typer.testing import CliRunner


def _git(repo: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=check,
        capture_output=True,
        text=True,
    )


def _init_repo(path: Path) -> Path:
    path.mkdir()
    _git(path, "init", "-b", "main")
    _git(path, "config", "user.name", "Test")
    _git(path, "config", "user.email", "test@example.com")
    (path / "tracked.txt").write_text("initial\n")
    _git(path, "add", ".")
    _git(path, "commit", "-m", "initial")
    return path


def test_create_commit_returns_the_commit_it_actually_created(tmp_path: Path) -> None:
    from gr2.python_cli.commit import create_commit

    repo = _init_repo(tmp_path / "repo")
    (repo / "tracked.txt").write_text("changed\n")
    _git(repo, "add", "tracked.txt")

    receipt = create_commit(repo, "native commit")

    actual_head = _git(repo, "rev-parse", "HEAD").stdout.strip()
    assert receipt.commit_sha == actual_head
    assert receipt.message == "native commit"
    assert _git(repo, "show", "-s", "--format=%s", "HEAD").stdout.strip() == "native commit"


def test_no_staged_changes_refuses_structurally_before_commit(tmp_path: Path) -> None:
    from gr2.python_cli.commit import NothingToCommitError, create_commit

    repo = _init_repo(tmp_path / "repo")
    head_before = _git(repo, "rev-parse", "HEAD").stdout.strip()

    with pytest.raises(NothingToCommitError):
        create_commit(repo, "must not exist")

    assert _git(repo, "rev-parse", "HEAD").stdout.strip() == head_before


def test_commit_outcome_uses_exit_status_not_command_prose(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli import commit as commit_ops

    repo = _init_repo(tmp_path / "repo")
    calls: list[tuple[str, ...]] = []

    def fake_git(_repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        if args[:3] == ("diff", "--cached", "--quiet"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "nothing to commit")
        if args[0] == "commit":
            return subprocess.CompletedProcess(["git", *args], 0, "", "nothing to commit")
        if args == ("rev-parse", "--verify", "HEAD"):
            sha = "b" * 40 if calls.count(args) == 1 else "a" * 40
            return subprocess.CompletedProcess(["git", *args], 0, sha + "\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(commit_ops, "git", fake_git)

    receipt = commit_ops.create_commit(repo, "status wins")

    assert receipt.commit_sha == "a" * 40
    assert any(args[0] == "commit" for args in calls)


def test_acknowledged_commit_without_a_new_head_refuses(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli import commit as commit_ops

    repo = _init_repo(tmp_path / "repo")

    def fake_git(_repo: Path, *args: str) -> subprocess.CompletedProcess[str]:
        if args[:3] == ("diff", "--cached", "--quiet"):
            return subprocess.CompletedProcess(["git", *args], 1, "", "")
        if args[0] == "commit":
            return subprocess.CompletedProcess(["git", *args], 0, "", "")
        if args == ("rev-parse", "--verify", "HEAD"):
            return subprocess.CompletedProcess(["git", *args], 0, "a" * 40 + "\n", "")
        raise AssertionError(args)

    monkeypatch.setattr(commit_ops, "git", fake_git)

    with pytest.raises(commit_ops.CommitError, match="HEAD did not advance"):
        commit_ops.create_commit(repo, "no fruit")


def test_amend_without_new_staged_changes_is_allowed(tmp_path: Path) -> None:
    from gr2.python_cli.commit import create_commit

    repo = _init_repo(tmp_path / "repo")

    receipt = create_commit(repo, "amended message", amend=True)

    assert receipt.amended is True
    assert _git(repo, "show", "-s", "--format=%s", "HEAD").stdout.strip() == "amended message"


def test_commit_cli_defaults_to_the_cwd_repository_only(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli.app import app

    repo_a = _init_repo(tmp_path / "a")
    repo_b = _init_repo(tmp_path / "b")
    for repo in (repo_a, repo_b):
        (repo / "tracked.txt").write_text("changed\n")
        _git(repo, "add", "tracked.txt")
    before_b = _git(repo_b, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.chdir(repo_a)

    result = CliRunner().invoke(app, ["commit", "-m", "cwd only"])

    assert result.exit_code == 0, result.output
    assert _git(repo_a, "show", "-s", "--format=%s", "HEAD").stdout.strip() == "cwd only"
    assert _git(repo_b, "rev-parse", "HEAD").stdout.strip() == before_b


def test_commit_cli_honors_explicit_repo_path_over_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli.app import app

    cwd_repo = _init_repo(tmp_path / "cwd")
    requested_repo = _init_repo(tmp_path / "requested")
    for repo in (cwd_repo, requested_repo):
        (repo / "tracked.txt").write_text("changed\n")
        _git(repo, "add", "tracked.txt")
    cwd_head = _git(cwd_repo, "rev-parse", "HEAD").stdout.strip()
    monkeypatch.chdir(cwd_repo)

    result = CliRunner().invoke(
        app,
        ["commit", "--repo-path", str(requested_repo), "-m", "requested only"],
    )

    assert result.exit_code == 0, result.output
    assert _git(cwd_repo, "rev-parse", "HEAD").stdout.strip() == cwd_head
    assert (
        _git(requested_repo, "show", "-s", "--format=%s", "HEAD").stdout.strip()
        == "requested only"
    )


def test_commit_cli_threads_amend_without_new_staged_changes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from gr2.python_cli.app import app

    repo = _init_repo(tmp_path / "repo")
    monkeypatch.chdir(tmp_path)

    result = CliRunner().invoke(
        app,
        ["commit", "--repo-path", str(repo), "--amend", "-m", "amended through cli"],
    )

    assert result.exit_code == 0, result.output
    assert (
        _git(repo, "show", "-s", "--format=%s", "HEAD").stdout.strip()
        == "amended through cli"
    )


# --- lane-aware commit (impedance B) ---------------------------------------

import argparse

from gr2.prototypes import lane_workspace_prototype as lanes
from gr2.python_cli import commit as commit_ops
from gr2.python_cli.app import app


def _workspace_multi(tmp_path: Path, repos: list[str]) -> Path:
    ws = tmp_path / "ws"
    (ws / ".grip").mkdir(parents=True)
    repo_blocks = "".join(
        f'\n[[repos]]\nname = "{r}"\npath = "repos/{r}"\nurl = "https://example.invalid/{r}.git"\n'
        for r in repos
    )
    (ws / ".grip" / "workspace_spec.toml").write_text(
        f'schema_version = 1\nworkspace_name = "m"\n{repo_blocks}\n'
        f'[[units]]\nname = "atlas"\npath = "agents/atlas"\nrepos = {repos!r}\n'.replace("'", '"')
    )
    return ws


def _materialized_lane(tmp_path: Path, repos: list[str], lane: str = "feature") -> Path:
    """A materialized lane whose repos/<r> checkouts are real git repos."""
    ws = _workspace_multi(tmp_path, repos)
    branch = ",".join(f"{r}=main" for r in repos)
    assert lanes.create_lane(argparse.Namespace(
        workspace_root=ws, owner_unit="atlas", lane_name=lane, type="feature",
        repos=",".join(repos), branch=branch, source="test", default_commands=[],
    )) == 0
    lane_root = lanes.lane_checkout_root(ws, "atlas", lane)
    (lane_root / "repos").mkdir(parents=True)
    for r in repos:
        _init_repo(lane_root / "repos" / r)
    return ws


def _stage_change(repo: Path, name: str = "new.txt", content: str = "x\n") -> None:
    (repo / name).write_text(content)
    _git(repo, "add", name)


def test_lane_commit_commits_each_staged_repo_under_one_message(tmp_path: Path) -> None:
    ws = _materialized_lane(tmp_path, ["a", "b"])
    lane_root = lanes.lane_checkout_root(ws, "atlas", "feature")
    _stage_change(lane_root / "repos" / "a")
    _stage_change(lane_root / "repos" / "b")

    report = commit_ops.commit_lane(ws, "atlas", "one message", lane_name="feature")

    by_repo = {r.repo: r for r in report.results}
    assert by_repo["a"].status == "committed" and by_repo["b"].status == "committed"
    for r in ("a", "b"):
        head = _git(lane_root / "repos" / r, "rev-parse", "HEAD").stdout.strip()
        assert by_repo[r].commit_sha == head
        assert _git(lane_root / "repos" / r, "show", "-s", "--format=%s", "HEAD").stdout.strip() == "one message"
    assert not report.any_failed and report.any_committed


def test_lane_commit_skips_empty_index_repo_and_says_so(tmp_path: Path) -> None:
    # Control: a repo with nothing staged is skipped, never an empty commit.
    ws = _materialized_lane(tmp_path, ["a", "b"])
    lane_root = lanes.lane_checkout_root(ws, "atlas", "feature")
    _stage_change(lane_root / "repos" / "a")  # b left empty
    head_b_before = _git(lane_root / "repos" / "b", "rev-parse", "HEAD").stdout.strip()

    report = commit_ops.commit_lane(ws, "atlas", "msg", lane_name="feature")

    by_repo = {r.repo: r for r in report.results}
    assert by_repo["a"].status == "committed"
    assert by_repo["b"].status == "skipped_empty" and by_repo["b"].commit_sha is None
    assert _git(lane_root / "repos" / "b", "rev-parse", "HEAD").stdout.strip() == head_b_before


def test_lane_commit_failing_repo_does_not_roll_back_others_and_is_named(tmp_path: Path) -> None:
    ws = _materialized_lane(tmp_path, ["a", "b"])
    lane_root = lanes.lane_checkout_root(ws, "atlas", "feature")
    _stage_change(lane_root / "repos" / "a")
    # Make repo b fail its commit: stage a change, then corrupt HEAD so commit errors.
    _stage_change(lane_root / "repos" / "b")
    (lane_root / "repos" / "b" / ".git" / "HEAD").write_text("ref: refs/heads/\n")

    report = commit_ops.commit_lane(ws, "atlas", "msg", lane_name="feature")
    by_repo = {r.repo: r for r in report.results}
    assert by_repo["a"].status == "committed"  # not rolled back
    assert by_repo["b"].status == "failed" and by_repo["b"].error
    assert report.any_failed and report.any_committed


def test_single_repo_cwd_commit_unchanged(tmp_path: Path) -> None:
    # Control: plain gr2 commit -m with no --workspace-root/--owner-unit is
    # single-repo cwd, exactly as before.
    repo = _init_repo(tmp_path / "repo")
    (repo / "new.txt").write_text("x\n")
    _git(repo, "add", "new.txt")
    result = CliRunner().invoke(app, ["commit", "-m", "solo", "--repo-path", str(repo)])
    assert result.exit_code == 0, result.output
    assert "Committed" in result.output
    assert _git(repo, "show", "-s", "--format=%s", "HEAD").stdout.strip() == "solo"


def test_cli_lane_commit_prints_one_line_per_repo(tmp_path: Path) -> None:
    ws = _materialized_lane(tmp_path, ["a", "b"])
    lane_root = lanes.lane_checkout_root(ws, "atlas", "feature")
    _stage_change(lane_root / "repos" / "a")  # b empty
    result = CliRunner().invoke(
        app, ["commit", "-m", "m", "--workspace-root", str(ws), "--owner-unit", "atlas", "--lane", "feature"],
    )
    assert result.exit_code == 0, result.output
    assert "a: committed" in result.output
    assert "b: skipped (empty index)" in result.output


def test_bound_lane_commits_only_the_bound_worktree(tmp_path: Path) -> None:
    # Bound lanes stay single-repo: the one target is the bound worktree, not a
    # repos/<key> checkout under the lane state tree.
    wt = _init_repo(tmp_path / "wt")
    doc = {"lane_kind": "bound", "repos": ["only"], "bound_worktree": str(wt)}
    targets = commit_ops._lane_repo_targets(tmp_path, "atlas", "b", doc)
    assert targets == [("only", Path(str(wt)))]
