"""TDD specs for step 1 of gr2's sync-run fix: seed_repo_cache must seed from a LOCAL shared
checkout when one already exists, and only reach the network when there is no
local checkout to seed from.

Measured cause (RAN on the coordinator's desk): `gr2 sync run` calls
`ensure_repo_cache(url, cache_repo_root)`, which always `git clone --mirror`s from
the network URL, even when the same repo is already fully materialized next to it
as a shared checkout. On a desk whose workspace spec carries `git@github.com:` URLs
(11 of 27, measured) this fails on SSH before anything is seeded, and the first
failure stops all 55 planned operations with nothing seeded (empty
`.grip/cache/repos`).

Tests cover:
1. gitops.ensure_repo_cache primitive: seeds from a local checkout, offline
2. gitops.ensure_repo_cache: falls back to the network when no local checkout exists
3. gitops.ensure_repo_cache: the seeded cache's origin remote is the SPEC url, not
   the local checkout path (the mirror must still track the real remote)
4. syncops._execute_operation / run_sync: seed_repo_cache offline end-to-end when
   the shared checkout already exists, with an unreachable spec URL standing in
   for a desk with no network / an SSH URL the agent cannot use
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from gr2.python_cli.gitops import ensure_repo_cache, is_git_dir
from gr2.python_cli.spec_apply import repo_cache_path
from gr2.python_cli.syncops import build_sync_plan, run_sync


UNREACHABLE_URL = "https://127.0.0.1:1/does-not-exist/repo.git"


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_local_checkout(path: Path, name: str) -> Path:
    """A real, ordinary (non-bare) git checkout -- stands in for an already
    materialized shared repo checkout on the desk."""
    path.mkdir(parents=True, exist_ok=True)
    assert _git(path, "init", "-b", "main").returncode == 0
    assert _git(path, "config", "user.name", "Test").returncode == 0
    assert _git(path, "config", "user.email", "test@example.com").returncode == 0
    (path / "README.md").write_text(f"# {name}\n")
    assert _git(path, "add", "README.md").returncode == 0
    assert _git(path, "commit", "-m", "initial").returncode == 0
    return path


def _init_bare_remote(tmp_path: Path, name: str) -> tuple[Path, str]:
    source = tmp_path / f"{name}-src"
    _init_local_checkout(source, name)
    remote = tmp_path / f"{name}.git"
    assert subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        capture_output=True, text=True, check=False,
    ).returncode == 0
    return remote, remote.as_uri()


def _write_workspace_spec(workspace_root: Path, repo_name: str, repo_path: str, repo_url: str) -> None:
    spec_path = workspace_root / ".grip" / "workspace_spec.toml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(
        textwrap.dedent(
            f"""
            workspace_name = "{workspace_root.name}"

            [[repos]]
            name = "{repo_name}"
            path = "{repo_path}"
            url = "{repo_url}"

            [[units]]
            name = "apollo"
            path = "agents/apollo/home"
            repos = ["{repo_name}"]
            """
        ).strip()
        + "\n"
    )


# ---------------------------------------------------------------------------
# 1. gitops.ensure_repo_cache: seed from local checkout, offline
# ---------------------------------------------------------------------------

class TestEnsureRepoCacheLocalSeed:

    def test_seeds_from_local_checkout_with_unreachable_url(self, tmp_path: Path):
        """An unreachable spec URL must not stop seeding when a local checkout exists."""
        checkout = _init_local_checkout(tmp_path / "checkout", "app")
        cache_path = tmp_path / "cache" / "app.git"

        created = ensure_repo_cache(UNREACHABLE_URL, cache_path, local_source=checkout)

        assert created is True
        assert is_git_dir(cache_path)

    def test_seeded_cache_carries_local_checkout_history(self, tmp_path: Path):
        checkout = _init_local_checkout(tmp_path / "checkout", "app")
        checkout_head = _git(checkout, "rev-parse", "HEAD").stdout.strip()
        cache_path = tmp_path / "cache" / "app.git"

        ensure_repo_cache(UNREACHABLE_URL, cache_path, local_source=checkout)

        proc = subprocess.run(
            ["git", "--git-dir", str(cache_path), "rev-parse", "main"],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == checkout_head


# ---------------------------------------------------------------------------
# 2. gitops.ensure_repo_cache: falls back to network with no local checkout
# ---------------------------------------------------------------------------

class TestEnsureRepoCacheNetworkFallback:

    def test_no_local_source_uses_network_url(self, tmp_path: Path):
        remote, url = _init_bare_remote(tmp_path, "app")
        cache_path = tmp_path / "cache" / "app.git"

        created = ensure_repo_cache(url, cache_path, local_source=None)

        assert created is True
        assert is_git_dir(cache_path)

    def test_local_source_path_that_is_not_a_git_repo_falls_back_to_network(self, tmp_path: Path):
        """A local_source pointing at a non-repo (or nothing) must not be trusted as a seed."""
        remote, url = _init_bare_remote(tmp_path, "app")
        not_a_repo = tmp_path / "not-a-checkout"
        not_a_repo.mkdir()
        cache_path = tmp_path / "cache" / "app.git"

        created = ensure_repo_cache(url, cache_path, local_source=not_a_repo)

        assert created is True
        assert is_git_dir(cache_path)

    def test_missing_local_source_path_falls_back_to_network(self, tmp_path: Path):
        remote, url = _init_bare_remote(tmp_path, "app")
        cache_path = tmp_path / "cache" / "app.git"

        created = ensure_repo_cache(url, cache_path, local_source=tmp_path / "never-existed")

        assert created is True
        assert is_git_dir(cache_path)


# ---------------------------------------------------------------------------
# 3. Seeded-from-local cache's origin remote is the SPEC url
# ---------------------------------------------------------------------------

class TestEnsureRepoCacheOriginRepoint:

    def test_origin_remote_is_spec_url_not_local_path(self, tmp_path: Path):
        checkout = _init_local_checkout(tmp_path / "checkout", "app")
        cache_path = tmp_path / "cache" / "app.git"
        spec_url = "https://github.com/synapt-dev/app.git"

        ensure_repo_cache(spec_url, cache_path, local_source=checkout)

        proc = subprocess.run(
            ["git", "--git-dir", str(cache_path), "remote", "get-url", "origin"],
            capture_output=True, text=True, check=False,
        )
        assert proc.returncode == 0
        assert proc.stdout.strip() == spec_url
        assert proc.stdout.strip() != str(checkout)

    def test_local_checkout_own_remote_is_untouched(self, tmp_path: Path):
        """Seeding the cache must never rewrite the CHECKOUT's own remote."""
        checkout = _init_local_checkout(tmp_path / "checkout", "app")
        assert _git(checkout, "remote", "add", "origin", "git@github.com:synapt-dev/app.git").returncode == 0
        cache_path = tmp_path / "cache" / "app.git"

        ensure_repo_cache("https://github.com/synapt-dev/app.git", cache_path, local_source=checkout)

        proc = _git(checkout, "remote", "get-url", "origin")
        assert proc.stdout.strip() == "git@github.com:synapt-dev/app.git"


# ---------------------------------------------------------------------------
# 4. End-to-end through the sync planner/executor: offline, existing checkout
# ---------------------------------------------------------------------------

class TestSyncRunSeedsOfflineWhenCheckoutExists:

    def test_run_sync_seeds_cache_offline_when_checkout_already_materialized(self, tmp_path: Path):
        """The exact shape this fix targets: a shared checkout already exists, the spec
        URL is unreachable (stands in for an SSH URL this desk cannot dial), and
        sync run must still seed the cache -- from the checkout, not the network.

        The checkout carries its OWN working origin remote (a real bare remote)
        so the unrelated fetch_shared_repo step -- step 3's lane, not this
        one -- can still succeed against the checkout's real remote; only the
        SPEC's recorded url is unreachable, isolating what this fix covers."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        checkout = _init_local_checkout(workspace_root / "repos" / "app", "app")
        checkout_remote, checkout_remote_url = _init_bare_remote(tmp_path, "app-origin")
        assert _git(checkout, "remote", "add", "origin", checkout_remote_url).returncode == 0
        assert _git(checkout, "fetch", "origin").returncode == 0
        _write_workspace_spec(workspace_root, "app", "repos/app", UNREACHABLE_URL)

        plan = build_sync_plan(workspace_root)
        op_kinds = [op.kind for op in plan.operations]
        assert "seed_repo_cache" in op_kinds

        result = run_sync(workspace_root)

        assert result.status == "success"
        cache_path = repo_cache_path(workspace_root, "app")
        assert is_git_dir(cache_path)

    def test_run_sync_still_uses_network_when_no_checkout_exists(self, tmp_path: Path):
        """Regression guard: a repo with NO local checkout still seeds from the
        network URL (first-clone-ever case, e.g. a brand new desk)."""
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        remote, url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, "app", "repos/app", url)

        result = run_sync(workspace_root)

        assert result.status == "success"
        cache_path = repo_cache_path(workspace_root, "app")
        assert is_git_dir(cache_path)
