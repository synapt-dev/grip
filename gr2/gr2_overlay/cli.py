"""CLI plumbing for `gr overlay` subcommands.

Wires typer commands to gr2_overlay module functions.
"""

from __future__ import annotations

import json
from pathlib import Path

import typer

from gr2_overlay import introspection, workspace_spec
from gr2_overlay.activate import activate_overlay, deactivate_overlay
from gr2_overlay.types import OverlayRef

overlay_app = typer.Typer(
    help="Config overlay capture, composition, and materialization (Tier A).",
)


def _default_overlay_store(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "overlay-store.git"


def _emit(result: str | dict | list, json_output: bool) -> None:
    if json_output:
        if isinstance(result, str):
            typer.echo(result)
        else:
            typer.echo(json.dumps(result, indent=2))
    else:
        typer.echo(result)


@overlay_app.command("activate")
def overlay_activate_cmd(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Overlay ref: <author>/<name>"),
    source_kind: str = typer.Option("local", help="Overlay source kind (local, git, registry)"),
    source_value: str | None = typer.Option(None, help="Overlay source value (path or URL)"),
    signer: str | None = typer.Option(None, help="Expected overlay signer"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Activate an overlay, eagerly materializing its files into the workspace."""
    overlay_ref = OverlayRef.parse(ref)
    overlay_store = _default_overlay_store(workspace_root)
    result = activate_overlay(
        workspace_root=workspace_root,
        overlay_store=overlay_store,
        overlay_ref=overlay_ref,
        overlay_source_kind=source_kind,
        overlay_source_value=source_value,
        overlay_signer=signer,
    )
    if json_output:
        typer.echo(json.dumps({"status": result.status, "completed": result.completed}))
    else:
        typer.echo(f"Activated {ref}: {result.status}")


@overlay_app.command("deactivate")
def overlay_deactivate_cmd(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Overlay ref: <author>/<name>"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Deactivate an overlay and remove its materialized files."""
    overlay_ref = OverlayRef.parse(ref)
    result = deactivate_overlay(
        workspace_root=workspace_root,
        overlay_ref=overlay_ref,
    )
    if json_output:
        typer.echo(json.dumps({"completed": result.completed}))
    else:
        typer.echo(f"Deactivated {ref}")


@overlay_app.command("diff")
def overlay_diff_cmd(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Overlay ref: <author>/<name>"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show the diff an overlay would produce if activated."""
    overlay_ref = OverlayRef.parse(ref)
    overlay_store = _default_overlay_store(workspace_root)
    file_list = introspection._read_overlay_file_list(overlay_store, overlay_ref)
    if json_output:
        typer.echo(json.dumps({"ref": ref, "files": file_list}))
    else:
        typer.echo(f"Files that would change for {ref}:")
        for f in file_list:
            typer.echo(f"  {f}")


@overlay_app.command("list")
def overlay_list_cmd(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List all declared overlays from workspace spec."""
    entries = workspace_spec.load_overlay_spec(workspace_root)
    if json_output:
        overlay_data = [
            {
                "name": e.name,
                "path": e.path,
                "applies_to": e.applies_to,
                "priority": e.priority,
            }
            for e in entries
        ]
        typer.echo(json.dumps({"overlays": overlay_data}, indent=2))
    else:
        if not entries:
            typer.echo("No overlays declared.")
        else:
            typer.echo("Declared overlays:")
            for e in entries:
                typer.echo(f"  {e.name} (priority={e.priority}, path={e.path})")


@overlay_app.command("stack")
def overlay_stack_cmd(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show the current overlay activation stack with priority ordering."""
    overlay_store = _default_overlay_store(workspace_root)
    result = introspection.overlay_stack(workspace_root, overlay_store, json_output)
    _emit(result, json_output)


@overlay_app.command("status")
def overlay_status_cmd(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show overlay materialization status: which overlays are active, stale, or conflicting."""
    overlay_store = _default_overlay_store(workspace_root)
    result = introspection.overlay_status(workspace_root, overlay_store, json_output)
    _emit(result, json_output)


@overlay_app.command("trace")
def overlay_trace_cmd(
    workspace_root: Path,
    file_path: str = typer.Argument(..., help="File path to trace overlay provenance for"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Trace which overlay(s) contributed to a specific file's current state."""
    overlay_store = _default_overlay_store(workspace_root)
    result = introspection.overlay_trace(workspace_root, overlay_store, file_path, json_output)
    _emit(result, json_output)


@overlay_app.command("why")
def overlay_why_cmd(
    workspace_root: Path,
    key: str = typer.Argument(..., help="Config key or file path to explain"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Explain why a config key has its current value (which overlay, base, or merge)."""
    overlay_store = _default_overlay_store(workspace_root)
    result = introspection.overlay_why(workspace_root, overlay_store, key, json_output)
    _emit(result, json_output)


@overlay_app.command("impact")
def overlay_impact_cmd(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Overlay ref: <author>/<name>"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show what files and keys would change if this overlay were activated or deactivated."""
    overlay_ref = OverlayRef.parse(ref)
    overlay_store = _default_overlay_store(workspace_root)
    result = introspection.overlay_impact(overlay_store, overlay_ref, json_output)
    _emit(result, json_output)
