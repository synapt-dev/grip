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

grip_app = typer.Typer(
    help="Workspace snapshots and history. "
    "Stores multi-repo state as content-addressable objects in .grip/.",
)
config_cli_app = typer.Typer(
    help="Config base+overlay management. "
    "TOML base (cold, reviewed) + JSON overlay (hot, agent-writable).",
)

verbose_option = typer.Option(
    False, "--verbose", "-v",
    help="Print debug details (paths, SHAs, timings) to stderr.",
)


def _debug(verbose: bool, msg: str) -> None:
    if verbose:
        typer.echo(f"[debug] {msg}", err=True)


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
    verbose: bool = verbose_option,
) -> None:
    """Initialize the .grip/ snapshot repo at a workspace root. Idempotent."""
    workspace_root = workspace_root.resolve()
    _debug(verbose, f"workspace_root={workspace_root}")
    try:
        grip_mod.grip_init(workspace_root)
    except grip_mod.GripInitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    _debug(verbose, f"grip_dir={workspace_root / '.grip'}")
    if json_output:
        typer.echo(json.dumps({"status": "initialized", "path": str(workspace_root / ".grip")}))
    else:
        typer.echo(f"Initialized .grip/ at {workspace_root}")


@grip_app.command("snapshot")
def grip_snapshot_cmd(
    workspace_root: Path,
    repos: str = typer.Option(..., "--repos", help="Comma-separated repo names to include"),
    message: str = typer.Option("", "--message", "-m", help="Snapshot message (shown in 'grip log')"),
    changeset_type: str = typer.Option("", "--type", help="Changeset type tag (e.g. ceremony, feature)"),
    sprint: str = typer.Option("", "--sprint", help="Sprint number tag"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Config overlay directory to include in snapshot"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    verbose: bool = verbose_option,
) -> None:
    """Record current repo HEADs and config overlay as a grip commit."""
    workspace_root = workspace_root.resolve()
    repo_map = _resolve_repos(workspace_root, repos)
    overlay = Path(overlay_dir).resolve() if overlay_dir else None
    _debug(verbose, f"workspace_root={workspace_root}")
    _debug(verbose, f"repos={sorted(repo_map.keys())}")
    if overlay:
        _debug(verbose, f"overlay_dir={overlay}")
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
    _debug(verbose, f"commit_sha={sha}")
    if json_output:
        typer.echo(json.dumps({"sha": sha, "repos": sorted(repo_map.keys())}))
    else:
        typer.echo(f"grip snapshot {sha[:12]} ({len(repo_map)} repos)")


@grip_app.command("log")
def grip_log_cmd(
    workspace_root: Path,
    max_count: int = typer.Option(10, "--max-count", "-n", help="Maximum number of entries to display"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    verbose: bool = verbose_option,
) -> None:
    """List grip snapshot history, most recent first.

    Each entry shows the short SHA, message, and repos captured.
    Use the full SHA from --json output as a ref for 'grip diff' or 'grip checkout'.
    """
    workspace_root = workspace_root.resolve()
    _debug(verbose, f"workspace_root={workspace_root}")
    try:
        entries = grip_mod.grip_log(workspace_root, max_count=max_count)
    except (grip_mod.GripInitError, grip_mod.GripCorruptError) as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    _debug(verbose, f"entries_found={len(entries)}")
    if json_output:
        typer.echo(json.dumps({
            "entries": [
                {"sha": e.sha, "message": e.message, "repos": e.repos, "timestamp": e.timestamp}
                for e in entries
            ]
        }))
    else:
        if not entries:
            typer.echo("No grip commits yet. Run 'grip snapshot' to create one.")
            return
        for e in entries:
            typer.echo(f"{e.sha[:12]}  {e.message}  [{', '.join(e.repos)}]")


@grip_app.command("diff")
def grip_diff_cmd(
    workspace_root: Path,
    ref_a: str = typer.Argument(..., help="First grip commit SHA (from 'grip log')"),
    ref_b: str = typer.Argument(..., help="Second grip commit SHA (from 'grip log')"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    verbose: bool = verbose_option,
) -> None:
    """Compare two grip snapshots and show which repos changed.

    Pass full or abbreviated SHAs from 'grip log --json'.
    """
    workspace_root = workspace_root.resolve()
    _debug(verbose, f"ref_a={ref_a} ref_b={ref_b}")
    try:
        result = grip_mod.grip_diff(workspace_root, ref_a, ref_b)
    except grip_mod.GripInitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    except grip_mod.GripCorruptError as exc:
        _emit_ref_not_found_hint(str(exc))
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps({
            "changed": result.changed,
            "added": result.added,
            "removed": result.removed,
        }))
    else:
        if not result.changed and not result.added and not result.removed:
            typer.echo("No changes between the two snapshots.")
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
    ref: str = typer.Argument(..., help="Grip commit SHA to restore (from 'grip log')"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    verbose: bool = verbose_option,
) -> None:
    """Restore workspace repo HEADs to the state recorded in a grip snapshot.

    Each repo is checked out to the exact commit captured at snapshot time.
    Repos already at the correct commit are left untouched.
    """
    workspace_root = workspace_root.resolve()
    _debug(verbose, f"ref={ref}")
    try:
        result = grip_mod.grip_checkout(workspace_root, ref)
    except grip_mod.GripInitError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)
    except grip_mod.GripCorruptError as exc:
        _emit_ref_not_found_hint(str(exc))
        raise typer.Exit(code=1)
    _debug(verbose, f"restored_repos={list(result.keys())}")
    if json_output:
        typer.echo(json.dumps({"repos": result}))
    else:
        for name, sha in result.items():
            typer.echo(f"  {name} -> {sha[:12]}")


def _emit_ref_not_found_hint(error_msg: str) -> None:
    typer.echo(error_msg, err=True)
    typer.echo(
        "Hint: run 'grip log' to list valid grip commit SHAs.",
        err=True,
    )


# ---------------------------------------------------------------------------
# gr config
# ---------------------------------------------------------------------------


@config_cli_app.command("apply")
def config_apply_cmd(
    base_path: Path = typer.Argument(..., help="Path to base TOML config file"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Overlay directory (default: sibling overlay/)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    verbose: bool = verbose_option,
) -> None:
    """Materialize a TOML base into a JSON runtime overlay.

    Creates the overlay directory if it does not exist. If an overlay
    already exists, new base keys are merged while preserving overlay edits.
    """
    base_path = base_path.resolve()
    overlay = Path(overlay_dir).resolve() if overlay_dir else base_path.parent / "overlay"
    _debug(verbose, f"base_path={base_path}")
    _debug(verbose, f"overlay_dir={overlay}")
    try:
        result = config_mod.config_apply(base_path, overlay)
    except config_mod.OverlayCorruptError as exc:
        _emit_corrupt_overlay_hint(str(exc))
        raise typer.Exit(code=1)
    _debug(verbose, f"keys={sorted(result.keys())}")
    if json_output:
        typer.echo(json.dumps(result))
    else:
        typer.echo(f"Applied {base_path.name} -> {overlay / (base_path.stem + '.json')}")


@config_cli_app.command("show")
def config_show_cmd(
    base_path: Path = typer.Argument(..., help="Path to base TOML config file"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Overlay directory"),
    key: str = typer.Option("", "--key", "-k", help="Dotted key path (e.g. agents.opus.model)"),
    strict: bool = typer.Option(False, "--strict", help="Fail if overlay _base_sha is stale"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    verbose: bool = verbose_option,
) -> None:
    """Show merged config: overlay values win, base fills gaps.

    Without --key, prints the full merged document. With --key, resolves
    a dotted path (e.g. agents.opus.model) and prints that value only.
    """
    base_path = base_path.resolve()
    overlay = Path(overlay_dir).resolve() if overlay_dir else base_path.parent / "overlay"
    _debug(verbose, f"base_path={base_path}")
    _debug(verbose, f"overlay_dir={overlay}")
    if key:
        _debug(verbose, f"key={key}")
    try:
        result = config_mod.config_show(
            base_path, overlay,
            key=key or None,
            strict=strict,
        )
    except config_mod.BaseStaleError as exc:
        typer.echo(str(exc), err=True)
        typer.echo(
            "Hint: run 'config apply' to re-materialize the overlay from the current base.",
            err=True,
        )
        raise typer.Exit(code=1)
    except config_mod.OverlayCorruptError as exc:
        _emit_corrupt_overlay_hint(str(exc))
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
    ref: str = typer.Argument(..., help="Grip commit SHA to restore config from (from 'grip log')"),
    overlay_dir: str = typer.Option("", "--overlay-dir", help="Overlay directory to restore into"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    verbose: bool = verbose_option,
) -> None:
    """Restore config overlay files from a prior grip snapshot.

    This is an exact restore: overlay JSON files not present in the target
    snapshot are deleted. Non-JSON files in the overlay directory are preserved.
    """
    workspace_root = workspace_root.resolve()
    overlay = Path(overlay_dir).resolve() if overlay_dir else workspace_root / "config" / "overlay"
    _debug(verbose, f"ref={ref}")
    _debug(verbose, f"overlay_dir={overlay}")
    try:
        result = config_mod.config_restore(workspace_root, ref, overlay)
    except config_mod.OverlayCorruptError as exc:
        _emit_corrupt_overlay_hint(str(exc))
        raise typer.Exit(code=1)
    _debug(verbose, f"restored_files={sorted(result.keys())}")
    if json_output:
        typer.echo(json.dumps({"restored": result}))
    else:
        typer.echo(f"Restored {len(result)} file(s) from {ref[:12]}")


def _emit_corrupt_overlay_hint(error_msg: str) -> None:
    typer.echo(error_msg, err=True)
    typer.echo(
        "The corrupt file has been quarantined with a .corrupt extension. "
        "Run 'config apply' to rebuild the overlay from the TOML base.",
        err=True,
    )
