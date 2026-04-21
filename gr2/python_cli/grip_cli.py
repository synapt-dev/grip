"""CLI commands for grip object model and config overlay.

Separate module so tests can import without pulling in all of app.py's
dependencies (gr2.prototypes, lane_workspace_prototype, etc.).
"""
from __future__ import annotations

import json
from pathlib import Path

import typer

from . import config as config_mod
from . import grip as grip_mod

grip_app = typer.Typer(help="Grip object model: workspace snapshots and history")
config_cli_app = typer.Typer(help="Config base+overlay management")


def _resolve_repos(workspace: Path, repos_csv: str) -> dict[str, Path]:
    result: dict[str, Path] = {}
    for name in repos_csv.split(","):
        name = name.strip()
        if not name:
            continue
        result[name] = workspace / name
    return result


# ---------------------------------------------------------------------------
# gr grip
# ---------------------------------------------------------------------------


@grip_app.command("init")
def grip_init_cmd(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Initialize the .grip/ git repo at workspace root."""
    workspace_root = workspace_root.resolve()
    try:
        grip_mod.grip_init(workspace_root)
    except grip_mod.GripInitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps({"status": "initialized", "path": str(workspace_root / ".grip")}))
    else:
        typer.echo(f"Initialized .grip/ at {workspace_root}")


@grip_app.command("snapshot")
def grip_snapshot_cmd(
    workspace_root: Path,
    repos: str = typer.Option(..., "--repos", help="Comma-separated repo names"),
    message: str = typer.Option("", "--message", "-m", help="Snapshot message"),
    changeset_type: str = typer.Option("", "--type", help="Changeset type (e.g. ceremony, feature)"),
    sprint: str = typer.Option("", "--sprint", help="Sprint number"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Config overlay directory to include in snapshot"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Snapshot current workspace into a grip commit."""
    workspace_root = workspace_root.resolve()
    repo_map = _resolve_repos(workspace_root, repos)
    overlay = Path(overlay_dir).resolve() if overlay_dir else None
    try:
        sha = grip_mod.grip_snapshot(
            workspace_root,
            repo_map,
            changeset_type=changeset_type,
            sprint=sprint,
            message=message,
            overlay_dir=overlay,
        )
    except grip_mod.GripInitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps({"sha": sha, "repos": sorted(repo_map.keys())}))
    else:
        typer.echo(f"grip snapshot {sha[:12]} ({len(repo_map)} repos)")


@grip_app.command("log")
def grip_log_cmd(
    workspace_root: Path,
    max_count: int = typer.Option(10, "--max-count", "-n", help="Max entries to show"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show grip commit history."""
    workspace_root = workspace_root.resolve()
    try:
        entries = grip_mod.grip_log(workspace_root, max_count=max_count)
    except (grip_mod.GripInitError, grip_mod.GripCorruptError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps({
            "entries": [
                {"sha": e.sha, "message": e.message, "repos": e.repos, "timestamp": e.timestamp}
                for e in entries
            ]
        }))
    else:
        if not entries:
            typer.echo("No grip commits yet.")
            return
        for e in entries:
            typer.echo(f"{e.sha[:12]}  {e.message}  [{', '.join(e.repos)}]")


@grip_app.command("diff")
def grip_diff_cmd(
    workspace_root: Path,
    ref_a: str = typer.Argument(..., help="First grip commit ref"),
    ref_b: str = typer.Argument(..., help="Second grip commit ref"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show changes between two grip commits."""
    workspace_root = workspace_root.resolve()
    try:
        result = grip_mod.grip_diff(workspace_root, ref_a, ref_b)
    except (grip_mod.GripInitError, grip_mod.GripCorruptError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps({
            "changed": result.changed,
            "added": result.added,
            "removed": result.removed,
        }))
    else:
        if not result.changed and not result.added and not result.removed:
            typer.echo("No changes.")
            return
        for name, info in result.changed.items():
            typer.echo(f"  ~ {name}: {info['old_commit'][:12]} -> {info['new_commit'][:12]}")
        for name in result.added:
            typer.echo(f"  + {name}")
        for name in result.removed:
            typer.echo(f"  - {name}")


@grip_app.command("checkout")
def grip_checkout_cmd(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Grip commit ref to restore"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Restore workspace repo HEADs from a grip commit."""
    workspace_root = workspace_root.resolve()
    try:
        result = grip_mod.grip_checkout(workspace_root, ref)
    except (grip_mod.GripInitError, grip_mod.GripCorruptError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps({"repos": result}))
    else:
        for name, sha in result.items():
            typer.echo(f"  {name} -> {sha[:12]}")


# ---------------------------------------------------------------------------
# gr config
# ---------------------------------------------------------------------------


@config_cli_app.command("apply")
def config_apply_cmd(
    base_path: Path = typer.Argument(..., help="Path to base TOML config file"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Overlay directory (default: sibling overlay/)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Materialize TOML base into JSON runtime overlay."""
    base_path = base_path.resolve()
    overlay = Path(overlay_dir).resolve() if overlay_dir else base_path.parent / "overlay"
    result = config_mod.config_apply(base_path, overlay)
    if json_output:
        typer.echo(json.dumps(result))
    else:
        typer.echo(f"Applied {base_path.name} -> {overlay / (base_path.stem + '.json')}")


@config_cli_app.command("show")
def config_show_cmd(
    base_path: Path = typer.Argument(..., help="Path to base TOML config file"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Overlay directory"),
    key: str = typer.Option("", "--key", "-k", help="Dotted key path (e.g. agents.opus.model)"),
    strict: bool = typer.Option(False, "--strict", help="Fail if overlay is stale"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show merged config (overlay-first, base-fallback)."""
    base_path = base_path.resolve()
    overlay = Path(overlay_dir).resolve() if overlay_dir else base_path.parent / "overlay"
    try:
        result = config_mod.config_show(
            base_path, overlay,
            key=key or None,
            strict=strict,
        )
    except config_mod.BaseStaleError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    if json_output:
        if key:
            typer.echo(json.dumps({"key": key, "value": result}))
        else:
            typer.echo(json.dumps(result))
    else:
        typer.echo(json.dumps(result, indent=2))


@config_cli_app.command("restore")
def config_restore_cmd(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Grip commit ref to restore config from"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Overlay directory to restore into"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Restore config overlay from a grip commit snapshot."""
    workspace_root = workspace_root.resolve()
    overlay = Path(overlay_dir).resolve() if overlay_dir else workspace_root / "config" / "overlay"
    result = config_mod.config_restore(workspace_root, ref, overlay)
    if json_output:
        typer.echo(json.dumps({"restored": result}))
    else:
        typer.echo(f"Restored {len(result)} file(s) from {ref[:12]}")
