from __future__ import annotations

import contextlib
import io
import json
import os
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import typer

from . import execops
from . import failures
from . import migration
from . import syncops
from .gitops import (
    branch_exists,
    checkout_branch,
    ensure_lane_checkout,
    fetch_ref,
    is_git_repo,
    refresh_existing_branch,
    remote_origin_url,
    repo_dirty,
    stash_if_dirty,
)
from .events import emit, EventType
from .hooks import HookContext, HookRuntimeError, apply_file_projections, load_repo_hooks, run_lifecycle_stage
from .platform import PRRef, get_platform_adapter
from . import spec_apply
from gr2.prototypes import lane_workspace_prototype as lane_proto
from gr2.prototypes import repo_maintenance_prototype as repo_proto


app = typer.Typer(
    help="Python-first gr2 CLI. This is the production UX proving layer before Rust."
)
repo_app = typer.Typer(help="Repo maintenance and inspection")
lane_app = typer.Typer(help="Lane creation and navigation")
lease_app = typer.Typer(help="Lane lease operations")
review_app = typer.Typer(help="Review and reviewer requirement operations")
pr_app = typer.Typer(help="Cross-repo PR orchestration")
workspace_app = typer.Typer(help="Workspace bootstrap and materialization")
spec_app = typer.Typer(help="Declarative workspace spec operations")
exec_app = typer.Typer(help="Lane-aware execution planning and execution")
sync_app = typer.Typer(help="Workspace-wide sync inspection and execution")

app.add_typer(repo_app, name="repo")
app.add_typer(lane_app, name="lane")
lane_app.add_typer(lease_app, name="lease")
app.add_typer(review_app, name="review")
app.add_typer(pr_app, name="pr")
app.add_typer(workspace_app, name="workspace")
app.add_typer(spec_app, name="spec")
app.add_typer(exec_app, name="exec")
app.add_typer(sync_app, name="sync")


def _workspace_repo_spec(workspace_root: Path, repo_name: str) -> dict[str, object]:
    spec = lane_proto.load_workspace_spec(workspace_root)
    for repo in spec.get("repos", []):
        if repo.get("name") == repo_name:
            return repo
    raise SystemExit(f"repo not found in workspace spec: {repo_name}")


def _workspace_spec_path(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "workspace_spec.toml"


def _lane_repo_root(workspace_root: Path, owner_unit: str, lane_name: str, repo_name: str) -> Path:
    return lane_proto.lane_dir(workspace_root, owner_unit, lane_name) / "repos" / repo_name


def _materialize_lane_repos(workspace_root: Path, owner_unit: str, lane_name: str, *, manual_hooks: bool = False) -> None:
    lane_doc = lane_proto.load_lane_doc(workspace_root, owner_unit, lane_name)
    branch_map = dict(lane_doc.get("branch_map", {}))
    lane_root = lane_proto.lane_dir(workspace_root, owner_unit, lane_name)

    for repo_name in lane_doc.get("repos", []):
        repo_spec = _workspace_repo_spec(workspace_root, repo_name)
        source_repo_root = (workspace_root / str(repo_spec["path"])).resolve()
        if not source_repo_root.exists():
            raise SystemExit(f"source repo path does not exist for lane materialization: {source_repo_root}")
        target_repo_root = _lane_repo_root(workspace_root, owner_unit, lane_name, repo_name)
        first_materialize = ensure_lane_checkout(
            source_repo_root=source_repo_root,
            target_repo_root=target_repo_root,
            branch=branch_map[repo_name],
        )
        hooks = load_repo_hooks(target_repo_root)
        if not hooks:
            continue
        ctx = HookContext(
            workspace_root=workspace_root,
            lane_root=lane_root,
            repo_root=target_repo_root,
            repo_name=repo_name,
            lane_owner=owner_unit,
            lane_subject=repo_name,
            lane_name=lane_name,
        )
        apply_file_projections(hooks, ctx)
        run_lifecycle_stage(
            hooks,
            "on_materialize",
            ctx,
            repo_dirty=repo_dirty(target_repo_root),
            first_materialize=first_materialize,
            allow_manual=manual_hooks,
        )


def _run_lane_stage(workspace_root: Path, owner_unit: str, lane_name: str, stage: str, *, manual_hooks: bool = False) -> None:
    lane_doc = lane_proto.load_lane_doc(workspace_root, owner_unit, lane_name)
    lane_root = lane_proto.lane_dir(workspace_root, owner_unit, lane_name)
    for repo_name in lane_doc.get("repos", []):
        repo_root = _lane_repo_root(workspace_root, owner_unit, lane_name, repo_name)
        if not repo_root.exists():
            continue
        branch = dict(lane_doc.get("branch_map", {})).get(repo_name)
        if branch:
            checkout_branch(repo_root, branch)
        hooks = load_repo_hooks(repo_root)
        if not hooks:
            continue
        ctx = HookContext(
            workspace_root=workspace_root,
            lane_root=lane_root,
            repo_root=repo_root,
            repo_name=repo_name,
            lane_owner=owner_unit,
            lane_subject=repo_name,
            lane_name=lane_name,
        )
        run_lifecycle_stage(
            hooks,
            stage,
            ctx,
            repo_dirty=repo_dirty(repo_root),
            first_materialize=False,
            allow_manual=manual_hooks,
        )


def _prepare_review_branch(workspace_root: Path, repo: str, pr_number: int, branch: str | None) -> str:
    repo_spec = _workspace_repo_spec(workspace_root, repo)
    repo_root = (workspace_root / str(repo_spec["path"])).resolve()
    if not repo_root.exists():
        raise SystemExit(f"shared repo missing for review checkout: {repo_root}\nrun `gr2 apply {workspace_root} --yes` first")

    target_branch = branch or f"pr/{pr_number}"
    source_ref = f"refs/heads/{branch}" if branch else f"refs/pull/{pr_number}/head"

    if branch_exists(repo_root, target_branch):
        refresh_existing_branch(repo_root, "origin", source_ref, target_branch)
        return target_branch

    if branch:
        fetch_ref(repo_root, "origin", source_ref, target_branch)
        return target_branch

    fetch_ref(repo_root, "origin", source_ref, target_branch)
    return target_branch


def _create_review_lane_metadata(
    workspace_root: Path,
    owner_unit: str,
    repo: str,
    pr_number: int,
    *,
    lane_name: str | None = None,
    branch: str | None = None,
) -> str:
    review_lane = lane_name or f"review-{pr_number}"
    review_branch = branch or f"pr/{pr_number}"
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        repo=repo,
        pr_number=pr_number,
        lane_name=review_lane,
        branch=review_branch,
    )
    with contextlib.redirect_stdout(io.StringIO()):
        _exit(lane_proto.create_review_lane(ns))
    return review_lane


def _repo_hook_context(workspace_root: Path, repo_root: Path) -> HookContext:
    return HookContext(
        workspace_root=workspace_root,
        lane_root=repo_root,
        repo_root=repo_root,
        repo_name=repo_root.name,
        lane_owner="workspace",
        lane_subject=repo_root.name,
        lane_name="workspace",
    )


def _resolve_lane_name(workspace_root: Path, owner_unit: str, lane_name: Optional[str]) -> str:
    if lane_name:
        return lane_name
    current_doc = lane_proto.load_current_lane_doc(workspace_root, owner_unit)
    return str(current_doc["current"]["lane_name"])


def _pr_groups_root(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "pr_groups"


def _pr_group_path(workspace_root: Path, pr_group_id: str) -> Path:
    return _pr_groups_root(workspace_root) / f"{pr_group_id}.json"


def _write_pr_group(workspace_root: Path, payload: dict[str, object]) -> Path:
    pr_group_id = str(payload["pr_group_id"])
    path = _pr_group_path(workspace_root, pr_group_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")
    return path


def _find_pr_group(workspace_root: Path, owner_unit: str, lane_name: str) -> tuple[Path, dict[str, object]]:
    root = _pr_groups_root(workspace_root)
    if not root.exists():
        raise SystemExit(f"pr group not found for {owner_unit}/{lane_name}: {root}")
    for path in sorted(root.glob("*.json")):
        doc = json.loads(path.read_text())
        if doc.get("owner_unit") == owner_unit and doc.get("lane_name") == lane_name:
            return path, doc
    raise SystemExit(f"pr group not found for {owner_unit}/{lane_name}: {root}")


def _group_state_from_statuses(statuses: list[dict[str, object]]) -> str:
    states = [str(item.get("state", "")).upper() for item in statuses]
    if not states:
        return "empty"
    if all(state == "MERGED" for state in states):
        return "merged"
    if any(state == "MERGED" for state in states):
        return "partially_merged"
    if all(state in {"OPEN", "MERGEABLE", "CLEAN"} for state in states):
        return "open"
    return "mixed"


def _repo_slug_from_url(url: str, fallback_name: str) -> str:
    cleaned = url.strip()
    if cleaned.startswith("git@github.com:"):
        slug = cleaned.split("git@github.com:", 1)[1]
        return slug.removesuffix(".git")
    if cleaned.startswith("https://github.com/"):
        slug = cleaned.split("https://github.com/", 1)[1]
        return slug.removesuffix(".git")
    return fallback_name


def _write_workspace_spec(workspace_root: Path, repos: list[dict[str, str]], default_unit: str) -> Path:
    spec_path = _workspace_spec_path(workspace_root)
    spec_path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        f'workspace_name = "{workspace_root.name}"',
        "",
    ]
    for repo in repos:
        lines.extend(
            [
                "[[repos]]",
                f'name = "{repo["name"]}"',
                f'path = "{repo["path"]}"',
                f'url = "{repo["url"]}"',
                "",
            ]
        )
    lines.extend(
        [
            "[[units]]",
            f'name = "{default_unit}"',
            f'path = "agents/{default_unit}/home"',
            "repos = [" + ", ".join(f'"{repo["name"]}"' for repo in repos) + "]",
            "",
        ]
    )
    spec_path.write_text("\n".join(lines))
    return spec_path


def _scan_existing_repos(workspace_root: Path) -> list[dict[str, str]]:
    repos: list[dict[str, str]] = []
    for child in sorted(workspace_root.iterdir()):
        if child.name.startswith("."):
            continue
        if child.name == "agents":
            continue
        if not child.is_dir():
            continue
        if not is_git_repo(child):
            continue
        url = remote_origin_url(child)
        repos.append(
            {
                "name": child.name,
                "path": child.relative_to(workspace_root).as_posix(),
                "url": url or "",
            }
        )
    return repos


def _exit(code: int) -> None:
    if code != 0:
        raise typer.Exit(code=code)


@sync_app.command("status")
def sync_status(
    workspace_root: Path,
    dirty_mode: str = typer.Option("stash", "--dirty", help="Dirty-state handling: stash, block, or discard"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Inspect workspace-wide sync readiness without mutating any repo state."""
    workspace_root = workspace_root.resolve()
    plan = syncops.build_sync_plan(workspace_root, dirty_mode=dirty_mode)
    if json_output:
        typer.echo(json.dumps(plan.as_dict(), indent=2))
        return
    typer.echo(syncops.render_sync_plan(plan))


@sync_app.command("run")
def sync_run(
    workspace_root: Path,
    dirty_mode: str = typer.Option("stash", "--dirty", help="Dirty-state handling: stash, block, or discard"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Execute the current sync plan, stopping on the first blocking runtime failure."""
    workspace_root = workspace_root.resolve()
    result = syncops.run_sync(workspace_root, dirty_mode=dirty_mode)
    if json_output:
        typer.echo(json.dumps(result.as_dict(), indent=2))
    else:
        typer.echo(syncops.render_sync_result(result))
    if result.status in {"blocked", "failed", "partial_failure"}:
        raise typer.Exit(code=1)


@workspace_app.command("init")
def workspace_init(
    workspace_root: Path,
    default_unit: str = typer.Option("default", help="Default owner unit for scanned repos"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Create a bare workspace_spec.toml by scanning an existing directory of repos."""
    workspace_root = workspace_root.resolve()
    repos = _scan_existing_repos(workspace_root)
    if not repos:
        raise SystemExit(f"no git repos found to initialize workspace spec under: {workspace_root}")
    spec_path = _write_workspace_spec(workspace_root, repos, default_unit)
    payload = {
        "workspace_root": str(workspace_root),
        "spec_path": str(spec_path),
        "repo_count": len(repos),
        "repos": repos,
        "default_unit": default_unit,
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        lines = [
            "WorkspaceInit",
            f"workspace_root = {workspace_root}",
            f"spec_path = {spec_path}",
            f"default_unit = {default_unit}",
            f"repo_count = {len(repos)}",
            "REPOS",
        ]
        lines.extend(f"- {repo['name']}\t{repo['path']}\t{repo['url'] or '-'}" for repo in repos)
        typer.echo("\n".join(lines))


@workspace_app.command("materialize")
def workspace_materialize(
    workspace_root: Path,
    yes: bool = typer.Option(False, "--yes", help="Pre-approve plans with more than 3 operations"),
    manual_hooks: bool = typer.Option(False, "--manual-hooks", help="Also run lifecycle hooks marked when=manual"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Read workspace_spec.toml and apply the current workspace materialization plan."""
    workspace_root = workspace_root.resolve()
    payload = spec_apply.apply_plan(workspace_root, yes=yes, manual_hooks=manual_hooks)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(spec_apply.render_apply_result(payload))


@workspace_app.command("detect-gr1")
def workspace_detect_gr1(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Detect whether a workspace is using the gr1 (.gitgrip) layout."""
    workspace_root = workspace_root.resolve()
    payload = migration.detect_gr1_workspace(workspace_root)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(migration.render_detection(payload))
    if not payload["detected"]:
        raise typer.Exit(code=1)


@workspace_app.command("migrate-gr1")
def workspace_migrate_gr1(
    workspace_root: Path,
    force: bool = typer.Option(False, "--force", help="Allow overwrite of an existing .grip/workspace_spec.toml"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Convert an existing gr1 (.gitgrip) workspace into parallel gr2 (.grip) layout."""
    workspace_root = workspace_root.resolve()
    payload = migration.migrate_gr1_workspace(workspace_root, force=force)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(migration.render_migration(payload))


@spec_app.command("show")
def spec_show(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show the current workspace spec."""
    workspace_root = workspace_root.resolve()
    typer.echo(spec_apply.show_spec(workspace_root, json_output=json_output))


@spec_app.command("validate")
def spec_validate(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Validate the current workspace spec."""
    workspace_root = workspace_root.resolve()
    issues = spec_apply.validate_spec(workspace_root)
    payload = {
        "workspace_root": str(workspace_root),
        "valid": not any(issue.level == "error" for issue in issues),
        "issues": [issue.as_dict() for issue in issues],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(spec_apply.render_validation(issues))
    if not payload["valid"]:
        raise typer.Exit(code=1)


@app.command("plan")
def workspace_plan(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Build a Python gr2 execution plan from the workspace spec."""
    workspace_root = workspace_root.resolve()
    _, operations = spec_apply.build_plan(workspace_root)
    if json_output:
        typer.echo(json.dumps([item.as_dict() for item in operations], indent=2))
    else:
        typer.echo(spec_apply.render_plan(operations))


@app.command("apply")
def workspace_apply(
    workspace_root: Path,
    yes: bool = typer.Option(False, "--yes", help="Pre-approve plans with more than 3 operations"),
    manual_hooks: bool = typer.Option(False, "--manual-hooks", help="Also run lifecycle hooks marked when=manual"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Apply the Python gr2 execution plan."""
    workspace_root = workspace_root.resolve()
    payload = spec_apply.apply_plan(workspace_root, yes=yes, manual_hooks=manual_hooks)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(spec_apply.render_apply_result(payload))


@exec_app.command("status")
def exec_status(
    workspace_root: Path,
    owner_unit: str,
    lane_name: Optional[str] = typer.Argument(None, help="Lane name. Defaults to the unit's current lane."),
    repos: Optional[str] = typer.Option(None, help="Optional comma-separated repo subset"),
    actor: str = typer.Option("agent:exec-status", help="Actor label for lease conflict evaluation"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show lane-aware execution status for a lane."""
    workspace_root = workspace_root.resolve()
    payload = execops.exec_status_payload(workspace_root, owner_unit, lane_name, repos=repos, actor=actor)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(execops.render_exec_status(payload))


@exec_app.command("run", context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def exec_run(
    ctx: typer.Context,
    workspace_root: Path,
    owner_unit: str,
    command: list[str] = typer.Argument(None, help="Command to run inside each selected lane repo"),
    lane_name: Optional[str] = typer.Option(None, "--lane", help="Lane name. Defaults to the unit's current lane."),
    repos: Optional[str] = typer.Option(None, help="Optional comma-separated repo subset"),
    actor: str = typer.Option(..., help="Actor label, e.g. agent:atlas"),
    ttl_seconds: int = typer.Option(900, "--ttl-seconds", help="TTL for the temporary exec lease"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Run a command across the repos in a lane."""
    workspace_root = workspace_root.resolve()
    full_command = list(command or []) + list(ctx.args)
    if not full_command:
        raise typer.BadParameter("missing command to run")
    payload = execops.run_exec(
        workspace_root,
        owner_unit,
        lane_name,
        actor=actor,
        command=full_command,
        repos=repos,
        ttl_seconds=ttl_seconds,
    )
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(execops.render_exec_run(payload))
    if payload.get("status") in {"blocked", "failed"}:
        raise typer.Exit(code=1)


@repo_app.command("status")
def repo_status(
    workspace_root: Path,
    spec: Optional[Path] = typer.Option(None, help="Path to workspace_spec.toml"),
    policy: Optional[Path] = typer.Option(None, help="Optional repo maintenance policy TOML"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show repo maintenance status without mutating workspace state."""
    workspace_root = workspace_root.resolve()
    spec_path = (spec or workspace_root / ".grip" / "workspace_spec.toml").resolve()
    spec_doc = repo_proto.read_workspace_spec(spec_path)
    policy_doc = repo_proto.read_policy(policy.resolve() if policy else None)

    actions = []
    for target in repo_proto.derive_targets(workspace_root, spec_doc):
        status = repo_proto.inspect_repo(target.path)
        repo_policy = repo_proto.policy_for(target, policy_doc)
        actions.append(repo_proto.classify(target, status, repo_policy))

    if json_output:
        typer.echo(json.dumps([item.as_dict() for item in actions], indent=2))
    else:
        typer.echo(repo_proto.render_table(actions))


@repo_app.command("hooks")
def repo_hooks_show(
    repo_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Inspect parsed .gr2/hooks.toml for a repo."""
    hooks = load_repo_hooks(repo_root.resolve())
    if hooks is None:
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(hooks.as_dict(), indent=2))
    else:
        typer.echo(json.dumps(hooks.as_dict(), indent=2))


@repo_app.command("hook-run")
def repo_hook_run(
    workspace_root: Path,
    repo_root: Path,
    stage: str = typer.Argument(..., help="Lifecycle stage: on_materialize | on_enter | on_exit"),
    manual: bool = typer.Option(False, "--manual", help="Allow hooks with when=manual to run"),
    first_materialize: bool = typer.Option(False, "--first-materialize", help="Treat this invocation as first materialization"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Run repo hooks explicitly for one lifecycle stage."""
    workspace_root = workspace_root.resolve()
    repo_root = repo_root.resolve()
    if stage not in {"on_materialize", "on_enter", "on_exit"}:
        raise typer.BadParameter("stage must be one of: on_materialize, on_enter, on_exit")
    hooks = load_repo_hooks(repo_root)
    if hooks is None:
        raise SystemExit(f"no .gr2/hooks.toml found in repo: {repo_root}")
    ctx = _repo_hook_context(workspace_root, repo_root)
    results = run_lifecycle_stage(
        hooks,
        stage,
        ctx,
        repo_dirty=repo_dirty(repo_root),
        first_materialize=first_materialize,
        allow_manual=manual,
    )
    payload = {
        "workspace_root": str(workspace_root),
        "repo_root": str(repo_root),
        "stage": stage,
        "results": [item.as_dict() for item in results],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload, indent=2))


@repo_app.command("projection-run")
def repo_projection_run(
    workspace_root: Path,
    repo_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Apply file projections explicitly for one repo."""
    workspace_root = workspace_root.resolve()
    repo_root = repo_root.resolve()
    hooks = load_repo_hooks(repo_root)
    if hooks is None:
        raise SystemExit(f"no .gr2/hooks.toml found in repo: {repo_root}")
    ctx = _repo_hook_context(workspace_root, repo_root)
    results = apply_file_projections(hooks, ctx)
    payload = {
        "workspace_root": str(workspace_root),
        "repo_root": str(repo_root),
        "results": [item.as_dict() for item in results],
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload, indent=2))


@lane_app.command("create")
def lane_create(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    repos: str = typer.Option(..., help="Comma-separated repo names"),
    branch: str = typer.Option(..., help="Default branch or repo=branch mappings"),
    lane_type: str = typer.Option("feature", "--type", help="Lane type"),
    source: str = typer.Option("manual", help="Creation source label"),
    command: list[str] = typer.Option(None, "--command", help="Default command for the lane"),
    manual_hooks: bool = typer.Option(False, "--manual-hooks", help="Also run lifecycle hooks marked when=manual during lane materialization"),
) -> None:
    """Create a lane."""
    workspace_root = workspace_root.resolve()
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        lane_name=lane_name,
        repos=repos,
        branch=branch,
        type=lane_type,
        source=source,
        default_commands=command or [],
    )
    _exit(lane_proto.create_lane(ns))
    _materialize_lane_repos(workspace_root, owner_unit, lane_name, manual_hooks=manual_hooks)
    repo_list = [r.strip() for r in repos.split(",")]
    branch_parts = branch.split(",")
    branch_map = {}
    for part in branch_parts:
        if "=" in part:
            k, v = part.split("=", 1)
            branch_map[k.strip()] = v.strip()
        else:
            for r in repo_list:
                branch_map[r] = part.strip()
    emit(
        event_type=EventType.LANE_CREATED,
        workspace_root=workspace_root,
        actor=source,
        owner_unit=owner_unit,
        payload={
            "lane_name": lane_name,
            "lane_type": lane_type,
            "repos": repo_list,
            "branch_map": branch_map,
        },
    )


@lane_app.command("enter")
def lane_enter(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    actor: str = typer.Option(..., help="Actor label, e.g. agent:atlas"),
    notify_channel: bool = typer.Option(False, "--notify-channel"),
    recall: bool = typer.Option(False, "--recall"),
    manual_hooks: bool = typer.Option(False, "--manual-hooks", help="Also run lifecycle hooks marked when=manual"),
) -> None:
    """Enter a lane and optionally emit channel/recall-compatible events."""
    workspace_root = workspace_root.resolve()
    unresolved = failures.unresolved_lane_failure(workspace_root, owner_unit, lane_name)
    if unresolved:
        typer.echo(
            json.dumps(
                {
                    "status": "blocked",
                    "code": "unresolved_failure_marker",
                    "operation_id": unresolved["operation_id"],
                    "lane_name": lane_name,
                },
                indent=2,
            )
        )
        raise typer.Exit(code=1)
    try:
        _run_lane_stage(workspace_root, owner_unit, lane_name, "on_enter", manual_hooks=manual_hooks)
    except HookRuntimeError as exc:
        payload = exc.payload
        repo_name = Path(str(payload.get("cwd", ""))).name or lane_name
        event = failures.write_failure_marker(
            workspace_root,
            operation="lane.enter",
            stage=str(payload.get("stage", "on_enter")),
            hook_name=str(payload.get("hook", payload.get("name", "unknown"))),
            repo=repo_name,
            owner_unit=owner_unit,
            lane_name=lane_name,
            partial_state={},
            event_id=None,
        )
        typer.echo(json.dumps(event, indent=2))
        raise typer.Exit(code=1)
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        lane_name=lane_name,
        actor=actor,
        notify_channel=notify_channel,
        recall=recall,
    )
    _exit(lane_proto.enter_lane(ns))
    lane_doc = lane_proto.load_lane_doc(workspace_root, owner_unit, lane_name)
    emit(
        event_type=EventType.LANE_ENTERED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=owner_unit,
        payload={
            "lane_name": lane_name,
            "lane_type": lane_doc.get("type", "feature"),
            "repos": lane_doc.get("repos", []),
        },
    )


@lane_app.command("resolve")
def lane_resolve(
    workspace_root: Path,
    owner_unit: str,
    operation_id: str,
    actor: str = typer.Option(..., help="Actor label, e.g. agent:atlas"),
    resolution: str = typer.Option(..., help="Resolution note: retry | skip | escalate"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Resolve a blocking failure marker for a lane-scoped operation."""
    workspace_root = workspace_root.resolve()
    payload = failures.resolve_failure_marker(
        workspace_root,
        operation_id=operation_id,
        resolved_by=actor,
        resolution=resolution,
        owner_unit=owner_unit,
    )
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload, indent=2))


@lane_app.command("exit")
def lane_exit(
    workspace_root: Path,
    owner_unit: str,
    actor: str = typer.Option(..., help="Actor label, e.g. human:layne"),
    notify_channel: bool = typer.Option(False, "--notify-channel"),
    recall: bool = typer.Option(False, "--recall"),
    manual_hooks: bool = typer.Option(False, "--manual-hooks", help="Also run lifecycle hooks marked when=manual"),
) -> None:
    """Exit the current lane for a unit."""
    workspace_root = workspace_root.resolve()
    current_doc = lane_proto.load_current_lane_doc(workspace_root, owner_unit)
    lane_name = current_doc["current"]["lane_name"]
    lane_doc = lane_proto.load_lane_doc(workspace_root, owner_unit, lane_name)
    stashed_repos: list[str] = []
    for repo_name in lane_doc.get("repos", []):
        repo_root = _lane_repo_root(workspace_root, owner_unit, lane_name, repo_name)
        if repo_root.exists():
            if stash_if_dirty(repo_root, f"gr2 exit {owner_unit}/{lane_name}"):
                stashed_repos.append(repo_name)
    _run_lane_stage(workspace_root, owner_unit, lane_name, "on_exit", manual_hooks=manual_hooks)
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        actor=actor,
        notify_channel=notify_channel,
        recall=recall,
    )
    _exit(lane_proto.exit_lane(ns))
    emit(
        event_type=EventType.LANE_EXITED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=owner_unit,
        payload={
            "lane_name": lane_name,
            "stashed_repos": stashed_repos,
        },
    )


@lane_app.command("current")
def lane_current(
    workspace_root: Path,
    owner_unit: str,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show current lane and recent history for a unit."""
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        json=json_output,
    )
    _exit(lane_proto.current_lane(ns))


@lease_app.command("acquire")
def lane_lease_acquire(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    actor: str = typer.Option(...),
    mode: str = typer.Option(..., help="edit | exec | review"),
    ttl_seconds: int = typer.Option(900, "--ttl-seconds"),
    force: bool = typer.Option(False, "--force"),
) -> None:
    """Acquire a lease for a lane."""
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        lane_name=lane_name,
        actor=actor,
        mode=mode,
        ttl_seconds=ttl_seconds,
        force=force,
    )
    _exit(lane_proto.acquire_lane_lease(ns))
    emit(
        event_type=EventType.LEASE_ACQUIRED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=owner_unit,
        payload={
            "lane_name": lane_name,
            "mode": mode,
            "ttl_seconds": ttl_seconds,
            "lease_id": f"{owner_unit}:{lane_name}",
        },
    )


@lease_app.command("release")
def lane_lease_release(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    actor: str = typer.Option(...),
) -> None:
    """Release a lease for a lane."""
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        lane_name=lane_name,
        actor=actor,
    )
    _exit(lane_proto.release_lane_lease(ns))
    emit(
        event_type=EventType.LEASE_RELEASED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=owner_unit,
        payload={
            "lane_name": lane_name,
            "lease_id": f"{owner_unit}:{lane_name}",
        },
    )


@lease_app.command("show")
def lane_lease_show(workspace_root: Path, owner_unit: str, lane_name: str) -> None:
    """Show active leases for a lane."""
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        lane_name=lane_name,
    )
    _exit(lane_proto.show_lane_leases(ns))


@review_app.command("requirements")
def review_requirements(
    workspace_root: Path,
    repo: str,
    pr_number: int,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Check whether compiled review requirements are satisfied for a repo and PR."""
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        repo=repo,
        pr_number=pr_number,
        json=json_output,
    )
    _exit(lane_proto.check_review_requirements(ns))


@review_app.command("checkout-pr")
def review_checkout_pr(
    workspace_root: Path,
    owner_unit: str,
    repo: str,
    pr_number: int,
    lane_name: Optional[str] = typer.Option(None, "--lane", help="Override the review lane name"),
    branch: Optional[str] = typer.Option(None, "--branch", help="Override the source branch/ref to fetch"),
    enter: bool = typer.Option(False, "--enter", help="Enter the review lane after materialization"),
    actor: Optional[str] = typer.Option(None, "--actor", help="Actor label to use when entering the lane"),
    manual_hooks: bool = typer.Option(False, "--manual-hooks", help="Also run lifecycle hooks marked when=manual during materialization/enter"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Create and materialize a review lane for a PR."""
    workspace_root = workspace_root.resolve()
    resolved_branch = _prepare_review_branch(workspace_root, repo, pr_number, branch)
    resolved_lane = _create_review_lane_metadata(
        workspace_root,
        owner_unit,
        repo,
        pr_number,
        lane_name=lane_name,
        branch=resolved_branch,
    )
    _materialize_lane_repos(workspace_root, owner_unit, resolved_lane, manual_hooks=manual_hooks)

    entered = False
    if enter:
        if not actor:
            raise typer.BadParameter("--actor is required when using --enter")
        _run_lane_stage(workspace_root, owner_unit, resolved_lane, "on_enter", manual_hooks=manual_hooks)
        ns = SimpleNamespace(
            workspace_root=workspace_root,
            owner_unit=owner_unit,
            lane_name=resolved_lane,
            actor=actor,
            notify_channel=False,
            recall=False,
        )
        with contextlib.redirect_stdout(io.StringIO()):
            _exit(lane_proto.enter_lane(ns))
        entered = True

    payload = {
        "workspace_root": str(workspace_root),
        "owner_unit": owner_unit,
        "repo": repo,
        "pr_number": pr_number,
        "lane_name": resolved_lane,
        "branch": resolved_branch,
        "entered": entered,
        "lane_repo_root": str(_lane_repo_root(workspace_root, owner_unit, resolved_lane, repo)),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload, indent=2))


@pr_app.command("create")
def pr_create(
    workspace_root: Path,
    owner_unit: str,
    lane_name: Optional[str] = typer.Argument(None, help="Lane name. Defaults to the unit's current lane."),
    platform: str = typer.Option("github", "--platform", help="Platform adapter name"),
    base_branch: str = typer.Option("main", "--base", help="Base branch for created PRs"),
    draft: bool = typer.Option(False, "--draft", help="Create PRs as drafts"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Create a grouped set of per-repo PRs for a lane."""
    workspace_root = workspace_root.resolve()
    resolved_lane = _resolve_lane_name(workspace_root, owner_unit, lane_name)
    lane_doc = lane_proto.load_lane_doc(workspace_root, owner_unit, resolved_lane)
    spec = lane_proto.load_workspace_spec(workspace_root)
    adapter = get_platform_adapter(platform)
    pr_group_id = f"pg_{os.urandom(4).hex()}"
    refs: list[dict[str, object]] = []
    branch_map = dict(lane_doc.get("branch_map", {}))
    for repo_name in lane_doc.get("repos", []):
        repo_spec = next(repo for repo in spec.get("repos", []) if repo.get("name") == repo_name)
        request = CreatePRRequest(
            repo=_repo_slug_from_url(str(repo_spec.get("url", "")), repo_name),
            title=resolved_lane,
            body=f"gr2 PR group {pr_group_id} for {owner_unit}/{resolved_lane}",
            head_branch=str(branch_map.get(repo_name, resolved_lane)),
            base_branch=base_branch,
            draft=draft,
        )
        ref = adapter.create_pr(request)
        refs.append(ref.as_dict())
    payload = {
        "pr_group_id": pr_group_id,
        "owner_unit": owner_unit,
        "lane_name": resolved_lane,
        "platform": platform,
        "refs": refs,
        "group_state": "open",
    }
    path = _write_pr_group(workspace_root, payload)
    payload["state_path"] = str(path)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload, indent=2))


@pr_app.command("status")
def pr_status(
    workspace_root: Path,
    owner_unit: str,
    lane_name: Optional[str] = typer.Argument(None, help="Lane name. Defaults to the unit's current lane."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show grouped PR status for a lane."""
    workspace_root = workspace_root.resolve()
    resolved_lane = _resolve_lane_name(workspace_root, owner_unit, lane_name)
    group_path, group = _find_pr_group(workspace_root, owner_unit, resolved_lane)
    adapter = get_platform_adapter(str(group.get("platform", "github")))
    statuses = []
    for ref_doc in group.get("refs", []):
        ref = PRRef(**ref_doc)
        statuses.append(adapter.pr_status(ref.repo, int(ref.number)).as_dict())
    group["statuses"] = statuses
    group["group_state"] = _group_state_from_statuses(statuses)
    _write_pr_group(workspace_root, group)
    payload = {
        "pr_group_id": group["pr_group_id"],
        "owner_unit": owner_unit,
        "lane_name": resolved_lane,
        "group_state": group["group_state"],
        "statuses": statuses,
        "state_path": str(group_path),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload, indent=2))


@pr_app.command("checks")
def pr_checks(
    workspace_root: Path,
    owner_unit: str,
    lane_name: Optional[str] = typer.Argument(None, help="Lane name. Defaults to the unit's current lane."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show grouped PR checks for a lane."""
    workspace_root = workspace_root.resolve()
    resolved_lane = _resolve_lane_name(workspace_root, owner_unit, lane_name)
    group_path, group = _find_pr_group(workspace_root, owner_unit, resolved_lane)
    adapter = get_platform_adapter(str(group.get("platform", "github")))
    rows = []
    for ref_doc in group.get("refs", []):
        ref = PRRef(**ref_doc)
        rows.append(
            {
                "repo": ref.repo,
                "number": ref.number,
                "checks": [item.as_dict() for item in adapter.pr_checks(ref.repo, int(ref.number))],
            }
        )
    payload = {
        "pr_group_id": group["pr_group_id"],
        "owner_unit": owner_unit,
        "lane_name": resolved_lane,
        "checks": rows,
        "state_path": str(group_path),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload, indent=2))


@pr_app.command("merge")
def pr_merge(
    workspace_root: Path,
    owner_unit: str,
    lane_name: Optional[str] = typer.Argument(None, help="Lane name. Defaults to the unit's current lane."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Merge grouped PRs for a lane."""
    workspace_root = workspace_root.resolve()
    resolved_lane = _resolve_lane_name(workspace_root, owner_unit, lane_name)
    group_path, group = _find_pr_group(workspace_root, owner_unit, resolved_lane)
    adapter = get_platform_adapter(str(group.get("platform", "github")))
    merged: list[str] = []
    failed: list[dict[str, object]] = []
    for ref_doc in group.get("refs", []):
        ref = PRRef(**ref_doc)
        try:
            adapter.merge_pr(ref.repo, int(ref.number))
            merged.append(ref.repo)
        except Exception as exc:
            failed.append({"repo": ref.repo, "number": ref.number, "reason": str(exc)})
            break
    if failed:
        group["group_state"] = "partially_merged" if merged else "merge_failed"
        group["merged"] = merged
        _write_pr_group(workspace_root, group)
        payload = {
            "status": "partial_failure" if merged else "failed",
            "pr_group_id": group["pr_group_id"],
            "owner_unit": owner_unit,
            "lane_name": resolved_lane,
            "merged": merged,
            "failed": failed,
            "state_path": str(group_path),
        }
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
        else:
            typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=1)
    payload = {
        "pr_group_id": group["pr_group_id"],
        "owner_unit": owner_unit,
        "lane_name": resolved_lane,
        "merged": merged,
        "state_path": str(group_path),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload, indent=2))


if __name__ == "__main__":
    app()
