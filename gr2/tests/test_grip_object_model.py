"""TDD specs for grip#606: gr grip snapshot/log/diff/checkout.

These tests define the Phase 0 acceptance contract for the grip object model.
Nothing in this module should be loosened to match a partial implementation.
The commands do not exist yet, so the suite is intentionally red.

Acceptance criteria by command:

snapshot
- walks workspace repos and records current git state into `.grip/`
- succeeds for multi-repo workspaces and empty repos
- records detached HEAD state explicitly
- blocks on dirty repos unless the command surface later declares otherwise
- fails cleanly when `.grip/` is missing

log
- lists stored grip snapshots in reverse chronological order
- includes snapshot ids and summary metadata
- fails cleanly when `.grip/` is missing

diff
- compares two grip snapshots and reports per-repo changes
- reports "no changes" when the snapshots are identical
- fails cleanly when requested snapshot ids do not exist

checkout
- restores repo HEADs to the snapshot state for all repos in the workspace
- restores detached HEAD snapshots correctly
- blocks on dirty repos unless forced semantics are added explicitly later
- fails cleanly when `.grip/` is missing
"""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gr2.python_cli.app import app


runner = CliRunner()


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_repo(root: Path, name: str, *, with_commit: bool = True) -> Path:
    repo = root / name
    repo.mkdir(parents=True, exist_ok=True)
    assert _git(repo, "init", "-b", "main").returncode == 0
    assert _git(repo, "config", "user.name", "Sentinel").returncode == 0
    assert _git(repo, "config", "user.email", "sentinel@example.com").returncode == 0
    if with_commit:
        (repo / "README.md").write_text(f"# {name}\n")
        assert _git(repo, "add", "README.md").returncode == 0
        assert _git(repo, "commit", "-m", "initial").returncode == 0
    return repo


def _commit_file(repo: Path, relative_path: str, content: str, message: str) -> str:
    path = repo / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content)
    assert _git(repo, "add", relative_path).returncode == 0
    assert _git(repo, "commit", "-m", message).returncode == 0
    return _git(repo, "rev-parse", "HEAD").stdout.strip()


def _write_workspace_spec(workspace_root: Path, repos: list[tuple[str, str]]) -> None:
    spec_path = workspace_root / ".grip" / "workspace_spec.toml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    repo_blocks = []
    for repo_name, repo_path in repos:
        repo_blocks.append(
            textwrap.dedent(
                f"""
                [[repos]]
                name = "{repo_name}"
                path = "{repo_path}"
                url = "https://example.com/{repo_name}.git"
                """
            ).strip()
        )
    spec_path.write_text(
        textwrap.dedent(
            f"""
            workspace_name = "{workspace_root.name}"

            {'\n\n'.join(repo_blocks)}
            """
        ).strip()
        + "\n"
    )


def _read_snapshot_index(workspace_root: Path) -> list[dict[str, object]]:
    index_path = workspace_root / ".grip" / "snapshots" / "index.json"
    return json.loads(index_path.read_text())


def _workspace_with_repos(tmp_path: Path) -> tuple[Path, Path, Path]:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    app_repo = _init_repo(workspace_root, "app")
    docs_repo = _init_repo(workspace_root, "docs")
    _write_workspace_spec(workspace_root, [("app", "app"), ("docs", "docs")])
    return workspace_root, app_repo, docs_repo


class TestGripSnapshot:
    def test_snapshot_records_multi_repo_heads_and_metadata(self, tmp_path: Path) -> None:
        """snapshot must capture one entry per repo with current HEAD metadata."""
        workspace_root, app_repo, docs_repo = _workspace_with_repos(tmp_path)

        result = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "baseline"])

        assert result.exit_code == 0, result.stdout
        index = _read_snapshot_index(workspace_root)
        assert len(index) == 1
        snapshot = index[0]
        assert snapshot["message"] == "baseline"
        assert set(snapshot["repos"]) == {"app", "docs"}
        assert snapshot["repo_states"]["app"]["head"] == _git(app_repo, "rev-parse", "HEAD").stdout.strip()
        assert snapshot["repo_states"]["docs"]["head"] == _git(docs_repo, "rev-parse", "HEAD").stdout.strip()

    def test_snapshot_accepts_empty_repo_without_commits(self, tmp_path: Path) -> None:
        """snapshot must classify an initialized-but-empty repo without crashing."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        empty_repo = _init_repo(workspace_root, "empty", with_commit=False)
        _write_workspace_spec(workspace_root, [("empty", "empty")])

        result = runner.invoke(app, ["grip", "snapshot", str(workspace_root)])

        assert result.exit_code == 0, result.stdout
        index = _read_snapshot_index(workspace_root)
        assert index[0]["repo_states"]["empty"]["is_empty"] is True
        assert index[0]["repo_states"]["empty"]["head"] is None

    def test_snapshot_marks_detached_head_state_explicitly(self, tmp_path: Path) -> None:
        """snapshot must persist detached HEAD so checkout can restore it later."""
        workspace_root, app_repo, _docs_repo = _workspace_with_repos(tmp_path)
        detached_sha = _git(app_repo, "rev-parse", "HEAD").stdout.strip()
        assert _git(app_repo, "checkout", detached_sha).returncode == 0

        result = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "detached"])

        assert result.exit_code == 0, result.stdout
        snapshot = _read_snapshot_index(workspace_root)[0]
        assert snapshot["repo_states"]["app"]["head"] == detached_sha
        assert snapshot["repo_states"]["app"]["head_state"] == "detached"

    def test_snapshot_blocks_when_repo_has_uncommitted_changes(self, tmp_path: Path) -> None:
        """snapshot must fail cleanly on a dirty repo until explicit dirty semantics exist."""
        workspace_root, app_repo, _docs_repo = _workspace_with_repos(tmp_path)
        (app_repo / "dirty.txt").write_text("uncommitted\n")

        result = runner.invoke(app, ["grip", "snapshot", str(workspace_root)])

        assert result.exit_code != 0
        assert "dirty" in result.stdout.lower()

    def test_snapshot_fails_cleanly_when_grip_dir_missing(self, tmp_path: Path) -> None:
        """snapshot must explain missing .grip state instead of crashing."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _init_repo(workspace_root, "app")

        result = runner.invoke(app, ["grip", "snapshot", str(workspace_root)])

        assert result.exit_code != 0
        assert ".grip" in result.stdout


class TestGripLog:
    def test_log_lists_snapshots_newest_first_with_summary(self, tmp_path: Path) -> None:
        """log must return reverse-chronological snapshots with id and message."""
        workspace_root, app_repo, _docs_repo = _workspace_with_repos(tmp_path)
        first = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "baseline"])
        assert first.exit_code == 0, first.stdout
        _commit_file(app_repo, "CHANGELOG.md", "v2\n", "update")
        second = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "after-update"])
        assert second.exit_code == 0, second.stdout

        result = runner.invoke(app, ["grip", "log", str(workspace_root)])

        assert result.exit_code == 0, result.stdout
        lines = [line for line in result.stdout.splitlines() if line.strip()]
        assert "after-update" in lines[0]
        assert "baseline" in lines[1]

    def test_log_fails_cleanly_when_grip_dir_missing(self, tmp_path: Path) -> None:
        """log must fail with a clear error when no grip object store exists."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()

        result = runner.invoke(app, ["grip", "log", str(workspace_root)])

        assert result.exit_code != 0
        assert ".grip" in result.stdout


class TestGripDiff:
    def test_diff_reports_per_repo_head_changes_between_snapshots(self, tmp_path: Path) -> None:
        """diff must report changed repos between two snapshots."""
        workspace_root, app_repo, docs_repo = _workspace_with_repos(tmp_path)
        first = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "baseline"])
        assert first.exit_code == 0, first.stdout
        _commit_file(app_repo, "CHANGELOG.md", "v2\n", "app update")
        _commit_file(docs_repo, "guide.md", "hello\n", "docs update")
        second = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "changed"])
        assert second.exit_code == 0, second.stdout

        index = _read_snapshot_index(workspace_root)
        first_id = index[0]["id"]
        second_id = index[1]["id"]
        result = runner.invoke(app, ["grip", "diff", str(workspace_root), first_id, second_id])

        assert result.exit_code == 0, result.stdout
        assert "app" in result.stdout
        assert "docs" in result.stdout
        assert "changed" in result.stdout.lower() or "->" in result.stdout

    def test_diff_reports_no_changes_for_identical_snapshots(self, tmp_path: Path) -> None:
        """diff must say no changes when repo state is identical."""
        workspace_root, _app_repo, _docs_repo = _workspace_with_repos(tmp_path)
        first = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "first"])
        assert first.exit_code == 0, first.stdout
        second = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "second"])
        assert second.exit_code == 0, second.stdout

        index = _read_snapshot_index(workspace_root)
        first_id = index[0]["id"]
        second_id = index[1]["id"]
        result = runner.invoke(app, ["grip", "diff", str(workspace_root), first_id, second_id])

        assert result.exit_code == 0, result.stdout
        assert "no changes" in result.stdout.lower()

    def test_diff_fails_for_missing_snapshot_id(self, tmp_path: Path) -> None:
        """diff must fail cleanly when either requested snapshot id is unknown."""
        workspace_root, _app_repo, _docs_repo = _workspace_with_repos(tmp_path)
        created = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "only"])
        assert created.exit_code == 0, created.stdout
        existing_id = _read_snapshot_index(workspace_root)[0]["id"]

        result = runner.invoke(app, ["grip", "diff", str(workspace_root), existing_id, "missing-snapshot"])

        assert result.exit_code != 0
        assert "missing" in result.stdout.lower()


class TestGripCheckout:
    def test_checkout_restores_all_repo_heads_to_snapshot(self, tmp_path: Path) -> None:
        """checkout must restore repo HEADs across the whole workspace."""
        workspace_root, app_repo, docs_repo = _workspace_with_repos(tmp_path)
        baseline = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "baseline"])
        assert baseline.exit_code == 0, baseline.stdout
        baseline_id = _read_snapshot_index(workspace_root)[0]["id"]
        baseline_app_head = _git(app_repo, "rev-parse", "HEAD").stdout.strip()
        baseline_docs_head = _git(docs_repo, "rev-parse", "HEAD").stdout.strip()

        _commit_file(app_repo, "CHANGELOG.md", "v2\n", "app update")
        _commit_file(docs_repo, "guide.md", "hello\n", "docs update")

        result = runner.invoke(app, ["grip", "checkout", str(workspace_root), baseline_id])

        assert result.exit_code == 0, result.stdout
        assert _git(app_repo, "rev-parse", "HEAD").stdout.strip() == baseline_app_head
        assert _git(docs_repo, "rev-parse", "HEAD").stdout.strip() == baseline_docs_head

    def test_checkout_restores_detached_head_snapshot(self, tmp_path: Path) -> None:
        """checkout must restore detached HEAD state when the snapshot captured one."""
        workspace_root, app_repo, _docs_repo = _workspace_with_repos(tmp_path)
        detached_sha = _git(app_repo, "rev-parse", "HEAD").stdout.strip()
        assert _git(app_repo, "checkout", detached_sha).returncode == 0
        created = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "detached"])
        assert created.exit_code == 0, created.stdout
        snapshot_id = _read_snapshot_index(workspace_root)[0]["id"]

        assert _git(app_repo, "switch", "main").returncode == 0
        _commit_file(app_repo, "CHANGELOG.md", "v2\n", "app update")

        result = runner.invoke(app, ["grip", "checkout", str(workspace_root), snapshot_id])

        assert result.exit_code == 0, result.stdout
        assert _git(app_repo, "rev-parse", "HEAD").stdout.strip() == detached_sha
        branch = _git(app_repo, "branch", "--show-current").stdout.strip()
        assert branch == ""

    def test_checkout_blocks_when_target_repo_is_dirty(self, tmp_path: Path) -> None:
        """checkout must fail cleanly rather than overwrite uncommitted changes."""
        workspace_root, app_repo, _docs_repo = _workspace_with_repos(tmp_path)
        created = runner.invoke(app, ["grip", "snapshot", str(workspace_root), "--message", "baseline"])
        assert created.exit_code == 0, created.stdout
        snapshot_id = _read_snapshot_index(workspace_root)[0]["id"]
        _commit_file(app_repo, "CHANGELOG.md", "v2\n", "update")
        (app_repo / "dirty.txt").write_text("uncommitted\n")

        result = runner.invoke(app, ["grip", "checkout", str(workspace_root), snapshot_id])

        assert result.exit_code != 0
        assert "dirty" in result.stdout.lower()

    def test_checkout_fails_cleanly_when_grip_dir_missing(self, tmp_path: Path) -> None:
        """checkout must explain missing grip state instead of crashing."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()

        result = runner.invoke(app, ["grip", "checkout", str(workspace_root), "snapshot-123"])

        assert result.exit_code != 0
        assert ".grip" in result.stdout
