"""TDD specs for step 4 of gr2's sync-run fix: `sync status` (not `sync run`) attempts one
`git ls-remote` per DISTINCT remote so a refused remote surfaces as a
non-blocking ISSUE in status, rather than as a mid-run surprise -- the
coordinator's own measured shape: `sync status` reported "ready, issue_count = 0" seconds
before `sync run` failed 100% on its first operation with nothing seeded.

`sync run`'s own plan-building calls stay probe-free by default (`run_sync`
already dials the network for real via seed/fetch; a second ls-remote per repo
ahead of that would just double the network cost for no new information).

Tests cover:
1. gitops.probe_remote primitive: reachable / unreachable, no I/O beyond git
2. probe_remote applies the SSH->HTTPS rewrite before dialing (step 2 of this fix)
3. build_sync_plan(probe_remotes=False) (the default, and what `sync run` uses
   internally) never touches the network for this
4. build_sync_plan(probe_remotes=True): one ls-remote per DISTINCT remote, a
   refusal becomes a non-blocking SyncIssue, plan status stays out of "blocked"
5. sync_status_payload always probes (it is the "status" surface)
"""
from __future__ import annotations

import subprocess
import textwrap
from pathlib import Path

import pytest

from gr2.python_cli.gitops import probe_remote
from gr2.python_cli.syncops import build_sync_plan, sync_status_payload


UNREACHABLE_URL = "https://127.0.0.1:1/does-not-exist/repo.git"


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


def _write_workspace_spec(workspace_root: Path, repos: list[tuple[str, str, str]]) -> None:
    """repos: list of (name, path, url)."""
    spec_path = workspace_root / ".grip" / "workspace_spec.toml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    blocks = []
    all_names = [name for name, _, _ in repos]
    for name, path, url in repos:
        blocks.append(
            textwrap.dedent(
                f"""
                [[repos]]
                name = "{name}"
                path = "{path}"
                url = "{url}"
                """
            ).strip()
        )
    repo_list = ", ".join(f'"{n}"' for n in all_names)
    body = "\n\n".join(blocks) + textwrap.dedent(
        f"""

        [[units]]
        name = "apollo"
        path = "agents/apollo/home"
        repos = [{repo_list}]
        """
    )
    spec_path.write_text(f'workspace_name = "{workspace_root.name}"\n\n' + body.strip() + "\n")


# ---------------------------------------------------------------------------
# 1 & 2. probe_remote primitive
# ---------------------------------------------------------------------------

class TestProbeRemote:

    def test_reachable_remote_returns_true(self, tmp_path: Path):
        _, url = _init_bare_remote(tmp_path, "app")
        reachable, detail = probe_remote(url)
        assert reachable is True
        assert detail == ""

    def test_unreachable_remote_returns_false_with_detail(self):
        reachable, detail = probe_remote(UNREACHABLE_URL)
        assert reachable is False
        assert detail != ""

    def test_probe_applies_ssh_rewrite_before_dialing(self, monkeypatch: pytest.MonkeyPatch):
        """Command-capture proof, no real network: probing an SSH github url
        must ls-remote the REWRITTEN https url, never the raw SSH one."""
        from gr2.python_cli import gitops as gitops_mod

        recorded: list[list[str]] = []

        def fake_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

        monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
        gitops_mod.probe_remote("git@github.com:synapt-dev/app.git")

        ls_remote_cmds = [c for c in recorded if c[:2] == ["git", "ls-remote"]]
        assert len(ls_remote_cmds) == 1
        assert ls_remote_cmds[0][-1] == "https://github.com/synapt-dev/app.git"


# ---------------------------------------------------------------------------
# 3. build_sync_plan default (and run_sync's internal usage): probe-free
# ---------------------------------------------------------------------------

class TestBuildSyncPlanDefaultNeverProbes:

    def test_default_call_issues_zero_ls_remote(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from gr2.python_cli import syncops as syncops_mod

        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        from gr2.python_cli import gitops as gitops_mod

        _, url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, [("app", "repos/app", url)])

        recorded: list[list[str]] = []
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            recorded.append(list(cmd))
            return real_run(cmd, *args, **kwargs)

        # probe_remote lives in gitops.py; that is the module whose subprocess
        # binding actually executes the call, regardless of which module's
        # namespace re-exports the function.
        monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
        build_sync_plan(workspace_root)  # probe_remotes defaults False

        ls_remote_cmds = [c for c in recorded if c[:2] == ["git", "ls-remote"]]
        assert ls_remote_cmds == []


# ---------------------------------------------------------------------------
# 4. build_sync_plan(probe_remotes=True): one ls-remote per DISTINCT remote
# ---------------------------------------------------------------------------

class TestBuildSyncPlanProbesOnRequest:

    def test_probes_once_per_distinct_remote(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
        from gr2.python_cli import gitops as gitops_mod

        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _, url_a = _init_bare_remote(tmp_path, "a")
        _, url_b = _init_bare_remote(tmp_path, "b")
        # a2 shares a's URL -- "distinct remote" means distinct URL, not distinct repo.
        _write_workspace_spec(
            workspace_root,
            [("a", "repos/a", url_a), ("a2", "repos/a2", url_a), ("b", "repos/b", url_b)],
        )

        recorded: list[str] = []
        real_run = subprocess.run

        def fake_run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "ls-remote"]:
                recorded.append(cmd[-1])
            return real_run(cmd, *args, **kwargs)

        monkeypatch.setattr(gitops_mod.subprocess, "run", fake_run)
        build_sync_plan(workspace_root, probe_remotes=True)

        assert sorted(recorded) == sorted([url_a, url_b]), "one probe per DISTINCT url, not per repo"

    def test_refused_remote_is_a_non_blocking_issue(self, tmp_path: Path):
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _write_workspace_spec(workspace_root, [("app", "repos/app", UNREACHABLE_URL)])

        plan = build_sync_plan(workspace_root, probe_remotes=True)

        remote_issues = [i for i in plan.issues if i.code == "remote_unreachable"]
        assert len(remote_issues) == 1
        assert remote_issues[0].subject == "app"
        assert remote_issues[0].blocks is False
        assert plan.status != "blocked", "a refused remote must not block the whole plan"

    def test_reachable_remote_produces_no_issue(self, tmp_path: Path):
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _, url = _init_bare_remote(tmp_path, "app")
        _write_workspace_spec(workspace_root, [("app", "repos/app", url)])

        plan = build_sync_plan(workspace_root, probe_remotes=True)

        assert [i for i in plan.issues if i.code == "remote_unreachable"] == []


# ---------------------------------------------------------------------------
# 5. sync_status_payload always probes
# ---------------------------------------------------------------------------

class TestSyncStatusPayloadAlwaysProbes:

    def test_status_payload_surfaces_refused_remote(self, tmp_path: Path):
        workspace_root = tmp_path / "workspace"
        workspace_root.mkdir()
        _write_workspace_spec(workspace_root, [("app", "repos/app", UNREACHABLE_URL)])

        payload = sync_status_payload(workspace_root)

        codes = [issue["code"] for issue in payload["issues"]]
        assert "remote_unreachable" in codes
