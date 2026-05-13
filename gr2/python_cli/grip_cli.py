"""CLI commands for grip object model and config overlay.

Separate module so tests can import without pulling in all of app.py's
dependencies (gr2.prototypes, lane_workspace_prototype, etc.).
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import typer

from . import config as config_mod
from . import grip as grip_mod
from .gitops import git, repo_dirty

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


def _discover_repos(workspace: Path) -> dict[str, Path]:
    """Auto-discover repos from workspace_spec.toml."""
    spec_path = workspace / ".grip" / "workspace_spec.toml"
    if not spec_path.exists():
        typer.echo(
            f"No .grip/workspace_spec.toml at {workspace}. Use --repos or create a workspace spec."
        )
        raise typer.Exit(code=1)
    with spec_path.open("rb") as fh:
        spec = tomllib.load(fh)
    result: dict[str, Path] = {}
    for repo in spec.get("repos", []):
        name = repo.get("name", "")
        path = repo.get("path", name)
        if name:
            result[name] = workspace / path
    return result


def _validate_grip_dir(workspace: Path) -> None:
    """Check .grip/ directory exists."""
    grip_dir = workspace / ".grip"
    if not grip_dir.exists():
        typer.echo(f"No .grip/ directory at {workspace}. Run workspace init first.")
        raise typer.Exit(code=1)


def _check_dirty_repos(repos: dict[str, Path]) -> list[str]:
    """Return list of dirty repo names."""
    dirty = []
    for name, path in sorted(repos.items()):
        if path.is_dir() and repo_dirty(path):
            dirty.append(name)
    return dirty


def _repo_head_state(repo_path: Path) -> dict[str, object]:
    """Get head state for a single repo."""
    head_proc = git(repo_path, "rev-parse", "HEAD")
    if head_proc.returncode != 0:
        return {"head": None, "is_empty": True, "head_state": "empty"}

    head_sha = head_proc.stdout.strip()
    branch = git(repo_path, "branch", "--show-current")
    branch_name = branch.stdout.strip() if branch.returncode == 0 else ""

    if branch_name:
        return {
            "head": head_sha,
            "is_empty": False,
            "head_state": "attached",
            "branch": branch_name,
        }
    return {"head": head_sha, "is_empty": False, "head_state": "detached"}


def _read_snapshot_index(workspace: Path) -> list[dict[str, object]]:
    index_path = workspace / ".grip" / "snapshots" / "index.json"
    if not index_path.exists():
        return []
    return json.loads(index_path.read_text())


def _write_snapshot_index(workspace: Path, index: list[dict[str, object]]) -> None:
    snapshots_dir = workspace / ".grip" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    index_path = snapshots_dir / "index.json"
    index_path.write_text(json.dumps(index, indent=2) + "\n")


def _find_snapshot_by_id(
    index: list[dict[str, object]],
    snapshot_id: str,
) -> dict[str, object] | None:
    for entry in index:
        if entry.get("id") == snapshot_id:
            return entry
    return None


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
    repos: str = typer.Option(
        "",
        "--repos",
        help="Comma-separated repo names (auto from spec if omitted)",
    ),
    message: str = typer.Option(
        "",
        "--message",
        "-m",
        help="Snapshot message",
    ),
    changeset_type: str = typer.Option(
        "",
        "--type",
        help="Changeset type (e.g. ceremony, feature)",
    ),
    sprint: str = typer.Option("", "--sprint", help="Sprint number"),
    overlay_dir: str = typer.Option(
        "",
        "--overlay-dir",
        help="Config overlay directory",
    ),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Snapshot current workspace into a grip commit."""
    workspace_root = workspace_root.resolve()

    _validate_grip_dir(workspace_root)

    if repos:
        repo_map = _resolve_repos(workspace_root, repos)
    else:
        repo_map = _discover_repos(workspace_root)

    if not repo_map:
        typer.echo("No repos found in workspace spec.")
        raise typer.Exit(code=1)

    dirty = _check_dirty_repos(repo_map)
    if dirty:
        typer.echo(f"Dirty repos detected: {', '.join(dirty)}. Commit or stash changes first.")
        raise typer.Exit(code=1)

    repo_states: dict[str, dict[str, object]] = {}
    for name, path in sorted(repo_map.items()):
        repo_states[name] = _repo_head_state(path)

    grip_mod.grip_init(workspace_root)

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

    snapshot_id = sha

    index = _read_snapshot_index(workspace_root)
    entry: dict[str, object] = {
        "id": snapshot_id,
        "sha": sha,
        "message": message or "grip snapshot",
        "repos": sorted(repo_map.keys()),
        "repo_states": repo_states,
    }
    if changeset_type:
        entry["type"] = changeset_type
    if sprint:
        entry["sprint"] = sprint
    index.append(entry)
    _write_snapshot_index(workspace_root, index)

    if json_output:
        typer.echo(json.dumps({"sha": sha, "id": snapshot_id, "repos": sorted(repo_map.keys())}))
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

    _validate_grip_dir(workspace_root)

    index = _read_snapshot_index(workspace_root)

    if json_output:
        display = list(reversed(index[-max_count:])) if index else []
        typer.echo(json.dumps({"entries": display}))
        return

    if not index:
        typer.echo("No grip snapshots yet.")
        return

    display = list(reversed(index[-max_count:]))
    for entry in display:
        sid = entry.get("id", "?")
        msg = entry.get("message", "")
        repos = entry.get("repos", [])
        typer.echo(f"{msg}  [{', '.join(repos)}]  ({sid[:12]})")


@grip_app.command("diff")
def grip_diff_cmd(
    workspace_root: Path,
    ref_a: str = typer.Argument(..., help="First snapshot id"),
    ref_b: str = typer.Argument(..., help="Second snapshot id"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show changes between two grip snapshots."""
    workspace_root = workspace_root.resolve()

    _validate_grip_dir(workspace_root)

    index = _read_snapshot_index(workspace_root)
    snap_a = _find_snapshot_by_id(index, ref_a)
    snap_b = _find_snapshot_by_id(index, ref_b)

    if snap_a is None:
        typer.echo(f"Snapshot not found: missing id '{ref_a}'")
        raise typer.Exit(code=1)
    if snap_b is None:
        typer.echo(f"Snapshot not found: missing id '{ref_b}'")
        raise typer.Exit(code=1)

    states_a = snap_a.get("repo_states", {})
    states_b = snap_b.get("repo_states", {})
    all_repos = set(states_a.keys()) | set(states_b.keys())

    changed: dict[str, dict[str, str]] = {}
    added: list[str] = []
    removed: list[str] = []

    for name in sorted(all_repos):
        if name in states_a and name not in states_b:
            removed.append(name)
        elif name not in states_a and name in states_b:
            added.append(name)
        else:
            head_a = states_a[name].get("head")
            head_b = states_b[name].get("head")
            if head_a != head_b:
                changed[name] = {"old": str(head_a), "new": str(head_b)}

    if json_output:
        typer.echo(json.dumps({"changed": changed, "added": added, "removed": removed}))
    else:
        if not changed and not added and not removed:
            typer.echo("No changes.")
            return
        for name, info in changed.items():
            old = info["old"][:12] if info["old"] else "None"
            new = info["new"][:12] if info["new"] else "None"
            typer.echo(f"  changed {name}: {old} -> {new}")
        for name in added:
            typer.echo(f"  + {name}")
        for name in removed:
            typer.echo(f"  - {name}")


@grip_app.command("checkout")
def grip_checkout_cmd(
    workspace_root: Path,
    ref: str = typer.Argument(..., help="Snapshot id to restore"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Restore workspace repo HEADs from a grip snapshot."""
    workspace_root = workspace_root.resolve()

    _validate_grip_dir(workspace_root)

    index = _read_snapshot_index(workspace_root)
    snapshot = _find_snapshot_by_id(index, ref)
    if snapshot is None:
        typer.echo(f"Snapshot not found: {ref}")
        raise typer.Exit(code=1)

    repo_states = snapshot.get("repo_states", {})

    existing_repos: dict[str, Path] = {}
    for name in repo_states:
        repo_path = workspace_root / name
        if repo_path.is_dir():
            existing_repos[name] = repo_path

    dirty = _check_dirty_repos(existing_repos)
    if dirty:
        typer.echo(f"Dirty repos detected: {', '.join(dirty)}. Commit or stash changes first.")
        raise typer.Exit(code=1)

    result: dict[str, str] = {}
    for name, state in sorted(repo_states.items()):
        head_sha = state.get("head")
        if not head_sha:
            continue
        repo_path = workspace_root / name
        if not repo_path.is_dir():
            continue
        git(repo_path, "checkout", head_sha)
        result[name] = head_sha

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
    overlay_dir: str = typer.Option(
        "",
        "--overlay-dir",
        help="Overlay directory (default: sibling overlay/)",
    ),
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
            base_path,
            overlay,
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
