"""TDD specs for step 3 of gr2's sync-run fix: a CLEAN checkout on its tracked branch must
fast-forward after fetch_shared_repo, not merely fetch and stop. A checkout
carrying local commits not on origin (diverged) is reported with ahead/behind
counts and left untouched, never force-moved.

Measured cause (RAN on the coordinator's desk): `fetch_shared_repo`
only ever calls `git fetch`, which updates the `origin/<branch>` tracking ref
but never the checkout's own branch -- so a desk that reads "sync ready" and
starts work is on stale code with a green sync behind it (grip's coordinator
desk read gitgrip `main` 18 behind after a "successful" sync).

This deliberately supersedes `test_sync_fetch_does_not_auto_merge` in
test_sync_fetch.py, which asserted the OLD (defective) contract as a design
decision ("sync must not auto-merge; only fetch"). That test is updated in
the same change as this fix, disclosed rather than silently broken -- fixing
that stale-contract test IS this fix's actual target, not a side effect.

Tests cover:
1. Clean, behind-only checkout: HEAD fast-forwards to origin/<branch>
2. The exact witness the fix was ordered against: a checkout 18 behind reads
   0 behind after sync run
3. A checkout with a local (unpushed) commit -- ahead>0, genuinely diverged --
   is left completely untouched, and ahead/behind are reported
4. An up-to-date checkout (ahead=0, behind=0) is a no-op, no failure
5. A dirty checkout that also has remote commits: stash (existing behavior)
   then still fast-forwards once clean
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from gr2.python_cli.gitops import current_head_sha
from gr2.python_cli.syncops import build_sync_plan, run_sync


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


def _push_commits(remote: Path, name: str, count: int, prefix: str = "new") -> str:
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
    for i in range(count):
        (clone / f"{prefix}-{i}.txt").write_text(f"content {i}\n")
        assert _git(clone, "add", f"{prefix}-{i}.txt").returncode == 0
        assert _git(clone, "commit", "-m", f"add {prefix}-{i}").returncode == 0
    assert _git(clone, "push", "origin", "main").returncode == 0
    return _git(clone, "rev-parse", "HEAD").stdout.strip()


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


def _ahead_behind(repo_root: Path, branch: str = "main") -> tuple[int, int]:
    proc = _git(repo_root, "rev-list", "--left-right", "--count", f"HEAD...origin/{branch}")
    assert proc.returncode == 0
    ahead_s, behind_s = proc.stdout.strip().split()
    return int(ahead_s), int(behind_s)


class TestFastForwardCleanBehindCheckout:

    def test_clean_checkout_fast_forwards_to_tracking_ref(self, tmp_path: Path):
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        repo_root = workspace_root / "repos" / "app"
        new_sha = _push_commits(remote, "app", 3)

        result = run_sync(workspace_root)
        assert result.status == "success"

        head_after = current_head_sha(repo_root)
        assert head_after == new_sha, "clean, behind-only checkout must fast-forward"

    def test_18_behind_reads_0_behind_after_sync(self, tmp_path: Path):
        """The exact witness the fix was ordered against."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        repo_root = workspace_root / "repos" / "app"
        _push_commits(remote, "app", 18)

        # origin/main only reflects the push once SOMETHING fetches; measure
        # "before" by fetching without merging (mirrors what the old,
        # defective fetch_shared_repo alone used to leave the checkout at).
        assert _git(repo_root, "fetch", "origin").returncode == 0
        ahead_before, behind_before = _ahead_behind(repo_root)
        assert behind_before == 18

        run_sync(workspace_root)

        ahead_after, behind_after = _ahead_behind(repo_root)
        assert behind_after == 0
        assert ahead_after == 0

    def test_up_to_date_checkout_is_a_no_op(self, tmp_path: Path):
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        repo_root = workspace_root / "repos" / "app"
        head_before = current_head_sha(repo_root)

        result = run_sync(workspace_root)
        assert result.status == "success"
        assert current_head_sha(repo_root) == head_before


class TestDivergedCheckoutLeftUntouched:

    def test_local_only_commit_is_never_moved(self, tmp_path: Path):
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        repo_root = workspace_root / "repos" / "app"
        (repo_root / "local-only.txt").write_text("mine\n")
        assert _git(repo_root, "add", "local-only.txt").returncode == 0
        assert _git(repo_root, "config", "user.name", "Local").returncode == 0
        assert _git(repo_root, "config", "user.email", "local@example.com").returncode == 0
        assert _git(repo_root, "commit", "-m", "local work").returncode == 0
        local_head = current_head_sha(repo_root)

        _push_commits(remote, "app", 2)

        result = run_sync(workspace_root)
        assert result.status == "success"

        assert current_head_sha(repo_root) == local_head, "a diverged checkout must never be force-moved"

    def test_diverged_checkout_reports_ahead_and_behind(self, tmp_path: Path):
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        repo_root = workspace_root / "repos" / "app"
        (repo_root / "local-only.txt").write_text("mine\n")
        assert _git(repo_root, "add", "local-only.txt").returncode == 0
        assert _git(repo_root, "config", "user.name", "Local").returncode == 0
        assert _git(repo_root, "config", "user.email", "local@example.com").returncode == 0
        assert _git(repo_root, "commit", "-m", "local work").returncode == 0

        _push_commits(remote, "app", 2)

        result = run_sync(workspace_root)
        applied_text = " ".join(result.applied).lower()
        assert "ahead" in applied_text and "behind" in applied_text
        assert "diverged" in applied_text or "unmerged" in applied_text or "left untouched" in applied_text


class TestFastForwardAfterStash:

    def test_dirty_checkout_stashes_then_still_fast_forwards(self, tmp_path: Path):
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, repo_url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", repo_url)
        run_sync(workspace_root)

        repo_root = workspace_root / "repos" / "app"
        (repo_root / "dirty.txt").write_text("uncommitted\n")

        new_sha = _push_commits(remote, "app", 2)

        result = run_sync(workspace_root, dirty_mode="stash")
        assert result.status == "success"

        head_after = current_head_sha(repo_root)
        assert head_after == new_sha, "clean-after-stash checkout must still fast-forward"
