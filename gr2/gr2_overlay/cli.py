"""CLI plumbing for `gr overlay` subcommands.

All subcommands are stubs for M1 scaffolding. Implementation comes in Stories 2-12.
"""

from __future__ import annotations

from pathlib import Path

import typer

overlay_app = typer.Typer(
    help="Config overlay capture, composition, and materialization (Tier A).",
)


@overlay_app.command("activate")
def overlay_activate(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Overlay ref: <author>/<name>"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Activate an overlay, eagerly materializing its files into the workspace."""
    typer.echo("not implemented", err=True)
    raise typer.Exit(code=1)


@overlay_app.command("deactivate")
def overlay_deactivate(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Overlay ref: <author>/<name>"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Deactivate an overlay and remove its materialized files."""
    typer.echo("not implemented", err=True)
    raise typer.Exit(code=1)


@overlay_app.command("diff")
def overlay_diff(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Overlay ref: <author>/<name>"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show the diff an overlay would produce if activated."""
    typer.echo("not implemented", err=True)
    raise typer.Exit(code=1)


@overlay_app.command("list")
def overlay_list(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """List all known overlays (local and remote refs)."""
    typer.echo("not implemented", err=True)
    raise typer.Exit(code=1)


@overlay_app.command("stack")
def overlay_stack(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show the current overlay activation stack with priority ordering."""
    typer.echo("not implemented", err=True)
    raise typer.Exit(code=1)


@overlay_app.command("status")
def overlay_status(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show overlay materialization status: which overlays are active, stale, or conflicting."""
    typer.echo("not implemented", err=True)
    raise typer.Exit(code=1)


@overlay_app.command("trace")
def overlay_trace(
    workspace_root: Path,
    file_path: str = typer.Argument(..., help="File path to trace overlay provenance for"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Trace which overlay(s) contributed to a specific file's current state."""
    typer.echo("not implemented", err=True)
    raise typer.Exit(code=1)


@overlay_app.command("why")
def overlay_why(
    workspace_root: Path,
    key: str = typer.Argument(..., help="Config key (dotted path) to explain"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Explain why a config key has its current value (which overlay, base, or merge)."""
    typer.echo("not implemented", err=True)
    raise typer.Exit(code=1)


@overlay_app.command("impact")
def overlay_impact(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Overlay ref: <author>/<name>"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show what files and keys would change if this overlay were activated or deactivated."""
    typer.echo("not implemented", err=True)
    raise typer.Exit(code=1)
