from __future__ import annotations

import fcntl
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from types import SimpleNamespace

from typer.testing import CliRunner

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from gr2.python_cli.app import app
from gr2.python_cli import app as app_module
from gr2.python_cli.platform import CreatePRRequest, PRCheck, PRRef, PRStatus
from gr2.python_cli.syncops import run_sync
from gr2.prototypes import lane_workspace_prototype as lane_proto


runner = CliRunner()


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
    assert _git(source, "config", "user.name", "Atlas").returncode == 0
    assert _git(source, "config", "user.email", "atlas@example.com").returncode == 0
    (source / "README.md").write_text(f"# {name}\n")
    assert _git(source, "add", "README.md").returncode == 0
    assert _git(source, "commit", "-m", "initial").returncode == 0

    remote = tmp_path / f"{name}.git"
    assert subprocess.run(
        ["git", "clone", "--bare", str(source), str(remote)],
        capture_output=True,
        text=True,
        check=False,
    ).returncode == 0
    return remote, remote.as_uri()


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
            name = "atlas"
            path = "agents/atlas/home"
            repos = ["{repo_name}"]
            """
        ).strip()
        + "\n"
    )


def _write_workspace_spec_multi(workspace_root: Path, repos: list[tuple[str, str]]) -> None:
    spec_path = workspace_root / ".grip" / "workspace_spec.toml"
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    repo_blocks = []
    for repo_name, repo_url in repos:
        repo_blocks.append(
            textwrap.dedent(
                f"""
                [[repos]]
                name = "{repo_name}"
                path = "repos/{repo_name}"
                url = "{repo_url}"
                """
            ).strip()
        )
    spec_path.write_text(
        textwrap.dedent(
            f"""
            workspace_name = "{workspace_root.name}"

            {'\n\n'.join(repo_blocks)}

            [[units]]
            name = "atlas"
            path = "agents/atlas/home"
            repos = [{", ".join(f'"{name}"' for name, _ in repos)}]
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


def _stash_list(repo_root: Path) -> list[str]:
    proc = _git(repo_root, "stash", "list")
    assert proc.returncode == 0
    return [line for line in proc.stdout.splitlines() if line.strip()]


def test_sync_run_emits_contract_payloads_and_cache_events(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, repo_url = _init_bare_remote(tmp_path, "app")
    _write_workspace_spec(workspace_root, "app", repo_url)

    result = run_sync(workspace_root)
    assert result.status == "success"

    outbox = _read_outbox(workspace_root)
    event_types = [str(row["type"]) for row in outbox]
    assert "sync.started" in event_types
    assert "sync.repo_updated" in event_types
    assert "sync.completed" in event_types
    assert "sync.cache_seeded" in event_types

    started = next(row for row in outbox if row["type"] == "sync.started")
    assert started["repos"] == ["app"]
    assert isinstance(started["strategy"], str)

    updated = next(row for row in outbox if row["type"] == "sync.repo_updated")
    assert updated["repo"] == "app"
    assert isinstance(updated["commits_pulled"], int)
    assert updated["commits_pulled"] >= 0

    completed = next(row for row in outbox if row["type"] == "sync.completed")
    assert completed["status"] == "success"
    assert completed["repos_updated"] == 1
    assert completed["repos_skipped"] == 0
    assert completed["repos_failed"] == 0
    assert isinstance(completed["duration_ms"], int)
    assert completed["duration_ms"] >= 0


def test_sync_run_emits_cache_refresh_event_when_cache_exists(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, repo_url = _init_bare_remote(tmp_path, "app")
    _write_workspace_spec(workspace_root, "app", repo_url)

    first = run_sync(workspace_root)
    assert first.status == "success"
    before_count = len(_read_outbox(workspace_root))

    second = run_sync(workspace_root)
    assert second.status == "success"

    outbox = _read_outbox(workspace_root)[before_count:]
    event_types = [str(row["type"]) for row in outbox]
    assert "sync.cache_refreshed" in event_types


def test_pr_command_group_exists_in_python_cli() -> None:
    result = runner.invoke(app, ["pr", "--help"])
    assert result.exit_code == 0
    assert "create" in result.stdout
    assert "status" in result.stdout
    assert "merge" in result.stdout
    assert "checks" in result.stdout


def test_pr_commands_route_through_platform_adapter(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, repo_url = _init_bare_remote(tmp_path, "app")
    _write_workspace_spec(workspace_root, "app", repo_url)
    run_sync(workspace_root)

    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit="atlas",
        lane_name="feat-auth",
        type="feature",
        repos="app",
        branch="feat/auth",
        default_commands=[],
        source="pytest",
    )
    lane_proto.create_lane(ns)

    calls: list[tuple[str, object]] = []

    class FakeAdapter:
        name = "fake"

        def create_pr(self, request: CreatePRRequest) -> PRRef:
            calls.append(("create", request))
            return PRRef(
                repo=request.repo,
                number=42,
                url="https://example.test/pr/42",
                head_branch=request.head_branch,
                base_branch=request.base_branch,
                title=request.title,
            )

        def merge_pr(self, repo: str, number: int) -> PRRef:
            calls.append(("merge", (repo, number)))
            return PRRef(repo=repo, number=number, url="https://example.test/pr/42")

        def pr_status(self, repo: str, number: int) -> PRStatus:
            calls.append(("status", (repo, number)))
            ref = PRRef(repo=repo, number=number, url="https://example.test/pr/42")
            return PRStatus(ref=ref, state="OPEN", mergeable="MERGEABLE", checks=[PRCheck(name="ci", status="COMPLETED", conclusion="SUCCESS")])

        def list_prs(self, repo: str, *, head_branch: str | None = None) -> list[PRRef]:
            calls.append(("list", (repo, head_branch)))
            return [PRRef(repo=repo, number=42, url="https://example.test/pr/42", head_branch=head_branch, base_branch="main", title="feat/auth")]

        def pr_checks(self, repo: str, number: int) -> list[PRCheck]:
            calls.append(("checks", (repo, number)))
            return [PRCheck(name="ci", status="COMPLETED", conclusion="SUCCESS")]

    monkeypatch.setattr(app_module, "get_platform_adapter", lambda name="github": FakeAdapter())

    result = runner.invoke(app, ["pr", "create", str(workspace_root), "atlas", "feat-auth", "--json"])
    assert result.exit_code == 0
    assert any(kind == "create" for kind, _ in calls)

    result = runner.invoke(app, ["pr", "status", str(workspace_root), "atlas", "feat-auth", "--json"])
    assert result.exit_code == 0
    assert any(kind == "status" for kind, _ in calls)

    result = runner.invoke(app, ["pr", "checks", str(workspace_root), "atlas", "feat-auth", "--json"])
    assert result.exit_code == 0
    assert any(kind == "checks" for kind, _ in calls)

    result = runner.invoke(app, ["pr", "merge", str(workspace_root), "atlas", "feat-auth", "--json"])
    assert result.exit_code == 0
    assert any(kind == "merge" for kind, _ in calls)


def test_pr_create_persists_group_state_by_pr_group_id(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, app_url = _init_bare_remote(tmp_path, "app")
    _, api_url = _init_bare_remote(tmp_path, "api")
    _write_workspace_spec_multi(workspace_root, [("app", app_url), ("api", api_url)])
    run_sync(workspace_root)

    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit="atlas",
        lane_name="feat-router",
        type="feature",
        repos="app,api",
        branch="feat/router",
        default_commands=[],
        source="pytest",
    )
    lane_proto.create_lane(ns)

    class FakeAdapter:
        name = "fake"

        def create_pr(self, request: CreatePRRequest) -> PRRef:
            number = 41 if request.repo == "app" else 42
            return PRRef(
                repo=request.repo,
                number=number,
                url=f"https://example.test/{request.repo}/pull/{number}",
                head_branch=request.head_branch,
                base_branch=request.base_branch,
                title=request.title,
            )

        def merge_pr(self, repo: str, number: int) -> PRRef:  # pragma: no cover - not used here
            raise AssertionError("merge_pr should not be called")

        def pr_status(self, repo: str, number: int) -> PRStatus:  # pragma: no cover - not used here
            raise AssertionError("pr_status should not be called")

        def list_prs(self, repo: str, *, head_branch: str | None = None) -> list[PRRef]:  # pragma: no cover
            return []

        def pr_checks(self, repo: str, number: int) -> list[PRCheck]:  # pragma: no cover
            return []

    monkeypatch.setattr(app_module, "get_platform_adapter", lambda name="github": FakeAdapter())

    result = runner.invoke(app, ["pr", "create", str(workspace_root), "atlas", "feat-router", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pr_group_id"].startswith("pg_")
    assert len(payload["refs"]) == 2

    group_path = workspace_root / ".grip" / "pr_groups" / f'{payload["pr_group_id"]}.json'
    assert group_path.exists(), "group state should be stored by pr_group_id, not lane name"
    stored = json.loads(group_path.read_text())
    assert {item["repo"]: item["number"] for item in stored["refs"]} == {"app": 41, "api": 42}


def test_pr_status_aggregates_group_state(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, app_url = _init_bare_remote(tmp_path, "app")
    _, api_url = _init_bare_remote(tmp_path, "api")
    _write_workspace_spec_multi(workspace_root, [("app", app_url), ("api", api_url)])
    run_sync(workspace_root)

    pr_group_id = "pg_deadbeef"
    group_path = workspace_root / ".grip" / "pr_groups" / f"{pr_group_id}.json"
    group_path.parent.mkdir(parents=True, exist_ok=True)
    group_path.write_text(
        json.dumps(
            {
                "pr_group_id": pr_group_id,
                "owner_unit": "atlas",
                "lane_name": "feat-router",
                "platform": "github",
                "refs": [
                    {"repo": "app", "number": 41, "url": "https://example.test/app/41"},
                    {"repo": "api", "number": 42, "url": "https://example.test/api/42"},
                ],
            }
        )
    )

    class FakeAdapter:
        name = "fake"

        def create_pr(self, request: CreatePRRequest) -> PRRef:  # pragma: no cover
            raise AssertionError("create_pr should not be called")

        def merge_pr(self, repo: str, number: int) -> PRRef:  # pragma: no cover
            raise AssertionError("merge_pr should not be called")

        def pr_status(self, repo: str, number: int) -> PRStatus:
            state = "OPEN" if repo == "app" else "MERGED"
            ref = PRRef(repo=repo, number=number, url=f"https://example.test/{repo}/{number}")
            return PRStatus(ref=ref, state=state, mergeable="MERGEABLE", checks=[])

        def list_prs(self, repo: str, *, head_branch: str | None = None) -> list[PRRef]:  # pragma: no cover
            return []

        def pr_checks(self, repo: str, number: int) -> list[PRCheck]:  # pragma: no cover
            return []

    monkeypatch.setattr(app_module, "get_platform_adapter", lambda name="github": FakeAdapter())

    result = runner.invoke(app, ["pr", "status", str(workspace_root), "atlas", "feat-router", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["pr_group_id"] == pr_group_id
    assert payload["group_state"] == "partially_merged"


def test_pr_merge_reports_partial_failure_and_preserves_state(tmp_path: Path, monkeypatch) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, app_url = _init_bare_remote(tmp_path, "app")
    _, api_url = _init_bare_remote(tmp_path, "api")
    _write_workspace_spec_multi(workspace_root, [("app", app_url), ("api", api_url)])
    run_sync(workspace_root)

    pr_group_id = "pg_badmerge"
    group_path = workspace_root / ".grip" / "pr_groups" / f"{pr_group_id}.json"
    group_path.parent.mkdir(parents=True, exist_ok=True)
    group_path.write_text(
        json.dumps(
            {
                "pr_group_id": pr_group_id,
                "owner_unit": "atlas",
                "lane_name": "feat-router",
                "platform": "github",
                "refs": [
                    {"repo": "app", "number": 41, "url": "https://example.test/app/41"},
                    {"repo": "api", "number": 42, "url": "https://example.test/api/42"},
                ],
            }
        )
    )

    class FakeAdapter:
        name = "fake"

        def create_pr(self, request: CreatePRRequest) -> PRRef:  # pragma: no cover
            raise AssertionError("create_pr should not be called")

        def merge_pr(self, repo: str, number: int) -> PRRef:
            if repo == "api":
                raise RuntimeError("merge conflict")
            return PRRef(repo=repo, number=number, url=f"https://example.test/{repo}/{number}")

        def pr_status(self, repo: str, number: int) -> PRStatus:  # pragma: no cover
            raise AssertionError("pr_status should not be called")

        def list_prs(self, repo: str, *, head_branch: str | None = None) -> list[PRRef]:  # pragma: no cover
            return []

        def pr_checks(self, repo: str, number: int) -> list[PRCheck]:  # pragma: no cover
            return []

    monkeypatch.setattr(app_module, "get_platform_adapter", lambda name="github": FakeAdapter())

    result = runner.invoke(app, ["pr", "merge", str(workspace_root), "atlas", "feat-router", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "partial_failure"
    assert payload["pr_group_id"] == pr_group_id
    assert payload["merged"] == ["app"]
    assert payload["failed"][0]["repo"] == "api"

    stored = json.loads(group_path.read_text())
    assert stored["group_state"] == "partially_merged"


def test_sync_run_reports_terminal_blocked_event_on_lock_contention(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, repo_url = _init_bare_remote(tmp_path, "app")
    _write_workspace_spec(workspace_root, "app", repo_url)
    run_sync(workspace_root)

    lock_path = workspace_root / ".grip" / "state" / "sync.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a+", encoding="utf-8") as lock_fh:
        fcntl.flock(lock_fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        result = runner.invoke(app, ["sync", "run", str(workspace_root), "--json"])

    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert any(item["code"] == "sync_lock_held" for item in payload["blocked"])

    outbox = _read_outbox(workspace_root)
    assert any(row["type"] == "sync.conflict" for row in outbox)
    terminal = [row for row in outbox if row["type"] == "sync.completed" and row.get("status") == "blocked"]
    assert terminal, "lock contention must still emit terminal sync.completed status=blocked"


def test_sync_run_dirty_block_reports_blocked_without_mutation(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, repo_url = _init_bare_remote(tmp_path, "app")
    _write_workspace_spec(workspace_root, "app", repo_url)
    run_sync(workspace_root)

    repo_root = workspace_root / "repos" / "app"
    (repo_root / "README.md").write_text("dirty block\n")

    result = runner.invoke(app, ["sync", "run", str(workspace_root), "--dirty", "block", "--json"])
    assert result.exit_code == 1
    payload = json.loads(result.stdout)
    assert payload["status"] == "blocked"
    assert payload["dirty_mode"] == "block"
    assert "app" in payload["dirty_targets"]
    assert any(item["code"] == "dirty_shared_repo" for item in payload["blocked"])
    assert repo_root.joinpath("README.md").read_text() == "dirty block\n"
    assert _stash_list(repo_root) == []


def test_sync_run_dirty_stash_stashes_changes_and_continues(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, repo_url = _init_bare_remote(tmp_path, "app")
    _write_workspace_spec(workspace_root, "app", repo_url)
    run_sync(workspace_root)

    repo_root = workspace_root / "repos" / "app"
    (repo_root / "README.md").write_text("dirty stash\n")

    result = runner.invoke(app, ["sync", "run", str(workspace_root), "--dirty", "stash", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["dirty_mode"] == "stash"
    assert "app" in payload["dirty_targets"]
    assert _git(repo_root, "status", "--porcelain").stdout.strip() == ""
    assert _stash_list(repo_root), "stash mode should leave a git stash entry"

    outbox = _read_outbox(workspace_root)
    assert any(
        row["type"] == "sync.repo_skipped" and row.get("repo") == "app" and row.get("reason") == "dirty_stashed"
        for row in outbox
    )


def test_sync_run_dirty_discard_discards_changes_without_stash(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir()
    _, repo_url = _init_bare_remote(tmp_path, "app")
    _write_workspace_spec(workspace_root, "app", repo_url)
    run_sync(workspace_root)

    repo_root = workspace_root / "repos" / "app"
    (repo_root / "README.md").write_text("dirty discard\n")

    result = runner.invoke(app, ["sync", "run", str(workspace_root), "--dirty", "discard", "--json"])
    assert result.exit_code == 0
    payload = json.loads(result.stdout)
    assert payload["status"] == "success"
    assert payload["dirty_mode"] == "discard"
    assert "app" in payload["dirty_targets"]
    assert repo_root.joinpath("README.md").read_text() == "# app\n"
    assert _stash_list(repo_root) == []

    outbox = _read_outbox(workspace_root)
    assert any(
        row["type"] == "sync.repo_skipped" and row.get("repo") == "app" and row.get("reason") == "dirty_discarded"
        for row in outbox
    )
