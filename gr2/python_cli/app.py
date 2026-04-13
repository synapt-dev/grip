from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Optional

import typer

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
