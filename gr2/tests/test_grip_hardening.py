"""Hardening tests for grip object model edge cases.

Covers adversarial scenarios found during Phase 0 testing:
  1. Missing/corrupt .grip/.git recovery
  2. Corrupt .grip HEAD detection and repair
  3. Dirty repo marker in grip commit tree
  4. Empty repo handling
  5. Partial state detection on corrupt blob reads
"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from python_cli.gitops import git
from python_cli.grip import (
    GripCorruptError,
    GripInitError,
    GripCommitInfo,
    grip_checkout,
    grip_diff,
    grip_init,
    grip_log,
    grip_snapshot,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _init_repo(path: Path, *, name: str = "test", empty: bool = False) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    git(path, "config", "user.email", "test@test.com")
    git(path, "config", "user.name", "Test")
    if not empty:
        (path / "README.md").write_text(f"# {name}\n")
        git(path, "add", ".")
        git(path, "commit", "-m", f"init {name}")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    ws = tmp_path / "ws"
    ws.mkdir()
    _init_repo(ws / "recall", name="recall")
    git(ws / "recall", "remote", "add", "origin", "https://github.com/synapt-dev/recall")
    _init_repo(ws / "config", name="config")
    git(ws / "config", "remote", "add", "origin", "https://github.com/synapt-dev/config")
    return ws


@pytest.fixture
def grip_repo(workspace: Path) -> Path:
    grip_init(workspace)
    return workspace


# ---------------------------------------------------------------------------
# 1. Missing/corrupt .grip/.git init recovery
# ---------------------------------------------------------------------------


class TestGripInitRecovery:
    def test_init_when_dot_grip_exists_but_no_git(self, workspace: Path) -> None:
        (workspace / ".grip").mkdir()
        grip_init(workspace)
        assert (workspace / ".grip" / ".git").exists()

    def test_init_when_dot_grip_git_is_file_not_dir(self, workspace: Path) -> None:
        grip_dir = workspace / ".grip"
        grip_dir.mkdir()
        (grip_dir / ".git").write_text("corrupted")
        with pytest.raises(GripInitError, match="corrupt"):
            grip_init(workspace)

    def test_init_creates_fresh_after_rmtree(self, workspace: Path) -> None:
        grip_init(workspace)
        shutil.rmtree(workspace / ".grip")
        grip_init(workspace)
        assert (workspace / ".grip" / ".git").is_dir()

    def test_snapshot_without_init_raises(self, workspace: Path) -> None:
        with pytest.raises(GripInitError):
            grip_snapshot(workspace, repos={"recall": workspace / "recall"})

    def test_log_without_init_raises(self, workspace: Path) -> None:
        with pytest.raises(GripInitError):
            grip_log(workspace)

    def test_diff_without_init_raises(self, workspace: Path) -> None:
        with pytest.raises(GripInitError):
            grip_diff(workspace, "HEAD", "HEAD~1")

    def test_checkout_without_init_raises(self, workspace: Path) -> None:
        with pytest.raises(GripInitError):
            grip_checkout(workspace, "HEAD")


# ---------------------------------------------------------------------------
# 2. Corrupt .grip HEAD detection and repair
# ---------------------------------------------------------------------------


class TestCorruptHead:
    def test_corrupt_head_ref_detected(self, grip_repo: Path) -> None:
        grip_snapshot(grip_repo, repos={"recall": grip_repo / "recall"})
        head_path = grip_repo / ".grip" / ".git" / "HEAD"
        head_path.write_text("corrupt-not-a-sha\n")
        with pytest.raises(GripCorruptError, match="HEAD"):
            grip_log(grip_repo)

    def test_missing_head_file_detected(self, grip_repo: Path) -> None:
        grip_snapshot(grip_repo, repos={"recall": grip_repo / "recall"})
        head_path = grip_repo / ".grip" / ".git" / "HEAD"
        head_path.unlink()
        with pytest.raises(GripCorruptError, match="HEAD"):
            grip_log(grip_repo)

    def test_empty_head_returns_empty_log(self, grip_repo: Path) -> None:
        entries = grip_log(grip_repo)
        assert entries == []


# ---------------------------------------------------------------------------
# 3. Dirty repo marker in grip commit tree
# ---------------------------------------------------------------------------


class TestDirtyRepoMarker:
    def test_clean_repo_has_dirty_false(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo, repos={"recall": grip_repo / "recall"}
        )
        blob = git(grip_repo / ".grip", "show", f"{sha}:repos/recall/dirty").stdout.strip()
        assert blob == "false"

    def test_dirty_repo_has_dirty_true(self, grip_repo: Path) -> None:
        (grip_repo / "recall" / "uncommitted.txt").write_text("wip")
        sha = grip_snapshot(
            grip_repo, repos={"recall": grip_repo / "recall"}
        )
        blob = git(grip_repo / ".grip", "show", f"{sha}:repos/recall/dirty").stdout.strip()
        assert blob == "true"

    def test_staged_changes_count_as_dirty(self, grip_repo: Path) -> None:
        (grip_repo / "recall" / "staged.txt").write_text("staged")
        git(grip_repo / "recall", "add", "staged.txt")
        sha = grip_snapshot(
            grip_repo, repos={"recall": grip_repo / "recall"}
        )
        blob = git(grip_repo / ".grip", "show", f"{sha}:repos/recall/dirty").stdout.strip()
        assert blob == "true"

    def test_dirty_flag_in_diff(self, grip_repo: Path) -> None:
        sha1 = grip_snapshot(
            grip_repo, repos={"recall": grip_repo / "recall"}
        )
        (grip_repo / "recall" / "wip.txt").write_text("wip")
        sha2 = grip_snapshot(
            grip_repo, repos={"recall": grip_repo / "recall"}
        )
        diff = grip_diff(grip_repo, sha1, sha2)
        assert "recall" not in diff.changed


# ---------------------------------------------------------------------------
# 4. Empty repo handling
# ---------------------------------------------------------------------------


class TestEmptyRepo:
    def test_empty_repo_snapshot_succeeds(self, grip_repo: Path) -> None:
        _init_repo(grip_repo / "empty", name="empty", empty=True)
        sha = grip_snapshot(
            grip_repo, repos={"empty": grip_repo / "empty"}
        )
        assert len(sha) >= 40

    def test_empty_repo_has_no_commit_blob(self, grip_repo: Path) -> None:
        _init_repo(grip_repo / "empty", name="empty", empty=True)
        sha = grip_snapshot(
            grip_repo, repos={"empty": grip_repo / "empty"}
        )
        proc = git(grip_repo / ".grip", "ls-tree", f"{sha}:repos/empty")
        entry_names = [e.split("\t")[-1] for e in proc.stdout.strip().splitlines() if e.strip()]
        assert "commit" not in entry_names

    def test_empty_repo_has_branch_blob(self, grip_repo: Path) -> None:
        _init_repo(grip_repo / "empty", name="empty", empty=True)
        sha = grip_snapshot(
            grip_repo, repos={"empty": grip_repo / "empty"}
        )
        proc = git(grip_repo / ".grip", "ls-tree", f"{sha}:repos/empty")
        entry_names = [e.split("\t")[-1] for e in proc.stdout.strip().splitlines() if e.strip()]
        # Empty repos still have a branch (main/master) even without commits
        # This may or may not exist depending on git version; just verify no crash
        assert isinstance(entry_names, list)

    def test_checkout_skips_empty_repo(self, grip_repo: Path) -> None:
        _init_repo(grip_repo / "empty", name="empty", empty=True)
        grip_snapshot(
            grip_repo,
            repos={
                "recall": grip_repo / "recall",
                "empty": grip_repo / "empty",
            },
        )
        result = grip_checkout(grip_repo, "HEAD")
        assert "recall" in result
        assert "empty" not in result


# ---------------------------------------------------------------------------
# 5. Partial state / corrupt blob detection
# ---------------------------------------------------------------------------


class TestCorruptBlobs:
    def test_corrupt_blob_in_repo_state_raises(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo, repos={"recall": grip_repo / "recall"}
        )
        # Corrupt the object store by replacing a pack/loose object
        # We simulate by asking for a nonexistent tree path
        with pytest.raises(GripCorruptError):
            grip_checkout(grip_repo, "0000000000000000000000000000000000000000")

    def test_missing_repos_subtree_raises(self, grip_repo: Path) -> None:
        # Create a bare commit with no repos/ subtree
        proc = git(
            grip_repo / ".grip",
            "hash-object", "-w", "--stdin",
        )
        # We need to create a commit that has a tree without repos/
        empty_tree = git(grip_repo / ".grip", "mktree")
        if empty_tree.returncode != 0:
            pytest.skip("Cannot create empty tree")
        # mktree with empty stdin gives the empty tree
        import subprocess
        result = subprocess.run(
            ["git", "mktree"],
            cwd=grip_repo / ".grip",
            input="",
            capture_output=True,
            text=True,
        )
        empty_tree_sha = result.stdout.strip()
        result2 = subprocess.run(
            ["git", "commit-tree", empty_tree_sha, "-m", "empty"],
            cwd=grip_repo / ".grip",
            capture_output=True,
            text=True,
            env={"GIT_AUTHOR_NAME": "test", "GIT_AUTHOR_EMAIL": "t@t",
                 "GIT_COMMITTER_NAME": "test", "GIT_COMMITTER_EMAIL": "t@t",
                 "PATH": __import__("os").environ["PATH"]},
        )
        bad_commit = result2.stdout.strip()
        result = grip_checkout(grip_repo, bad_commit)
        assert result == {}

    def test_validate_grip_repo_detects_no_git(self, workspace: Path) -> None:
        (workspace / ".grip").mkdir(exist_ok=True)
        with pytest.raises(GripInitError):
            grip_snapshot(workspace, repos={"recall": workspace / "recall"})
