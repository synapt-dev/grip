"""TDD specs for step 2 of gr2's sync-run fix: an SSH GitHub spec URL is rewritten to its
HTTPS equivalent for the mirror seed/refresh and for fetch_shared_repo, on by
default, without ever touching a CHECKOUT's own configured remote.

Measured cause (RAN on the coordinator's desk): the host's SSH is
1Password-managed and the workspace rule (claude.md, SSH Policy) is never to
touch it and to fall back to HTTPS silently; the agent's SSH agent refuses to
sign, so every `git@github.com:` URL fails for network operations gr2 performs
on the agent's behalf. `gr2 sync run` has no HTTPS fallback and no URL rewrite.

Tests cover:
1. rewrite_ssh_github_url primitive (pure, no I/O)
2. the env-flag gate, default ON
3. ensure_repo_cache network-fallback clone uses the rewritten URL (git command
   argument asserted directly, no real network reached)
4. fetch_repo routes an SSH-origin checkout's fetch through the rewritten URL
   with an explicit refspec, and leaves the checkout's own `origin` remote
   config completely untouched
5. fetch_repo is unchanged (plain `fetch origin`) for an already-HTTPS origin
"""
from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from gr2.python_cli.gitops import (
    fetch_repo,
    git,
    rewrite_ssh_github_url,
)


def _git(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )


def _init_local_checkout(path: Path, name: str) -> Path:
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


# ---------------------------------------------------------------------------
# 1. rewrite_ssh_github_url: pure function
# ---------------------------------------------------------------------------

class TestRewriteSshGithubUrl:

    def test_rewrites_ssh_github_url_with_git_suffix(self):
        assert rewrite_ssh_github_url("git@github.com:synapt-dev/.github.git") == \
            "https://github.com/synapt-dev/.github.git"

    def test_rewrites_ssh_github_url_without_git_suffix(self):
        assert rewrite_ssh_github_url("git@github.com:synapt-dev/grip") == \
            "https://github.com/synapt-dev/grip.git"

    def test_leaves_https_url_unchanged(self):
        url = "https://github.com/synapt-dev/grip.git"
        assert rewrite_ssh_github_url(url) == url

    def test_leaves_non_github_ssh_url_unchanged(self):
        url = "git@gitlab.com:org/repo.git"
        assert rewrite_ssh_github_url(url) == url

    def test_leaves_non_git_url_unchanged(self):
        url = "https://huggingface.co/datasets/synapt/results"
        assert rewrite_ssh_github_url(url) == url


# ---------------------------------------------------------------------------
# 2. env-flag gate, default ON
# ---------------------------------------------------------------------------

class TestRewriteEnvFlag:

    def test_rewrite_applied_by_default(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.delenv("GR2_SYNC_REWRITE_SSH_URLS", raising=False)
        from gr2.python_cli.gitops import _effective_remote_url
        assert _effective_remote_url("git@github.com:synapt-dev/grip.git") == \
            "https://github.com/synapt-dev/grip.git"

    def test_rewrite_disabled_via_env_zero(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GR2_SYNC_REWRITE_SSH_URLS", "0")
        from gr2.python_cli.gitops import _effective_remote_url
        assert _effective_remote_url("git@github.com:synapt-dev/grip.git") == \
            "git@github.com:synapt-dev/grip.git"

    def test_rewrite_disabled_via_env_false(self, monkeypatch: pytest.MonkeyPatch):
        monkeypatch.setenv("GR2_SYNC_REWRITE_SSH_URLS", "false")
        from gr2.python_cli.gitops import _effective_remote_url
        assert _effective_remote_url("git@github.com:synapt-dev/grip.git") == \
            "git@github.com:synapt-dev/grip.git"


# ---------------------------------------------------------------------------
# 3. ensure_repo_cache network-fallback clone uses the rewritten URL
# ---------------------------------------------------------------------------

class TestEnsureRepoCacheUsesRewrittenUrl:

    def test_network_clone_receives_rewritten_url(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """No local_source: the SSH spec url must never reach the actual `git
        clone` argv; the rewritten https url must, proven from the recorded
        command rather than from a real network attempt."""
        from gr2.python_cli import gitops as gitops_mod

        recorded: list[list[str]] = []
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            if cmd[:2] == ["git", "clone"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)

        cache_path = tmp_path / "cache" / "app.git"
        gitops_mod.ensure_repo_cache("git@github.com:synapt-dev/app.git", cache_path, local_source=None)

        clone_cmds = [c for c in recorded if c[:2] == ["git", "clone"]]
        assert len(clone_cmds) == 1
        assert "https://github.com/synapt-dev/app.git" in clone_cmds[0]
        assert "git@github.com:synapt-dev/app.git" not in clone_cmds[0]

    def test_network_clone_respects_disabled_rewrite(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from gr2.python_cli import gitops as gitops_mod

        monkeypatch.setenv("GR2_SYNC_REWRITE_SSH_URLS", "0")
        recorded: list[list[str]] = []
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            if cmd[:2] == ["git", "clone"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)

        cache_path = tmp_path / "cache" / "app.git"
        gitops_mod.ensure_repo_cache("git@github.com:synapt-dev/app.git", cache_path, local_source=None)

        clone_cmds = [c for c in recorded if c[:2] == ["git", "clone"]]
        assert "git@github.com:synapt-dev/app.git" in clone_cmds[0]


# ---------------------------------------------------------------------------
# 3b. ensure_repo_cache REFRESH path self-heals a cache seeded before this fix
#     (or by a human with working SSH) whose origin is still the raw SSH url
# ---------------------------------------------------------------------------

class TestEnsureRepoCacheRefreshRepointsSshOrigin:

    def test_refresh_repoints_ssh_origin_before_updating(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """An EXISTING cache whose origin is still `git@github.com:...` (pre-fix
        state, or a human's working SSH) must have its origin repointed to the
        rewritten https url before `remote update --prune` runs -- otherwise
        every refresh after the very first seed keeps failing the same way."""
        from gr2.python_cli import gitops as gitops_mod

        cache_path = tmp_path / "cache" / "app.git"
        cache_path.mkdir(parents=True)
        assert subprocess.run(
            ["git", "init", "--bare", str(cache_path)],
            capture_output=True, text=True, check=False,
        ).returncode == 0
        assert subprocess.run(
            ["git", "--git-dir", str(cache_path), "remote", "add", "origin", "git@github.com:synapt-dev/app.git"],
            capture_output=True, text=True, check=False,
        ).returncode == 0

        recorded: list[list[str]] = []
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            if cmd[:2] == ["git", "--git-dir"] and "update" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)

        created = gitops_mod.ensure_repo_cache("git@github.com:synapt-dev/app.git", cache_path, local_source=None)
        assert created is False

        set_url_cmds = [c for c in recorded if "set-url" in c]
        assert len(set_url_cmds) == 1
        assert set_url_cmds[0][-1] == "https://github.com/synapt-dev/app.git"

        update_idx = next(i for i, c in enumerate(recorded) if "update" in c)
        set_url_idx = next(i for i, c in enumerate(recorded) if "set-url" in c)
        assert set_url_idx < update_idx, "origin must be repointed BEFORE remote update runs"

    def test_refresh_does_not_touch_an_already_https_origin(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from gr2.python_cli import gitops as gitops_mod

        cache_path = tmp_path / "cache" / "app.git"
        cache_path.mkdir(parents=True)
        assert subprocess.run(
            ["git", "init", "--bare", str(cache_path)],
            capture_output=True, text=True, check=False,
        ).returncode == 0
        assert subprocess.run(
            ["git", "--git-dir", str(cache_path), "remote", "add", "origin", "https://github.com/synapt-dev/app.git"],
            capture_output=True, text=True, check=False,
        ).returncode == 0

        recorded: list[list[str]] = []
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            if cmd[:2] == ["git", "--git-dir"] and "update" in cmd:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)

        gitops_mod.ensure_repo_cache("https://github.com/synapt-dev/app.git", cache_path, local_source=None)

        set_url_cmds = [c for c in recorded if "set-url" in c]
        assert set_url_cmds == []


# ---------------------------------------------------------------------------
# 4. fetch_repo: SSH origin routes through the rewritten URL, checkout's own
#    remote config is left completely untouched
# ---------------------------------------------------------------------------

class TestFetchRepoSshRewrite:

    def test_ssh_origin_fetch_routes_through_rewritten_url_and_full_refspec(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Command-capture proof, no real network: an SSH-origin checkout's
        fetch must issue `git fetch <rewritten https url> +refs/heads/*:refs/remotes/origin/*`,
        never a plain `git fetch origin` (which would resolve to the SSH url
        via git config and fail the way the coordinator's desk measured)."""
        from gr2.python_cli import gitops as gitops_mod

        checkout = _init_local_checkout(tmp_path / "checkout", "app")
        assert _git(checkout, "remote", "add", "origin", "git@github.com:synapt-dev/app.git").returncode == 0

        recorded: list[list[str]] = []
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            if cmd[:2] == ["git", "fetch"]:
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)

        fetch_repo(checkout)

        fetch_cmds = [c for c in recorded if c[:2] == ["git", "fetch"]]
        assert len(fetch_cmds) == 1
        assert fetch_cmds[0] == [
            "git", "fetch", "https://github.com/synapt-dev/app.git",
            "+refs/heads/*:refs/remotes/origin/*",
        ]

    def test_checkout_own_origin_remote_config_untouched_by_fetch(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        """fetch_repo must NEVER run `remote set-url` on the checkout itself."""
        from gr2.python_cli import gitops as gitops_mod

        checkout = _init_local_checkout(tmp_path / "checkout", "app")
        assert _git(checkout, "remote", "add", "origin", "git@github.com:synapt-dev/app.git").returncode == 0

        def fake_run(cmd, *args, **kwargs):
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
        try:
            fetch_repo(checkout)
        except SystemExit:
            pass
        monkeypatch.undo()  # `subprocess` is one shared module object: the patch above
        # fakes every subprocess.run call process-wide, this test's own verification
        # included, unless it is lifted before reading the checkout's real remote back.

        origin_after = _git(checkout, "remote", "get-url", "origin").stdout.strip()
        assert origin_after == "git@github.com:synapt-dev/app.git"


# ---------------------------------------------------------------------------
# 5. fetch_repo unchanged for an already-HTTPS origin
# ---------------------------------------------------------------------------

class TestFetchRepoHttpsOriginUnchanged:

    def test_https_origin_still_fetches_via_plain_origin(self, tmp_path: Path):
        remote, remote_url = _init_bare_remote(tmp_path, "app")
        checkout = _init_local_checkout(tmp_path / "checkout", "app")
        assert _git(checkout, "remote", "add", "origin", remote_url).returncode == 0

        fetch_repo(checkout)  # must not raise

        origin_ref = _git(checkout, "rev-parse", "--verify", "origin/main")
        assert origin_ref.returncode == 0
