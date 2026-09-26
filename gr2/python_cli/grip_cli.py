"""CLI commands for grip object model and config overlay.

Separate module so tests can import without pulling in all of app.py's
dependencies (gr2.prototypes, lane_workspace_prototype, etc.).
"""

from __future__ import annotations

import json
import subprocess
import tomllib
from pathlib import Path
from urllib.parse import urlsplit

import typer

from . import config as config_mod
from . import gitops
from . import grip as grip_mod
from .gitops import git, repo_dirty
from .workspace_guidance import missing_gr2_workspace_guidance

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


def _member_working_root(workspace_root: Path, name: str, declared_path: Path) -> Path:
    """The path a store verb reads and writes for one member.

    The state helper answers what the declared path holds, because asking the
    path directly let git answer for the ENCLOSING root: on a workspace
    adopted from a superproject the declared path is the EMPTY placeholder,
    git answered every question there for the ENCLOSING root, `store snapshot`
    recorded the ROOT's HEAD as every member's head (rc 0), and the dirty
    check refused with the ROOT's untracked files as the members' dirt. A
    missing member keeps the old shape (skipped by the dirty check, recorded
    empty) -- a missing member is nothing to read, not a conflict. A present
    path that is a repo root is used as-is; an empty placeholder falls to the
    unit's materialized copy of that member, which is the clone `workspace
    materialize` placed at its pin; a present path that is neither refuses
    with the verb that fixes it.
    """
    if not declared_path.exists():
        return declared_path
    state = gitops.repo_path_state(declared_path)
    if state == "repo_root":
        return declared_path
    if state == "empty_placeholder":
        try:
            spec = tomllib.loads((workspace_root / ".grip" / "workspace_spec.toml").read_text())
        except (OSError, tomllib.TOMLDecodeError):
            raise SystemExit(
                f"run gr2 workspace materialize first: {declared_path} is an empty placeholder "
                "and the workspace spec cannot be read to find its materialized copy"
            )
        for unit in spec.get("units", []):
            if name not in [str(item) for item in unit.get("repos", [])]:
                continue
            candidate = (workspace_root / str(unit.get("path", "")) / name).resolve()
            if gitops.repo_path_state(candidate) == "repo_root":
                return candidate
        raise SystemExit(
            f"run gr2 workspace materialize first: {name} is not materialized in any unit "
            f"(the declared path {declared_path} is an empty placeholder)"
        )
    raise SystemExit(f"run gr2 workspace materialize first: {declared_path} is not a repository")


def _member_map(workspace_root: Path, repos_csv: str) -> dict[str, Path]:
    """The store verbs' member map, resolved to working roots.

    The declared paths are what the spec says; what a store verb READS is the
    working root of each member, which on an adopted superproject is the
    unit's materialized copy, not the placeholder at the root.
    """
    if repos_csv:
        declared = _resolve_repos(workspace_root, repos_csv)
    else:
        declared = _discover_repos(workspace_root)
    return {name: _member_working_root(workspace_root, name, path) for name, path in declared.items()}


def _validate_grip_dir(workspace: Path) -> None:
    """Check .grip/ directory exists."""
    grip_dir = workspace / ".grip"
    if not grip_dir.exists():
        typer.echo(
            f"No .grip/ directory at {workspace}. "
            f"{missing_gr2_workspace_guidance(workspace, 'Run `gr2 workspace init .` first.')}"
        )
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
    if not repo_path.exists():
        # a missing path is nothing to read, not a conflict: record it empty
        # without invoking git, which would raise before the rc check below
        # could answer (the declared path can hold no placeholder and no repo)
        return {"head": None, "is_empty": True, "head_state": "empty"}
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


class AmbiguousSnapshotId(Exception):
    """A prefix matching more than one snapshot. Never resolved by guessing."""

    def __init__(self, prefix: str, matches: list[str]) -> None:
        self.prefix = prefix
        self.matches = matches
        super().__init__(prefix)


def _find_snapshot_by_id(
    index: list[dict[str, object]],
    snapshot_id: str,
) -> dict[str, object] | None:
    """Resolve a snapshot by full id or by unique prefix.

    The store holds 40-character ids and ``grip log`` prints the first 12, so
    an exact-match-only lookup made the ONLY id a user can obtain from the CLI
    the one that does not work: ``grip checkout`` and ``grip diff`` answered
    ``Snapshot not found`` for an id ``grip log`` had just printed, while the
    working id existed solely inside ``.grip/snapshots/index.json``.

    Prefix resolution follows git's rule, including the part that matters most:
    an ambiguous prefix RAISES rather than picking the first match.  Silently
    resolving to one of several would make ``checkout`` land on an arbitrary
    snapshot, which is worse than the refusal this replaces.
    """
    if not snapshot_id:
        return None
    for entry in index:
        if entry.get("id") == snapshot_id:
            return entry
    matches = [e for e in index if str(e.get("id", "")).startswith(snapshot_id)]
    if len(matches) > 1:
        raise AmbiguousSnapshotId(snapshot_id, [str(e.get("id", "")) for e in matches])
    return matches[0] if matches else None


# ---------------------------------------------------------------------------
# gr grip
# ---------------------------------------------------------------------------


def _resolve_snapshot_or_exit(
    index: list[dict[str, object]],
    snapshot_id: str,
) -> dict[str, object] | None:
    """Resolve for a CLI verb, turning ambiguity into a refusal.

    ``_find_snapshot_by_id`` RAISES on an ambiguous prefix, which is right for
    a library: picking one of several arbitrarily is the thing we are trying
    not to do.  But every verb called it bare, so the raise reached the user as
    a traceback -- the exact class this whole range exists to remove,
    reintroduced by the fix for it.

    The verbs go through here rather than each carrying its own ``except``,
    for the same reason the current-lane fix added a required accessor: there
    were three call sites, and a per-site handler makes the fourth one's
    omission silent.  ``None`` (not found) still returns, because each verb
    words that message differently.
    """
    try:
        return _find_snapshot_by_id(index, snapshot_id)
    except AmbiguousSnapshotId as exc:
        typer.echo(
            f"Ambiguous snapshot id: {exc.prefix!r} matches {len(exc.matches)} snapshots:"
        )
        for match in exc.matches:
            typer.echo(f"  {match}")
        typer.echo("Use more characters to disambiguate.")
        raise typer.Exit(code=1)


def _store_git(root: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(["git", "-C", str(root), *args], text=True, capture_output=True, check=False)
    if check and result.returncode:
        raise RuntimeError(result.stderr.strip() or "git command failed")
    return result


class NativeStoreRefusal(RuntimeError):
    """A native-store refusal with a stable CLI status."""

    def __init__(self, message: str, code: int) -> None:
        super().__init__(message)
        self.code = code


def _native_members(root: Path) -> list[dict[str, str]]:
    path = root / "grip.toml"
    if path.exists():
        with path.open("rb") as handle:
            data = tomllib.load(handle)
    else:
        # A root remote need not carry member objects.  Read the canonical spec
        # directly so materialize can populate those members before checkout.
        spec = _store_git(root, "show", "HEAD:grip.toml").stdout
        data = tomllib.loads(spec)
    members = data.get("member", [])
    if not isinstance(members, list) or not members:
        raise RuntimeError("grip.toml has no members")
    return [dict(member) for member in members]


def _write_native_members(root: Path, members: list[dict[str, str]]) -> None:
    lines = ["version = 0", ""]
    for member in members:
        lines.extend(["[[member]]", f"name = {json.dumps(member['name'])}", f"path = {json.dumps(member['path'])}", f"remote = {json.dumps(member['remote'])}", f"pin = {json.dumps(member['pin'])}", ""])
    (root / "grip.toml").write_text("\n".join(lines))


def _url_has_credentials(url: str) -> bool:
    """Refuse URL userinfo except an SSH login, never by printing the URL."""
    head = url.split("://", 1)[0]
    address = url.split("::", 1)[1] if "::" in head else url
    parsed = urlsplit(address)
    if parsed.password is not None:
        return True
    return parsed.username is not None and parsed.scheme.lower() not in {"ssh", "git+ssh", "ssh+git"}


def _credential_refusal(name: str) -> NativeStoreRefusal:
    return NativeStoreRefusal(
        f"{name} origin contains credentials; remove URL userinfo and use a credential helper", 4
    )


def _native_store_init(root: Path) -> None:
    if (root / ".git").exists():
        raise RuntimeError(f"store already initialized at {root}")
    if not ((root / ".gitgrip").is_dir() or (root / ".grip" / "workspace_spec.toml").is_file()):
        raise RuntimeError(
            f"{root} is not a gripspace root: expected .gitgrip/ or .grip/workspace_spec.toml"
        )
    members: list[dict[str, str]] = []
    for path in sorted(root.iterdir()):
        if not path.is_dir() or path.name.startswith("."):
            continue
        top = _store_git(path, "rev-parse", "--show-toplevel", check=False)
        if top.returncode or Path(top.stdout.strip()).resolve() != path.resolve():
            continue
        remote = _store_git(path, "remote", "get-url", "origin", check=False)
        if remote.returncode:
            raise RuntimeError(f"{path.name} has no origin remote")
        url = remote.stdout.strip()
        if _url_has_credentials(url):
            raise _credential_refusal(path.name)
        members.append({"name": path.name, "path": path.name, "remote": url, "pin": _store_git(path, "rev-parse", "HEAD").stdout.strip()})
    if not members:
        raise RuntimeError("no sibling git repositories found to store")
    _store_git(root, "init")
    _write_native_members(root, members)


def _native_store_commit(root: Path, message: str) -> None:
    members = _native_members(root)
    changed: list[dict[str, str]] = []
    for member in members:
        if _url_has_credentials(member["remote"]):
            raise _credential_refusal(member["name"])
        path = root / member["path"]
        head = _store_git(path, "rev-parse", "HEAD").stdout.strip()
        if _store_git(path, "merge-base", "--is-ancestor", head, "origin/main", check=False).returncode:
            raise NativeStoreRefusal(
                f"{member['name']} pin {head} is not on origin/main; push it first", 3
            )
        changed.append({**member, "pin": head})
    _write_native_members(root, changed)
    _store_git(root, "add", "grip.toml")
    for member in changed:
        _store_git(root, "update-index", "--add", "--cacheinfo", f"160000,{member['pin']},{member['path']}")
    _store_git(root, "-c", "user.name=gr2", "-c", "user.email=gr2@example.invalid", "commit", "-m", message)


def _native_store_materialize(root: Path) -> None:
    for member in _native_members(root):
        path = root / member["path"]
        if not path.exists():
            result = subprocess.run(["git", "clone", member["remote"], str(path)], text=True, capture_output=True, check=False)
            if result.returncode:
                raise RuntimeError(result.stderr.strip())
        _store_git(path, "checkout", "--detach", member["pin"])


@grip_app.command("init")
def grip_init_cmd(
    workspace_root: Path | None = typer.Argument(None),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Initialize a native store in cwd, or the legacy .grip repo at a path."""
    if workspace_root is None:
        try:
            _native_store_init(Path.cwd())
        except NativeStoreRefusal as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=exc.code)
        except RuntimeError as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=1)
        typer.echo(f"Initialized git-native store at {Path.cwd()}")
        return
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


@grip_app.command("commit")
def grip_commit_cmd(message: str = typer.Option(..., "--message", "-m")) -> None:
    """Record origin-covered member pins in the root git tree."""
    try:
        _native_store_commit(Path.cwd(), message)
    except NativeStoreRefusal as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=exc.code)
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


@grip_app.command("materialize")
def grip_materialize_cmd() -> None:
    """Materialize each canonical pin from its declared origin."""
    try:
        _native_store_materialize(Path.cwd())
    except RuntimeError as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=1)


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

    # The member map resolves to working roots: on an adopted superproject
    # the declared path is the placeholder and the member state lives in the
    # unit's materialized copy, so the dirty check and the head record read
    # the member, never the root.
    repo_map = _member_map(workspace_root, repos)

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
    snap_a = _resolve_snapshot_or_exit(index, ref_a)
    snap_b = _resolve_snapshot_or_exit(index, ref_b)

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
    snapshot = _resolve_snapshot_or_exit(index, ref)
    if snapshot is None:
        typer.echo(f"Snapshot not found: {ref}")
        raise typer.Exit(code=1)

    repo_states = snapshot.get("repo_states", {})

    existing_repos: dict[str, Path] = {}
    for name in repo_states:
        # the same resolution as the map: the member's working root, not the
        # bare name under the root (which on an adopted superproject is the
        # placeholder git would answer for the root itself)
        repo_path = _member_working_root(workspace_root, name, workspace_root / name)
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
        # the same resolution as the map: the member's working root, not the
        # bare name under the root
        repo_path = _member_working_root(workspace_root, name, workspace_root / name)
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
    if not base_path.is_file():
        typer.echo(f"base config file not found: {base_path}", err=True)
        raise typer.Exit(code=1)
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
    if not base_path.is_file():
        typer.echo(f"base config file not found: {base_path}", err=True)
        raise typer.Exit(code=1)
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
