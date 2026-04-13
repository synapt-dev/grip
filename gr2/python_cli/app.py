from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import typer

from .gitops import checkout_branch, ensure_lane_checkout, repo_dirty, stash_if_dirty
from .hooks import HookContext, apply_file_projections, load_repo_hooks, run_lifecycle_stage
from gr2.prototypes import lane_workspace_prototype as lane_proto
from gr2.prototypes import repo_maintenance_prototype as repo_proto


app = typer.Typer(
    help="Python-first gr2 CLI. This is the production UX proving layer before Rust."
)
repo_app = typer.Typer(help="Repo maintenance and inspection")
lane_app = typer.Typer(help="Lane creation and navigation")
lease_app = typer.Typer(help="Lane lease operations")
review_app = typer.Typer(help="Review and reviewer requirement operations")

app.add_typer(repo_app, name="repo")
app.add_typer(lane_app, name="lane")
lane_app.add_typer(lease_app, name="lease")
app.add_typer(review_app, name="review")


def _workspace_repo_spec(workspace_root: Path, repo_name: str) -> dict[str, object]:
    spec = lane_proto.load_workspace_spec(workspace_root)
    for repo in spec.get("repos", []):
        if repo.get("name") == repo_name:
            return repo
    raise SystemExit(f"repo not found in workspace spec: {repo_name}")


def _lane_repo_root(workspace_root: Path, owner_unit: str, lane_name: str, repo_name: str) -> Path:
    return lane_proto.lane_dir(workspace_root, owner_unit, lane_name) / "repos" / repo_name


def _materialize_lane_repos(workspace_root: Path, owner_unit: str, lane_name: str) -> None:
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
        )


def _run_lane_stage(workspace_root: Path, owner_unit: str, lane_name: str, stage: str) -> None:
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
        )


def _exit(code: int) -> None:
    if code != 0:
        raise typer.Exit(code=code)


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
    _materialize_lane_repos(workspace_root, owner_unit, lane_name)


@lane_app.command("enter")
def lane_enter(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    actor: str = typer.Option(..., help="Actor label, e.g. agent:atlas"),
    notify_channel: bool = typer.Option(False, "--notify-channel"),
    recall: bool = typer.Option(False, "--recall"),
) -> None:
    """Enter a lane and optionally emit channel/recall-compatible events."""
    workspace_root = workspace_root.resolve()
    _run_lane_stage(workspace_root, owner_unit, lane_name, "on_enter")
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        lane_name=lane_name,
        actor=actor,
        notify_channel=notify_channel,
        recall=recall,
    )
    _exit(lane_proto.enter_lane(ns))


@lane_app.command("exit")
def lane_exit(
    workspace_root: Path,
    owner_unit: str,
    actor: str = typer.Option(..., help="Actor label, e.g. human:layne"),
    notify_channel: bool = typer.Option(False, "--notify-channel"),
    recall: bool = typer.Option(False, "--recall"),
) -> None:
    """Exit the current lane for a unit."""
    workspace_root = workspace_root.resolve()
    current_doc = lane_proto.load_current_lane_doc(workspace_root, owner_unit)
    lane_name = current_doc["current"]["lane_name"]
    lane_doc = lane_proto.load_lane_doc(workspace_root, owner_unit, lane_name)
    for repo_name in lane_doc.get("repos", []):
        repo_root = _lane_repo_root(workspace_root, owner_unit, lane_name, repo_name)
        if repo_root.exists():
            stash_if_dirty(repo_root, f"gr2 exit {owner_unit}/{lane_name}")
    _run_lane_stage(workspace_root, owner_unit, lane_name, "on_exit")
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        actor=actor,
        notify_channel=notify_channel,
        recall=recall,
    )
    _exit(lane_proto.exit_lane(ns))


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


if __name__ == "__main__":
    app()
