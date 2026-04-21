"""TDD tests for Phase 0 grip object model.

Tests define the interface contract for:
  - gr grip snapshot: walk repos, build git tree, create grip commit in .grip/
  - gr grip log: display grip commit history
  - gr grip diff: show changes between two grip commits
  - gr grip checkout: restore workspace repo HEADs from a grip commit
  - round-trip: snapshot → checkout → verify state matches
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from python_cli.grip import (
    GripCommitInfo,
    GripDiff,
    grip_checkout,
    grip_diff,
    grip_init,
    grip_log,
    grip_snapshot,
)
from python_cli.gitops import git


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _init_repo(path: Path, *, name: str = "test") -> Path:
    """Create a git repo with one commit and return its path."""
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init")
    git(path, "config", "user.email", "test@test.com")
    git(path, "config", "user.name", "Test")
    readme = path / "README.md"
    readme.write_text(f"# {name}\n")
    git(path, "add", ".")
    git(path, "commit", "-m", f"init {name}")
    return path


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """Create a workspace with two repos (recall, config) each with one commit."""
    ws = tmp_path / "ws"
    ws.mkdir()

    _init_repo(ws / "recall", name="recall")
    git(ws / "recall", "remote", "add", "origin", "https://github.com/synapt-dev/recall")

    _init_repo(ws / "config", name="config")
    git(ws / "config", "remote", "add", "origin", "https://github.com/synapt-dev/config")

    return ws


@pytest.fixture
def grip_repo(workspace: Path) -> Path:
    """Initialize the .grip/ git repo and return workspace path."""
    grip_init(workspace)
    return workspace


# ---------------------------------------------------------------------------
# grip_init
# ---------------------------------------------------------------------------


class TestGripInit:
    def test_creates_dot_grip_git_repo(self, workspace: Path) -> None:
        grip_init(workspace)
        grip_dir = workspace / ".grip"
        assert grip_dir.is_dir()
        assert (grip_dir / ".git").exists()

    def test_idempotent(self, workspace: Path) -> None:
        grip_init(workspace)
        grip_init(workspace)
        assert (workspace / ".grip" / ".git").exists()


# ---------------------------------------------------------------------------
# grip_snapshot
# ---------------------------------------------------------------------------


class TestGripSnapshot:
    def test_returns_commit_sha(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall", "config": grip_repo / "config"},
        )
        assert isinstance(sha, str)
        assert len(sha) >= 40

    def test_creates_git_commit_in_grip_repo(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall", "config": grip_repo / "config"},
        )
        proc = git(grip_repo / ".grip", "cat-file", "-t", sha)
        assert proc.stdout.strip() == "commit"

    def test_tree_has_repos_subtree(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        proc = git(grip_repo / ".grip", "ls-tree", sha)
        entries = proc.stdout.strip().splitlines()
        tree_names = [e.split("\t")[-1] for e in entries]
        assert "repos" in tree_names

    def test_repo_subtree_has_commit_blob(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        proc = git(grip_repo / ".grip", "ls-tree", f"{sha}:repos/recall")
        entry_names = [e.split("\t")[-1] for e in proc.stdout.strip().splitlines()]
        assert "commit" in entry_names

    def test_commit_blob_matches_repo_head(self, grip_repo: Path) -> None:
        recall_head = git(grip_repo / "recall", "rev-parse", "HEAD").stdout.strip()
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        blob = git(grip_repo / ".grip", "show", f"{sha}:repos/recall/commit").stdout.strip()
        assert blob == recall_head

    def test_branch_blob_matches_current_branch(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        blob = git(grip_repo / ".grip", "show", f"{sha}:repos/recall/branch").stdout.strip()
        expected = git(grip_repo / "recall", "branch", "--show-current").stdout.strip()
        assert blob == expected

    def test_remote_blob_matches_origin(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        blob = git(grip_repo / ".grip", "show", f"{sha}:repos/recall/remote").stdout.strip()
        assert blob == "https://github.com/synapt-dev/recall"

    def test_multiple_repos_in_tree(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={
                "recall": grip_repo / "recall",
                "config": grip_repo / "config",
            },
        )
        proc = git(grip_repo / ".grip", "ls-tree", f"{sha}:repos")
        entry_names = [e.split("\t")[-1] for e in proc.stdout.strip().splitlines()]
        assert "recall" in entry_names
        assert "config" in entry_names

    def test_changeset_metadata_type(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
            changeset_type="ceremony",
        )
        blob = git(grip_repo / ".grip", "show", f"{sha}:.grip/type").stdout.strip()
        assert blob == "ceremony"

    def test_changeset_metadata_sprint(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
            sprint="27",
        )
        blob = git(grip_repo / ".grip", "show", f"{sha}:.grip/sprint").stdout.strip()
        assert blob == "27"

    def test_commit_message_includes_type(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
            changeset_type="ceremony",
            message="Sprint 26 ceremony",
        )
        proc = git(grip_repo / ".grip", "log", "-1", "--format=%s", sha)
        assert "ceremony" in proc.stdout.strip().lower() or "Sprint 26" in proc.stdout.strip()

    def test_updates_head_ref(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        head = git(grip_repo / ".grip", "rev-parse", "HEAD").stdout.strip()
        assert head == sha

    def test_successive_snapshots_form_chain(self, grip_repo: Path) -> None:
        sha1 = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        # Make a change in recall
        (grip_repo / "recall" / "new.txt").write_text("change")
        git(grip_repo / "recall", "add", ".")
        git(grip_repo / "recall", "commit", "-m", "second commit")

        sha2 = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        assert sha1 != sha2
        parent = git(grip_repo / ".grip", "rev-parse", f"{sha2}^").stdout.strip()
        assert parent == sha1


# ---------------------------------------------------------------------------
# grip_log
# ---------------------------------------------------------------------------


class TestGripLog:
    def test_returns_list_of_commit_info(self, grip_repo: Path) -> None:
        grip_snapshot(grip_repo, repos={"recall": grip_repo / "recall"})
        entries = grip_log(grip_repo)
        assert isinstance(entries, list)
        assert len(entries) >= 1
        assert isinstance(entries[0], GripCommitInfo)

    def test_commit_info_has_sha(self, grip_repo: Path) -> None:
        sha = grip_snapshot(grip_repo, repos={"recall": grip_repo / "recall"})
        entries = grip_log(grip_repo)
        assert entries[0].sha == sha

    def test_commit_info_has_message(self, grip_repo: Path) -> None:
        grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
            message="test snapshot",
        )
        entries = grip_log(grip_repo)
        assert "test snapshot" in entries[0].message

    def test_commit_info_has_repo_names(self, grip_repo: Path) -> None:
        grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall", "config": grip_repo / "config"},
        )
        entries = grip_log(grip_repo)
        assert "recall" in entries[0].repos
        assert "config" in entries[0].repos

    def test_max_count_limits_results(self, grip_repo: Path) -> None:
        for i in range(5):
            (grip_repo / "recall" / f"file{i}.txt").write_text(str(i))
            git(grip_repo / "recall", "add", ".")
            git(grip_repo / "recall", "commit", "-m", f"commit {i}")
            grip_snapshot(grip_repo, repos={"recall": grip_repo / "recall"})

        entries = grip_log(grip_repo, max_count=3)
        assert len(entries) == 3

    def test_most_recent_first(self, grip_repo: Path) -> None:
        sha1 = grip_snapshot(
            grip_repo, repos={"recall": grip_repo / "recall"}, message="first"
        )
        (grip_repo / "recall" / "change.txt").write_text("x")
        git(grip_repo / "recall", "add", ".")
        git(grip_repo / "recall", "commit", "-m", "second recall commit")
        sha2 = grip_snapshot(
            grip_repo, repos={"recall": grip_repo / "recall"}, message="second"
        )
        entries = grip_log(grip_repo)
        assert entries[0].sha == sha2
        assert entries[1].sha == sha1

    def test_empty_log_on_fresh_grip(self, workspace: Path) -> None:
        grip_init(workspace)
        entries = grip_log(workspace)
        assert entries == []


# ---------------------------------------------------------------------------
# grip_diff
# ---------------------------------------------------------------------------


class TestGripDiff:
    def _two_snapshots(self, grip_repo: Path) -> tuple[str, str]:
        """Create two snapshots with different recall HEAD."""
        sha1 = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall", "config": grip_repo / "config"},
        )
        (grip_repo / "recall" / "diff.txt").write_text("changed")
        git(grip_repo / "recall", "add", ".")
        git(grip_repo / "recall", "commit", "-m", "change for diff")
        sha2 = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall", "config": grip_repo / "config"},
        )
        return sha1, sha2

    def test_returns_grip_diff(self, grip_repo: Path) -> None:
        sha1, sha2 = self._two_snapshots(grip_repo)
        result = grip_diff(grip_repo, sha1, sha2)
        assert isinstance(result, GripDiff)

    def test_changed_repos(self, grip_repo: Path) -> None:
        sha1, sha2 = self._two_snapshots(grip_repo)
        result = grip_diff(grip_repo, sha1, sha2)
        assert "recall" in result.changed

    def test_unchanged_repos_not_in_changed(self, grip_repo: Path) -> None:
        sha1, sha2 = self._two_snapshots(grip_repo)
        result = grip_diff(grip_repo, sha1, sha2)
        assert "config" not in result.changed

    def test_changed_entry_has_old_and_new_commit(self, grip_repo: Path) -> None:
        sha1, sha2 = self._two_snapshots(grip_repo)
        result = grip_diff(grip_repo, sha1, sha2)
        recall_diff = result.changed["recall"]
        assert "old_commit" in recall_diff
        assert "new_commit" in recall_diff
        assert recall_diff["old_commit"] != recall_diff["new_commit"]

    def test_added_repo(self, grip_repo: Path) -> None:
        sha1 = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        sha2 = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall", "config": grip_repo / "config"},
        )
        result = grip_diff(grip_repo, sha1, sha2)
        assert "config" in result.added

    def test_removed_repo(self, grip_repo: Path) -> None:
        sha1 = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall", "config": grip_repo / "config"},
        )
        sha2 = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        result = grip_diff(grip_repo, sha1, sha2)
        assert "config" in result.removed

    def test_identical_snapshots_no_changes(self, grip_repo: Path) -> None:
        sha = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        result = grip_diff(grip_repo, sha, sha)
        assert len(result.changed) == 0
        assert len(result.added) == 0
        assert len(result.removed) == 0


# ---------------------------------------------------------------------------
# grip_checkout
# ---------------------------------------------------------------------------


class TestGripCheckout:
    def test_returns_repo_shas(self, grip_repo: Path) -> None:
        grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        result = grip_checkout(grip_repo, "HEAD")
        assert isinstance(result, dict)
        assert "recall" in result

    def test_sha_matches_snapshot(self, grip_repo: Path) -> None:
        recall_head = git(grip_repo / "recall", "rev-parse", "HEAD").stdout.strip()
        grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        result = grip_checkout(grip_repo, "HEAD")
        assert result["recall"] == recall_head

    def test_checkout_older_snapshot(self, grip_repo: Path) -> None:
        old_head = git(grip_repo / "recall", "rev-parse", "HEAD").stdout.strip()
        sha1 = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        (grip_repo / "recall" / "new.txt").write_text("new")
        git(grip_repo / "recall", "add", ".")
        git(grip_repo / "recall", "commit", "-m", "newer commit")
        grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        result = grip_checkout(grip_repo, sha1)
        assert result["recall"] == old_head

    def test_checkout_detaches_head_to_commit(self, grip_repo: Path) -> None:
        grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        (grip_repo / "recall" / "new.txt").write_text("new")
        git(grip_repo / "recall", "add", ".")
        git(grip_repo / "recall", "commit", "-m", "newer")
        sha2 = grip_snapshot(
            grip_repo,
            repos={"recall": grip_repo / "recall"},
        )
        # Checkout the first snapshot (HEAD~1 in .grip/)
        grip_checkout(grip_repo, f"{sha2}~1")
        actual_head = git(grip_repo / "recall", "rev-parse", "HEAD").stdout.strip()
        # After checkout, recall's HEAD should match the older grip snapshot
        first_snap_recall = git(
            grip_repo / ".grip", "show", f"{sha2}~1:repos/recall/commit"
        ).stdout.strip()
        assert actual_head == first_snap_recall


# ---------------------------------------------------------------------------
# Round-trip
# ---------------------------------------------------------------------------


class TestRoundTrip:
    def test_snapshot_checkout_preserves_state(self, grip_repo: Path) -> None:
        """Snapshot current state, advance repos, checkout old snapshot, verify."""
        repos = {
            "recall": grip_repo / "recall",
            "config": grip_repo / "config",
        }
        original_heads = {
            name: git(path, "rev-parse", "HEAD").stdout.strip()
            for name, path in repos.items()
        }
        snap_sha = grip_snapshot(grip_repo, repos=repos, message="round-trip base")

        # Advance both repos
        for name, path in repos.items():
            (path / "advance.txt").write_text(f"advanced {name}")
            git(path, "add", ".")
            git(path, "commit", "-m", f"advance {name}")
        grip_snapshot(grip_repo, repos=repos, message="advanced")

        # Checkout the original snapshot
        restored = grip_checkout(grip_repo, snap_sha)
        for name, expected_sha in original_heads.items():
            assert restored[name] == expected_sha

    def test_snapshot_diff_log_integration(self, grip_repo: Path) -> None:
        """Full workflow: two snapshots, log shows both, diff shows changes."""
        repos = {"recall": grip_repo / "recall", "config": grip_repo / "config"}
        sha1 = grip_snapshot(grip_repo, repos=repos, message="snap 1")

        (grip_repo / "recall" / "change.txt").write_text("changed")
        git(grip_repo / "recall", "add", ".")
        git(grip_repo / "recall", "commit", "-m", "update recall")
        sha2 = grip_snapshot(grip_repo, repos=repos, message="snap 2")

        entries = grip_log(grip_repo, max_count=10)
        shas = [e.sha for e in entries]
        assert sha1 in shas
        assert sha2 in shas

        diff = grip_diff(grip_repo, sha1, sha2)
        assert "recall" in diff.changed
        assert "config" not in diff.changed
