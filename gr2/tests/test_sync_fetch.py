"""TDD specs for grip#562: sync must fetch existing repos from remote.

The current sync flow refreshes the bare cache but never fetches into
working checkouts. An existing repo at repos/app/ never gets new commits
from origin on re-sync. These tests enforce the missing fetch behavior.

Tests cover:
1. gitops.fetch_repo primitive
2. build_sync_plan generates fetch_shared_repo for existing repos
3. _execute_operation handles fetch_shared_repo
4. sync.repo_fetched event emission
5. End-to-end: second sync brings remote commits into local tracking refs
"""
from __future__ import annotations

import json
import subprocess
import textwrap
from pathlib import Path

import pytest

from gr2.python_cli.gitops import (
    current_head_sha,
    is_git_repo,
)
from gr2.python_cli.syncops import (
    build_sync_plan,
    run_sync,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_bare_remote(tmp_path: Path, name: str) -> tuple[Path, str]:
    source = tmp_path / f"{name}-src"
    source.mkdir(parents=True, exist_ok=True)
    assert _git(source, "init", "-b", "main").returncode == 0
    assert _git(source, "config", "user.name", "Test").returncode == 0
    assert _git(source, "config", "user.email", "test@example.com").returncode == 0
    (source / "README.md").write_text(f"# {name}\n")
    assert _git(source, "add", "README.md").returncode == 0
    assert _git(source, "commit", "-m", "initial").returncode == 0

    remote = tmp_path / f"{name}.git"
    assert subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        capture_output=True, text=True, check=False,
    ).returncode == 0
    return remote, remote.as_uri()


def _push_new_commit(remote: Path, name: str, filename: str = "new.txt") -> str:
    """Push a new commit to the bare remote. Returns the new HEAD sha."""
    clone = remote.parent / f"{name}-push-clone"
    if clone.exists():
        import shutil
        shutil.rmtree(clone)
    assert subprocess.run(
        ["git", "clone", str(remote), str(clone)],
        capture_output=True, text=True, check=False,
    ).returncode == 0
    assert _git(clone, "config", "user.name", "Pusher").returncode == 0
    assert _git(clone, "config", "user.email", "push@example.com").returncode == 0
    (clone / filename).write_text("new content\n")
    assert _git(clone, "add", filename).returncode == 0
    assert _git(clone, "commit", "-m", f"add {filename}").returncode == 0
    assert _git(clone, "push", "origin", "main").returncode == 0
    proc = _git(clone, "rev-parse", "HEAD")
    return proc.stdout.strip()


def _write_workspace_spec(workspace_root: Path, repo_name: str, repo_url: str) -> None:
    spec_path = workspace_root / ".grip" / "workspace_spec.toml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        textwrap.dedent(
            f"""
            workspace_name = "{workspace_root.name}"

            [[repos]]
            name = "{repo_name}"
            path = "repos/{repo_name}"
            url = "{repo_url}"

            [[units]]
            name = "apollo"
            path = "agents/apollo/home"
            repos = ["{repo_name}"]
            """
        ).strip()
        + "\n"
    )


def _read_outbox(workspace_root: Path) -> list[dict[str, object]]:
    outbox = workspace_root / ".grip" / "events" / "outbox.jsonl"
    rows: list[dict[str, object]] = []
    if not outbox.exists():
        return rows
    for line in outbox.read_text().splitlines():
        if not line.strip():
            continue
        rows.append(json.loads(line))
    return rows


# ---------------------------------------------------------------------------
# 1. gitops.fetch_repo primitive
# ---------------------------------------------------------------------------

class TestFetchRepoPrimitive:

    def test_fetch_repo_exists_and_callable(self, tmp_path: Path):
        """fetch_repo must be importable from gitops."""
        from gr2.python_cli.gitops import fetch_repo
        assert callable(fetch_repo)

    def test_fetch_repo_updates_remote_tracking_refs(self, tmp_path: Path):
        """After a new commit on origin, fetch_repo must update origin/main."""
        from gr2.python_cli.gitops import fetch_repo

        remote, url = _init_bare_remote(tmp_path, "app")
        checkout = tmp_path / "checkout"
        assert subprocess.run(
            ["git", "clone", str(remote), str(checkout)],
            capture_output=True, text=True, check=False,
        ).returncode == 0

        old_ref = _git(checkout, "rev-parse", "origin/main").stdout.strip()
        new_sha = _push_new_commit(remote, "app")
        fetch_repo(checkout)
        new_ref = _git(checkout, "rev-parse", "origin/main").stdout.strip()

        assert old_ref != new_ref
        assert new_ref == new_sha

    def test_fetch_repo_does_not_change_working_tree(self, tmp_path: Path):
        """fetch_repo must only update refs, not modify the working tree."""
        from gr2.python_cli.gitops import fetch_repo

        remote, url = _init_bare_remote(tmp_path, "app")
        checkout = tmp_path / "checkout"
        assert subprocess.run(
            ["git", "clone", str(remote), str(checkout)],
            capture_output=True, text=True, check=False,
        ).returncode == 0

        head_before = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        _push_new_commit(remote, "app")
        fetch_repo(checkout)
        head_after = _git(checkout, "rev-parse", "HEAD").stdout.strip()

        assert head_before == head_after, "fetch must not change HEAD"

    def test_fetch_repo_raises_on_invalid_repo(self, tmp_path: Path):
        """fetch_repo on a non-repo path must raise SystemExit."""
        from gr2.python_cli.gitops import fetch_repo

        not_a_repo = tmp_path / "empty"
        not_a_repo.mkdir()
        with pytest.raises(SystemExit):
            fetch_repo(not_a_repo)

    def test_fetch_repo_default_remote_is_origin(self, tmp_path: Path):
        """fetch_repo with no remote arg should fetch from origin."""
        from gr2.python_cli.gitops import fetch_repo
        import inspect
        sig = inspect.signature(fetch_repo)
        params = sig.parameters
        assert "remote" in params
        assert params["remote"].default == "origin"


# ---------------------------------------------------------------------------
# 2. build_sync_plan generates fetch_shared_repo
# ---------------------------------------------------------------------------

class TestSyncPlanFetch:

    def test_plan_includes_fetch_for_existing_clean_repo(self, tmp_path: Path):
        """An existing, clean shared repo must get a fetch_shared_repo operation."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        plan = build_sync_plan(workspace_root)
        op_kinds = [op.kind for op in plan.operations]
        assert "fetch_shared_repo" in op_kinds

    def test_fetch_op_targets_correct_repo_root(self, tmp_path: Path):
        """fetch_shared_repo operation must target the actual repo checkout path."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        plan = build_sync_plan(workspace_root)
        fetch_ops = [op for op in plan.operations if op.kind == "fetch_shared_repo"]
        assert len(fetch_ops) == 1
        assert fetch_ops[0].target_path == str(workspace_root / "repos" / "app")
        assert fetch_ops[0].subject == "app"
        assert fetch_ops[0].scope == "shared_repo"

    def test_no_fetch_for_missing_repo(self, tmp_path: Path):
        """A repo that doesn't exist yet should get clone, not fetch."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)

        plan = build_sync_plan(workspace_root)
        op_kinds = [op.kind for op in plan.operations]
        assert "fetch_shared_repo" not in op_kinds
        assert "clone_shared_repo" in op_kinds

    def test_fetch_ordered_after_dirty_handling(self, tmp_path: Path):
        """fetch_shared_repo must come after stash/discard dirty handling."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        repo_root = workspace_root / "repos" / "app"
        (repo_root / "dirty.txt").write_text("uncommitted\n")

        plan = build_sync_plan(workspace_root, dirty_mode="stash")
        op_kinds = [op.kind for op in plan.operations]
        stash_idx = op_kinds.index("stash_dirty_repo")
        fetch_idx = op_kinds.index("fetch_shared_repo")
        assert stash_idx < fetch_idx, "dirty handling must precede fetch"


# ---------------------------------------------------------------------------
# 3. _execute_operation handles fetch_shared_repo
# ---------------------------------------------------------------------------

class TestExecuteFetch:

    def test_sync_run_executes_fetch_on_existing_repo(self, tmp_path: Path):
        """run_sync on an already-synced workspace must execute the fetch."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        new_sha = _push_new_commit(remote, "app")
        result = run_sync(workspace_root)
        assert result.status == "success"
        assert any("fetch" in msg.lower() for msg in result.applied)

    def test_fetch_updates_remote_tracking_in_checkout(self, tmp_path: Path):
        """After sync, origin/main in the checkout must reflect the remote push."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        repo_root = workspace_root / "repos" / "app"
        old_ref = _git(repo_root, "rev-parse", "origin/main").stdout.strip()
        new_sha = _push_new_commit(remote, "app")

        run_sync(workspace_root)
        new_ref = _git(repo_root, "rev-parse", "origin/main").stdout.strip()
        assert new_ref == new_sha
        assert new_ref != old_ref


# ---------------------------------------------------------------------------
# 4. sync.repo_fetched event
# ---------------------------------------------------------------------------

class TestFetchEvent:

    def test_fetch_emits_repo_fetched_event(self, tmp_path: Path):
        """run_sync must emit sync.repo_fetched when fetching an existing repo."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)
        before_count = len(_read_outbox(workspace_root))

        _push_new_commit(remote, "app")
        run_sync(workspace_root)

        outbox = _read_outbox(workspace_root)[before_count:]
        fetched_events = [e for e in outbox if e["type"] == "sync.repo_fetched"]
        assert len(fetched_events) == 1

    def test_fetched_event_payload(self, tmp_path: Path):
        """sync.repo_fetched event must include repo name and tracking ref info."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)
        before_count = len(_read_outbox(workspace_root))

        _push_new_commit(remote, "app")
        run_sync(workspace_root)

        outbox = _read_outbox(workspace_root)[before_count:]
        fetched = next(e for e in outbox if e["type"] == "sync.repo_fetched")
        assert fetched["repo"] == "app"
        assert "old_ref" in fetched
        assert "new_ref" in fetched

    def test_fetched_event_type_in_enum(self):
        """SYNC_REPO_FETCHED must exist in EventType enum."""
        from gr2.python_cli.events import EventType
        assert hasattr(EventType, "SYNC_REPO_FETCHED")
        assert EventType.SYNC_REPO_FETCHED.value == "sync.repo_fetched"


# ---------------------------------------------------------------------------
# 5. End-to-end: full sync cycle with fetch
# ---------------------------------------------------------------------------

class TestSyncFetchEndToEnd:

    def test_second_sync_fetches_new_commits(self, tmp_path: Path):
        """Full cycle: clone, push to remote, re-sync, verify fetch happened."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)

        first = run_sync(workspace_root)
        assert first.status == "success"

        repo_root = workspace_root / "repos" / "app"
        old_origin_main = _git(repo_root, "rev-parse", "origin/main").stdout.strip()

        new_sha = _push_new_commit(remote, "app")
        second = run_sync(workspace_root)
        assert second.status == "success"

        new_origin_main = _git(repo_root, "rev-parse", "origin/main").stdout.strip()
        assert new_origin_main == new_sha
        assert new_origin_main != old_origin_main

    def test_sync_fetch_does_not_auto_merge(self, tmp_path: Path):
        """Sync fetches but does NOT auto-merge into the current branch."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        repo_root = workspace_root / "repos" / "app"
        head_before = current_head_sha(repo_root)

        _push_new_commit(remote, "app")
        run_sync(workspace_root)

        head_after = current_head_sha(repo_root)
        assert head_before == head_after, "sync must not auto-merge; only fetch"

    def test_sync_fetch_with_no_new_commits(self, tmp_path: Path):
        """Re-sync with no new remote commits should still succeed."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        second = run_sync(workspace_root)
        assert second.status == "success"

    def test_sync_event_sequence_on_resync(self, tmp_path: Path):
        """Re-sync event sequence must include cache_refreshed and repo_fetched."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)
        before_count = len(_read_outbox(workspace_root))

        _push_new_commit(remote, "app")
        run_sync(workspace_root)

        outbox = _read_outbox(workspace_root)[before_count:]
        types = [str(e["type"]) for e in outbox]
        assert "sync.started" in types
        assert "sync.cache_refreshed" in types
        assert "sync.repo_fetched" in types
        assert "sync.completed" in types
