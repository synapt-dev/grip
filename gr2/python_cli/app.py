from __future__ import annotations

import contextlib
import io
import json
import os
import tomllib
from pathlib import Path
from types import SimpleNamespace
from typing import List, Optional

import typer
from gr2.prototypes import lane_workspace_prototype as lane_proto
from gr2.prototypes import repo_maintenance_prototype as repo_proto

from . import add as add_ops
from . import branch as branch_ops
from . import commit as commit_ops
from . import execops, failures, grip, migration, spec_apply, syncops
from . import pr as pr_ops
from . import prune as prune_ops
from . import target as target_ops
from . import project_review
from . import push as push_ops
from .events import EventType, emit_after_outcome
from .gitops import (
    branch_exists,
    checkout_branch,
    ensure_lane_checkout,
    fetch_ref,
    git,
    is_git_repo,
    refresh_existing_branch,
    remote_origin_url,
    repo_dirty,
    stash_if_dirty,
)
from .grip_cli import config_cli_app, grip_app
from .hooks import (
    HookContext,
    HookRuntimeError,
    apply_file_projections,
    load_repo_hooks,
    run_lifecycle_stage,
)
from .merge_verification import MergeVerificationTarget
from .platform import PRRef, get_platform_adapter

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
target_app = typer.Typer(help="Stored PR target (settings.target) that prune and future verbs default to")

app.add_typer(repo_app, name="repo")
app.add_typer(lane_app, name="lane")
lane_app.add_typer(lease_app, name="lease")
app.add_typer(review_app, name="review")
app.add_typer(pr_app, name="pr")
app.add_typer(workspace_app, name="workspace")
app.add_typer(spec_app, name="spec")
app.add_typer(exec_app, name="exec")
app.add_typer(sync_app, name="sync")
app.add_typer(target_app, name="target")
# The snapshot store over .grip/.git. Its verb is `store` (init/snapshot/log/diff/
# checkout); `grip` stays as a hidden alias for one release so existing callers keep
# working. Both names resolve to the same grip_app callbacks.
app.add_typer(grip_app, name="store")
app.add_typer(grip_app, name="grip", hidden=True)
app.add_typer(config_cli_app, name="config")


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
    fork_base: dict[str, dict[str, str]] = {}

    # The fork base of every repo whose checkout exists is persisted in the `finally`
    # below, INDEPENDENT of the hooks' outcome. A projection whose source is missing
    # raises HookRuntimeError (exit 1) from `apply_file_projections`; recording only
    # after the loop meant a blocked hook left a half lane -- lane.toml and the
    # checkout present, but no fork_base -- and `review create-project` then refused
    # with "no recorded fork base", the same symptom as a lane that never recorded one
    # from an unrelated cause. Each repo's base is collected
    # BEFORE its own hooks run, so the finally captures every materialized repo,
    # including the one whose hook just blocked, while the exit 1 and its JSON report
    # still propagate.
    try:
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
                workspace_root=workspace_root,
            )
            # Record the fork base = the materialization point (the branch the lane forked
            # from and the sha it started at), so `review create-project` can pin base..head
            # Collected here, before this repo's hooks run below.
            head = git(target_repo_root, "rev-parse", "HEAD")
            head_sha = head.stdout.strip() if head.returncode == 0 else ""
            if len(head_sha) == 40:
                fork_base[repo_name] = {"branch": branch_map[repo_name], "sha": head_sha}
            hooks = load_repo_hooks(target_repo_root)
            if not hooks:
                continue
            ctx = HookContext(
                workspace_root=workspace_root,
                unit_root=lane_proto.lane_state_root(workspace_root) / owner_unit,
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
    finally:
        # Persist the fork base for every repo materialized above, even if a hook
        # raised on the way out. Without this a materialized lane can have no
        # fork_base and `review create-project` refuses.
        if fork_base:
            lane_proto.record_fork_base(workspace_root, owner_unit, lane_name, fork_base)


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
            unit_root=lane_proto.lane_state_root(workspace_root) / owner_unit,
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
        unit_root=workspace_root,
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
    current_doc = lane_proto.require_current_lane(workspace_root, owner_unit)
    return str(current_doc["lane_name"])


def _find_pr_group(workspace_root: Path, owner_unit: str, lane_name: str) -> tuple[Path, dict[str, object]]:
    root = workspace_root / ".grip" / "pr_groups"
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


def _merge_verification_targets(
    workspace_root: Path,
) -> dict[str, MergeVerificationTarget]:
    """Bind host slugs to explicit local DAGs and source URLs before merging."""

    workspace_spec = lane_proto.load_workspace_spec(workspace_root)
    targets: dict[str, MergeVerificationTarget] = {}
    for repo_spec_value in workspace_spec.get("repos", []):
        repo_spec = dict(repo_spec_value)
        repo_name = str(repo_spec.get("name", ""))
        remote = str(repo_spec.get("url", "")).strip()
        if not remote:
            raise SystemExit(f"repo has no source URL for merge verification: {repo_name}")
        host_repo = _repo_slug_from_url(remote, repo_name)
        if host_repo in targets:
            raise SystemExit(f"duplicate host repo in merge verification targets: {host_repo}")
        repo_root = (workspace_root / str(repo_spec.get("path", ""))).resolve()
        if not repo_root.is_dir() or not is_git_repo(repo_root):
            raise SystemExit(f"local merge-verification DAG is unavailable: {repo_root}")
        targets[host_repo] = MergeVerificationTarget(repo_root=repo_root, remote=remote)
    return targets


def _configured_merge_method(workspace_root: Path) -> str | None:
    settings = lane_proto.load_workspace_spec(workspace_root).get("settings", {})
    if not isinstance(settings, dict):
        raise SystemExit("workspace spec [settings] must be a table")
    value = settings.get("merge_method")
    if value is None:
        return None
    if not isinstance(value, str):
        raise SystemExit("workspace setting merge_method must be a string")
    return value


def _find_workspace_root(start: Path) -> Path | None:
    """Nearest ancestor of `start` (inclusive) holding a gr2 workspace spec, else None.

    prune runs single-repo (cwd/--repo-path) but the stored PR target lives in the
    WORKSPACE spec, so we walk up to find it. No spec found -- a bare repo, or gr2
    used outside a gripspace -- returns None, and the stored-target step is skipped
    so prune keeps working standalone.
    """
    for candidate in (start, *start.parents):
        if (candidate / ".grip" / "workspace_spec.toml").is_file():
            return candidate
    return None


def _configured_target(workspace_root: Path) -> str | None:
    """The gripspace's stored PR target (`[settings].target`), read never written."""
    settings = lane_proto.load_workspace_spec(workspace_root).get("settings", {})
    if not isinstance(settings, dict):
        raise SystemExit("workspace spec [settings] must be a table")
    value = settings.get("target")
    if value is None:
        return None
    if not isinstance(value, str):
        raise SystemExit("workspace setting target must be a string")
    return value


def _toml_basic_string(value: str) -> str:
    """Render one string through the TOML basic-string grammar.

    WorkspaceSpec values are operator-controlled at several call sites. Keeping
    the escaping here makes every value written by ``_write_workspace_spec``
    parseable, rather than relying on each caller to reject a partial set of
    characters.
    """
    if not isinstance(value, str):
        raise TypeError(f"TOML basic string requires str, got {type(value).__name__}")

    escapes = {
        '"': '\\"',
        "\\": "\\\\",
        "\b": "\\b",
        "\t": "\\t",
        "\n": "\\n",
        "\f": "\\f",
        "\r": "\\r",
    }
    rendered: list[str] = ['"']
    for character in value:
        code_point = ord(character)
        if character in escapes:
            rendered.append(escapes[character])
        elif code_point < 0x20 or code_point == 0x7F:
            rendered.append(f"\\u{code_point:04X}")
        elif 0xD800 <= code_point <= 0xDFFF:
            raise ValueError("TOML basic strings cannot contain surrogate code points")
        else:
            rendered.append(character)
    rendered.append('"')
    return "".join(rendered)


def _write_workspace_spec(
    workspace_root: Path,
    repos: list[dict[str, str]],
    default_unit: str,
    *,
    workspace_name: str | None = None,
) -> Path:
    spec_path = _workspace_spec_path(workspace_root)
    emitted_workspace_name = workspace_root.name if workspace_name is None else workspace_name
    lines = [
        f"workspace_name = {_toml_basic_string(emitted_workspace_name)}",
        "",
    ]
    for repo in repos:
        lines.extend(
            [
                "[[repos]]",
                f"name = {_toml_basic_string(repo['name'])}",
                f"path = {_toml_basic_string(repo['path'])}",
                f"url = {_toml_basic_string(repo['url'])}",
                "",
            ]
        )
    lines.extend(
        [
            "[[units]]",
            f"name = {_toml_basic_string(default_unit)}",
            f"path = {_toml_basic_string(f'agents/{default_unit}/home')}",
            "repos = [" + ", ".join(_toml_basic_string(repo["name"]) for repo in repos) + "]",
            "",
        ]
    )
    spec_path.parent.mkdir(parents=True, exist_ok=True)
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


def _declared_workspace_topology(
    workspace_root: Path,
) -> tuple[str | None, list[dict[str, str]]]:
    """Lower neutral ``workspace.toml`` repository declarations for gr2.

    This deliberately reads only the fields the existing WorkspaceSpec writer
    accepts. Declarations can carry fields such as ``default_ref`` as well,
    but those do not belong in the WorkspaceSpec emission path.
    """
    topology_path = workspace_root / "workspace.toml"
    if not topology_path.is_file():
        raise SystemExit(f"workspace topology not found: {topology_path}")
    try:
        with topology_path.open("rb") as topology_file:
            document = tomllib.load(topology_file)
    except tomllib.TOMLDecodeError as exc:
        raise SystemExit(f"workspace.toml at {topology_path} is not valid TOML: {exc}") from exc

    workspace_name = document.get("workspace_name")
    if workspace_name is not None and not isinstance(workspace_name, str):
        raise SystemExit("workspace.toml workspace_name must be a string when declared")

    raw_repos = document.get("repos", [])
    if not isinstance(raw_repos, list):
        raise SystemExit("workspace.toml repos must be an array of tables")
    if not raw_repos:
        raise SystemExit("workspace.toml declares no [[repos]] entries")

    repos: list[dict[str, str]] = []
    for index, raw_repo in enumerate(raw_repos):
        if not isinstance(raw_repo, dict):
            raise SystemExit(f"workspace.toml repos[{index}] must be a table")
        key = str(raw_repo.get("key", "<missing key>"))
        for field in ("key", "path", "url"):
            value = raw_repo.get(field)
            if not value:
                raise SystemExit(
                    f"workspace.toml repos[{index}] ({key!r}) is missing {field!r}"
                )
            if not isinstance(value, str):
                raise SystemExit(
                    f"workspace.toml repos[{index}] ({key!r}) must declare {field!r} as a string"
                )
        repos.append(
            {
                "name": str(raw_repo["key"]),
                "path": str(raw_repo["path"]),
                "url": str(raw_repo["url"]),
            }
        )
    return workspace_name, repos


def _exit(code: int) -> None:
    if code != 0:
        raise typer.Exit(code=code)


def _consume_lane_transition(outcome: lane_proto.LaneTransitionOutcome | int) -> lane_proto.LaneTransitionOutcome | None:
    """Render the state writer's one outcome instead of inferring one in the CLI."""
    if isinstance(outcome, lane_proto.LaneTransitionOutcome):
        typer.echo(json.dumps(outcome.as_dict(), indent=2))
        _exit(outcome.exit_code)
        return outcome
    _exit(outcome)
    return None


@sync_app.command("status")
def sync_status(
    workspace_root: Path,
    dirty_mode: str = typer.Option("block", "--dirty", help="Dirty-state handling: block (stop, the default), stash, or discard"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Inspect workspace-wide sync readiness without mutating any repo state."""
    workspace_root = workspace_root.resolve()
    plan = syncops.build_sync_plan(workspace_root, dirty_mode=dirty_mode, probe_remotes=True)
    if json_output:
        typer.echo(json.dumps(plan.as_dict(), indent=2))
        return
    typer.echo(syncops.render_sync_plan(plan))


@sync_app.command("run")
def sync_run(
    workspace_root: Path,
    dirty_mode: str = typer.Option("block", "--dirty", help="Dirty-state handling: block (stop, the default), stash, or discard"),
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


@workspace_app.command("init-from-topology")
def workspace_init_from_topology(
    workspace_root: Path,
    default_unit: str = typer.Option("default", help="Default owner unit for declared repos"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Create WorkspaceSpec from neutral ``workspace.toml`` repo declarations."""
    workspace_root = workspace_root.resolve()
    workspace_name, repos = _declared_workspace_topology(workspace_root)
    spec_path = _write_workspace_spec(
        workspace_root,
        repos,
        default_unit,
        workspace_name=workspace_name,
    )
    payload = {
        "workspace_root": str(workspace_root),
        "spec_path": str(spec_path),
        "repo_count": len(repos),
        "repos": repos,
        "default_unit": default_unit,
        "source": "workspace.toml",
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        lines = [
            "WorkspaceInitFromTopology",
            f"workspace_root = {workspace_root}",
            f"spec_path = {spec_path}",
            f"default_unit = {default_unit}",
            f"repo_count = {len(repos)}",
            "source = workspace.toml",
            "REPOS",
        ]
        lines.extend(f"- {repo['name']}\t{repo['path']}\t{repo['url']}" for repo in repos)
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


@workspace_app.command("status")
def workspace_status_cmd(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show workspace state: gr1-only, gr2-only, coexistence, or none."""
    workspace_root = workspace_root.resolve()
    payload = migration.workspace_status(workspace_root)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(migration.render_status(payload))


@workspace_app.command("convert-clone")
def workspace_convert_clone_cmd(
    path: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Convert a linked git worktree at PATH into an own clone, in place.

    Refuses a dirty tree, a symlinked .git, or a path that is not a linked
    worktree at all -- gr does not support worktree-backed repos.
    """
    try:
        receipt = repo_proto.convert_worktree_to_clone(path.resolve())
    except repo_proto.ConvertCloneError as exc:
        typer.echo(f"convert-clone refused: {exc}", err=True)
        raise typer.Exit(code=1) from exc

    if json_output:
        typer.echo(json.dumps(receipt, indent=2))
    else:
        typer.echo(f"Converted {receipt['path']} from a linked worktree to an own clone.")
        typer.echo(f"  branch: {receipt['branch']}")
        typer.echo(f"  head:   {receipt['head_sha']}")
        typer.echo(f"  was linked to: {receipt['old_common_dir']}")


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
    apply: bool = typer.Option(False, "--apply", help="After migration, validate and apply the spec in one step"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Convert an existing gr1 (.gitgrip) workspace into parallel gr2 (.grip) layout."""
    workspace_root = workspace_root.resolve()
    payload = migration.migrate_gr1_workspace(workspace_root, force=force)
    if apply:
        issues = spec_apply.validate_spec(workspace_root)
        errors = [i for i in issues if i.level == "error"]
        if errors:
            payload["apply_status"] = "validation_failed"
            payload["validation_errors"] = [i.as_dict() for i in errors]
        else:
            apply_result = spec_apply.apply_plan(workspace_root, yes=True)
            payload["apply_status"] = "applied"
            payload["apply_result"] = apply_result
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(migration.render_migration(payload))
        if apply and payload.get("apply_status") == "applied":
            typer.echo("\nWorkspace materialized successfully.")
        elif apply and payload.get("apply_status") == "validation_failed":
            typer.echo(f"\nSpec validation failed: {len(payload.get('validation_errors', []))} error(s).")


@workspace_app.command("migrate-lane-state")
def workspace_migrate_lane_state(
    workspace_root: Path,
    receipt: Path = typer.Option(..., "--receipt", help="New receipt path naming every lane tree the migration moved"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Move legacy agents/<unit>/lanes/<lane>/ trees to .grip/state/lanes/<unit>/<lane>/ once, with a receipt."""
    workspace_root = workspace_root.resolve()
    payload = migration.migrate_lane_state(workspace_root, receipt_path=receipt)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        count = payload["count"]
        typer.echo(f"Migrated {count} lane tree(s) to .grip/state/lanes/; receipt at {receipt}")
        for row in payload["moved"]:
            typer.echo(f"  {row['source']} -> {row['dest']} ({row['files']} file(s))")


@workspace_app.command("bootstrap-gr1")
def workspace_bootstrap_gr1(
    workspace_root: Path,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
    regenerate: bool = typer.Option(False, "--regenerate", help="Atomically regenerate an existing generated spec"),
    expected_spec_sha256: Optional[str] = typer.Option(None, "--expected-spec-sha256"),
    receipt: Optional[Path] = typer.Option(None, "--receipt"),
    rollback_receipt: Optional[Path] = typer.Option(None, "--rollback-receipt"),
    expected_current_spec_sha256: Optional[str] = typer.Option(None, "--expected-current-spec-sha256"),
) -> None:
    """Compile the canonical gr1 manifest and initialize the gr2 grip store."""
    workspace_root = workspace_root.resolve()
    if rollback_receipt is not None:
        if regenerate or expected_spec_sha256 is not None:
            raise typer.BadParameter("--rollback-receipt is mutually exclusive with regeneration inputs")
        if expected_current_spec_sha256 is None or receipt is None:
            raise typer.BadParameter("--rollback-receipt requires --expected-current-spec-sha256 and --receipt")
        payload = migration.rollback_gr1_workspace(
            workspace_root,
            rollback_receipt_path=rollback_receipt,
            expected_current_spec_sha256=expected_current_spec_sha256,
            receipt_path=receipt,
        )
    elif regenerate:
        if expected_spec_sha256 is None or receipt is None:
            raise typer.BadParameter("--regenerate requires --expected-spec-sha256 and --receipt")
        payload = migration.regenerate_gr1_workspace(workspace_root, expected_spec_sha256=expected_spec_sha256, receipt_path=receipt)
    else:
        payload = migration.bootstrap_gr1_workspace(workspace_root)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        # Render each payload by its OWN keys. bootstrap, regenerate and rollback
        # return different schemas (rollback has no manifest_path, regenerate has
        # no repo_count); a fixed key list raised KeyError after a successful
        # mutation, exiting 1 on a completed rollback. Print scalars in order;
        # json-encode nested values so nothing is dropped.
        typer.echo(str(payload.get("schema", "Gr1Bootstrap")))
        for key, value in payload.items():
            if key == "schema":
                continue
            if isinstance(value, (dict, list)):
                value = json.dumps(value)
            typer.echo(f"{key} = {value}")


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


@app.command("branch")
def branch_cmd(
    name: str,
    base: str | None = typer.Option(
        None,
        "--base",
        "--from",
        "--start-point",
        help="Create (or reset) the branch off this ref instead of current HEAD",
    ),
    repo_path: Path | None = typer.Option(
        None,
        "--repo-path",
        help="Repo to operate on (defaults to cwd; gr2 verbs are single-repo, not gripspace-wide)",
    ),
) -> None:
    """Create or switch to a branch, natively -- no gr1 dependency."""
    target = (repo_path or Path.cwd()).resolve()
    try:
        branch_ops.create_branch(target, name, base=base)
    except branch_ops.BranchError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"Switched to branch '{name}'")


@app.command("add")
def add_cmd(
    paths: list[str] = typer.Argument(..., help="Paths or pathspecs to stage"),
    repo_path: Path | None = typer.Option(
        None,
        "--repo-path",
        help="Repo to operate on (defaults to cwd; gr2 verbs are single-repo)",
    ),
) -> None:
    """Stage paths in one repository, including tracked deletions."""
    target = (repo_path or Path.cwd()).resolve()
    try:
        result = add_ops.stage_files(target, paths)
    except add_ops.AddError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    if result.staged_files:
        typer.echo(f"Staged {len(result.staged_files)} path(s): {', '.join(result.staged_files)}")
    else:
        typer.echo("No changes staged for the requested paths")


@app.command("commit")
def commit_cmd(
    message: str = typer.Option(..., "--message", "-m", help="Commit message"),
    amend: bool = typer.Option(False, "--amend", help="Amend the current commit"),
    repo_path: Path | None = typer.Option(
        None,
        "--repo-path",
        help="Repo to operate on (defaults to cwd; single-repo)",
    ),
    workspace_root: Path | None = typer.Option(
        None,
        "--workspace-root",
        help="Lane-aware: commit each repo of a lane in this workspace (requires --owner-unit)",
    ),
    owner_unit: str | None = typer.Option(
        None,
        "--owner-unit",
        help="Lane-aware: the unit whose lane to commit across",
    ),
    lane_name: str | None = typer.Option(
        None,
        "--lane",
        help="Lane-aware: lane name (defaults to the unit's current lane)",
    ),
) -> None:
    """Create or amend a commit from the staged index.

    Single-repo by default (cwd or --repo-path). With --workspace-root and
    --owner-unit, commits each repo of a materialized lane under one message
    (bound lanes stay single-repo).
    """
    lane_mode = workspace_root is not None and owner_unit is not None
    if lane_mode and repo_path is not None:
        typer.echo("Error: --repo-path is single-repo; do not combine it with --workspace-root/--owner-unit", err=True)
        raise typer.Exit(code=1)
    if lane_mode:
        try:
            report = commit_ops.commit_lane(
                workspace_root, owner_unit, message, lane_name=lane_name, amend=amend
            )
        except commit_ops.CommitError as exc:
            typer.echo(f"Error: {exc}", err=True)
            raise typer.Exit(code=1) from exc
        for row in report.results:
            if row.status == "committed":
                typer.echo(f"{row.repo}: committed {row.commit_sha}")
            elif row.status == "skipped_empty":
                typer.echo(f"{row.repo}: skipped (empty index)")
            else:
                typer.echo(f"{row.repo}: FAILED — {row.error}")
        if report.any_failed:
            raise typer.Exit(code=1)
        return
    target = (repo_path or Path.cwd()).resolve()
    try:
        receipt = commit_ops.create_commit(target, message, amend=amend)
    except commit_ops.CommitError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    action = "Amended" if receipt.amended else "Committed"
    typer.echo(f"{action} {receipt.commit_sha}")


@app.command("push")
def push_cmd(
    remote: str | None = typer.Option(None, "--remote", help="Configured remote to push"),
    set_upstream: bool = typer.Option(False, "--set-upstream", "-u"),
    force_with_lease: bool = typer.Option(
        False,
        "--force-with-lease",
        help="Replace the remote ref only if its observed value still matches",
    ),
    repo_path: Path | None = typer.Option(
        None,
        "--repo-path",
        help="Repo to operate on (defaults to cwd; gr2 verbs are single-repo)",
    ),
) -> None:
    """Push one branch and verify its immutable remote commit."""
    target = (repo_path or Path.cwd()).resolve()
    try:
        receipt = push_ops.push_current_branch(
            target,
            remote=remote,
            set_upstream=set_upstream,
            force_with_lease=force_with_lease,
        )
    except push_ops.PushError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(
        f"Pushed {receipt.branch} to {receipt.remote} at {receipt.remote_sha} "
        "(remote ref verified)"
    )


@app.command("prune")
def prune_cmd(
    target: str | None = typer.Option(
        None,
        "--target",
        help="Ref to measure merged-ness against (default: the gripspace's stored PR target, then origin/dev, then origin/HEAD, then origin/main)",
    ),
    remote: str = typer.Option(
        "origin",
        "--remote",
        help="Remote whose stored-target/dev/HEAD/main resolve the default --target (ignored when --target is given)",
    ),
    execute: bool = typer.Option(
        False,
        "--execute",
        help="Actually delete the merged branches (default: dry-run, prints what it would delete)",
    ),
    repo_path: Path | None = typer.Option(
        None,
        "--repo-path",
        help="Repo to operate on (defaults to cwd; gr2 verbs are single-repo)",
    ),
) -> None:
    r"""List merged local branches and delete them only with --execute.

    Merged is decided by PATCH-ID (git cherry) then a SQUASH tree check, never by
    containment alone -- so squash- and rebase-merged lane branches, which gr1's
    containment prune leaves behind, are caught. The SQUASH check matches a
    branch's whole diff against a single commit already on the target's
    first-parent line (the shape a squash-merge produces). The per-branch reason
    is labelled in the output: \[patch-id] means the branch's commits are already
    present in the target by patch-id; \[squash] means its combined diff matches
    one target commit.

    Never deletes the current branch, the target, or main/dev, and never touches
    a remote ref. There is no verbose or debug flag -- the default dry-run already
    prints every branch it would delete and why. Cost scales as one `git cherry`
    per local branch, plus (only when a branch is not already patch-id-merged) one
    bounded first-parent scan of the target that is built once and shared across
    branches.

    With no --target, the gripspace's stored \[settings].target (the branch this
    workspace merges into -- an epic branch, say) is preferred over origin/dev.
    prune only READS it; `gr target set` is the only writer. A stale stored target
    (its remote ref absent) is skipped and the report's target line names it.
    """
    target_repo = (repo_path or Path.cwd()).resolve()
    stored_target: str | None = None
    if target is None:
        workspace_root = _find_workspace_root(target_repo)
        if workspace_root is not None:
            stored_target = _configured_target(workspace_root)
    try:
        report = prune_ops.prune(
            target_repo, target=target, remote=remote, stored_target=stored_target, execute=execute
        )
    except prune_ops.PruneError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(prune_ops.render_report(report))
    if report.failed:
        raise typer.Exit(code=1)


def _target_workspace_root(repo_path: Path | None) -> tuple[Path, Path]:
    """(start, workspace_root) for a target command, or exit 1 if outside a gripspace."""
    start = (repo_path or Path.cwd()).resolve()
    workspace_root = _find_workspace_root(start)
    if workspace_root is None:
        typer.echo(
            f"Error: no gr2 workspace spec at or above {start}; a stored target needs a "
            f"workspace (run inside a gripspace, or pass --target to prune directly).",
            err=True,
        )
        raise typer.Exit(code=1)
    return start, workspace_root


@target_app.command("set")
def target_set(
    branch: str = typer.Argument(..., help="Branch to store as the PR target, e.g. dev or epic/x"),
    remote: str = typer.Option(
        "origin", "--remote", help="Remote to check <branch> against for the absent-ref warning"
    ),
    repo_path: Path | None = typer.Option(
        None, "--repo-path", help="Repo to check the ref against (defaults to cwd)"
    ),
) -> None:
    """Store <branch> as the gripspace PR target (settings.target).

    Preserves every other spec field (repos, units, an existing merge_method) via
    a full tomllib->tomli_w round-trip; the spec is machine-written, so dropping
    TOML comments in the round-trip is a non-issue. WARNS, never refuses, when
    <remote>/<branch> is absent in the checked clone -- prune tolerates the same
    stale case by skipping the stored step -- so a typo is visible at write time.
    """
    start, workspace_root = _target_workspace_root(repo_path)
    try:
        target_ops.set_target(workspace_root, branch)
    except target_ops.TargetError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(f"stored target: {branch}  ({target_ops.spec_path(workspace_root)})")
    # Absent-ref warning (never fatal): check the clone the caller is standing in.
    if is_git_repo(start):
        ref = branch if branch.startswith(f"{remote}/") else f"{remote}/{branch}"
        if not prune_ops._ref_exists(start, ref):
            typer.echo(
                f"warning: {ref} not found in this clone; stored anyway "
                f"(prune skips the stored target until the ref exists)"
            )


@target_app.command("show")
def target_show(
    repo_path: Path | None = typer.Option(
        None, "--repo-path", help="Repo whose workspace spec to read (defaults to cwd)"
    ),
) -> None:
    """Print the stored settings.target, or 'unset'."""
    _start, workspace_root = _target_workspace_root(repo_path)
    try:
        value = target_ops.show_target(workspace_root)
    except target_ops.TargetError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo(value if value is not None else "unset")


@target_app.command("unset")
def target_unset(
    repo_path: Path | None = typer.Option(
        None, "--repo-path", help="Repo whose workspace spec to edit (defaults to cwd)"
    ),
) -> None:
    """Remove settings.target, preserving every other field."""
    _start, workspace_root = _target_workspace_root(repo_path)
    try:
        removed = target_ops.unset_target(workspace_root)
    except target_ops.TargetError as exc:
        typer.echo(f"Error: {exc}", err=True)
        raise typer.Exit(code=1) from exc
    typer.echo("target unset" if removed else "target was not set")


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
    branch: Optional[str] = typer.Option(None, help="Default branch or repo=branch mappings (required unless --bind; ignored with --bind, where the branch is read from the bound worktree)"),
    lane_type: str = typer.Option("feature", "--type", help="Lane type"),
    source: str = typer.Option("manual", help="Creation source label"),
    command: list[str] = typer.Option(None, "--command", help="Default command for the lane"),
    manual_hooks: bool = typer.Option(False, "--manual-hooks", help="Also run lifecycle hooks marked when=manual during lane materialization"),
    bind: Optional[Path] = typer.Option(None, "--bind", help="Bind the lane to an EXISTING clean, non-detached single-repo worktree instead of materializing a fresh clone (gr2-lane-author-shape ruling). The receipt is stamped lane_kind=bound."),
) -> None:
    """Create a lane and materialize its repos.

    A materialized lane clones each repo and records the fork base (the point it
    forked from its integration branch) — the base a review measures from. Once you
    have committed work in the lane, `review create-project <workspace> <owner>
    <lane>` pins each repo at that fork base .. head and prints the gr:<sha> for
    `review open-project`. A `--bind` lane owns no clone and is single-repo.
    """
    workspace_root = workspace_root.resolve()
    if bind is None and not branch:
        raise typer.BadParameter("--branch is required unless --bind is given")
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        lane_name=lane_name,
        repos=repos,
        branch=branch or "",
        type=lane_type,
        source=source,
        default_commands=command or [],
        bind=str(bind) if bind is not None else None,
    )
    _exit(lane_proto.create_lane(ns))
    # A bound lane owns no clone: skip materialization. The branch_map for the
    # event comes from the lane document create_lane just wrote (derived from the
    # bound worktree), not from the --branch arg, which --bind ignores.
    if bind is None:
        _materialize_lane_repos(workspace_root, owner_unit, lane_name, manual_hooks=manual_hooks)
    repo_list = [r.strip() for r in repos.split(",")]
    # The event payload carries lane_kind (and bound_worktree for a bound lane)
    # so an event-stream consumer can tell a bound lane from a materialized one
    # without a second read of lane.toml.
    lane_kind = "materialized"
    bound_worktree_payload: Optional[str] = None
    if bind is not None:
        doc = tomllib.loads(lane_proto.lane_file(workspace_root, owner_unit, lane_name).read_text())
        branch_map = doc.get("branch_map", {})
        lane_kind = doc.get("lane_kind", "bound")
        bound_worktree_payload = doc.get("bound_worktree")
    else:
        branch_map = {}
        for part in (branch or "").split(","):
            if "=" in part:
                k, v = part.split("=", 1)
                branch_map[k.strip()] = v.strip()
            else:
                for r in repo_list:
                    branch_map[r] = part.strip()
    payload: dict[str, object] = {
        "lane_name": lane_name,
        "lane_type": lane_type,
        "lane_kind": lane_kind,
        "repos": repo_list,
        "branch_map": branch_map,
    }
    if bound_worktree_payload is not None:
        payload["bound_worktree"] = bound_worktree_payload
    emit_after_outcome(
        event_type=EventType.LANE_CREATED,
        workspace_root=workspace_root,
        actor=source,
        owner_unit=owner_unit,
        payload=payload,
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
    outcome = _consume_lane_transition(lane_proto.enter_lane(ns))
    lane_doc = lane_proto.load_lane_doc(workspace_root, owner_unit, lane_name)
    emit_after_outcome(
        event_type=EventType.LANE_ENTERED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=owner_unit,
        payload={
            "lane_name": outcome.current_lane if outcome else lane_name,
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
    current_doc = lane_proto.require_current_lane(workspace_root, owner_unit)
    lane_name = current_doc["lane_name"]
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
    outcome = _consume_lane_transition(lane_proto.exit_lane(ns))
    emit_after_outcome(
        event_type=EventType.LANE_EXITED,
        workspace_root=workspace_root,
        actor=actor,
        owner_unit=owner_unit,
        payload={
            "lane_name": outcome.previous_lane if outcome else lane_name,
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


@lane_app.command("bind")
def lane_bind(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    base: Optional[str] = typer.Option(None, "--base", help="Base SHA the reviewed range is measured from: a full 40-hex commit that is an ancestor of the worktree head. Omit to read the lane's recorded fork base; an explicit --base wins. A lane with no recorded fork base and no --base refuses."),
    allow_local: bool = typer.Option(False, "--allow-local", help="Allow a non-portable local: identity for a worktree with no GitHub origin (test/local use)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Bind a review receipt for a BOUND lane, sourced live from its worktree.

    Re-checks the bound worktree is a clean, non-detached git checkout whose HEAD
    has NOT drifted from the head recorded at create, then writes the
    (repo, base, head, lane_kind=bound) receipt into the worktree's own .git. A
    moved HEAD, a dirty tree, or a base that is not an ancestor commit of head
    refuses. When --base is omitted the base is the lane's recorded fork base (the
    review base is never HEAD^); an explicit --base wins. Only for lanes created
    with ``lane create --bind``.
    """
    base_source = "explicit --base" if base is not None else "lane fork base"
    try:
        record = lane_proto.bind_bound_lane(
            workspace_root.resolve(), owner_unit, lane_name, base=base, allow_local=allow_local
        )
    except SystemExit as exc:
        typer.echo(str(exc), err=True)
        raise typer.Exit(code=2)
    if json_output:
        typer.echo(json.dumps(record.to_dict(), indent=2))
    else:
        typer.echo(
            f"bound review receipt: repo={record.repo} base={record.base} "
            f"head={record.head} (base from {base_source})"
        )


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
    emit_after_outcome(
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
    emit_after_outcome(
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
def lane_lease_show(
    workspace_root: Path,
    owner_unit: str,
    lane_name: str,
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Show active leases for a lane."""
    ns = SimpleNamespace(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        lane_name=lane_name,
        json=json_output,
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


@review_app.command("open")
def review_open(
    workspace_root: Path,
    target: str = typer.Argument(..., help="What to open: a PR number (PR-head lane), a gr:<sha> bind id (reconstruction), or a project-review id"),
    repo: Optional[str] = typer.Argument(None, help="PR-head only: the repository key (with an owner_unit-shaped target)"),
    pr_number: Optional[int] = typer.Argument(None, help="PR-head only: the PR number (legacy positional form)"),
    lane_name: Optional[str] = typer.Option(None, "--lane", help="Override the review lane name"),
    platform: str = typer.Option("github", "--platform", help="Platform adapter name"),
    run: Optional[str] = typer.Option(None, "--run", help="After opening, dispatch this command inside the lane (cwd-contained)"),
    lane_dir: Optional[Path] = typer.Option(None, "--lane-dir", help="gr:<sha> only: directory to reconstruct into"),
    enter: bool = typer.Option(False, "--enter", help="gr:<sha> only: materialize the reconstruction (the only open mode)"),
    repo_key: Optional[str] = typer.Option(None, "--repo", help="gr:<sha> only: repository key to materialize; omit for every bound row"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Open a review lane. ``open`` decides on its POSITIONALS first, then its argument:

    - the PR-head form is ``OWNER_UNIT REPO PR_NUMBER`` (three positionals): the
      owner_unit is any word, so when both REPO and PR_NUMBER are present the target
      is taken as the owner_unit and NOT classified;
    - with only a lone target, ``open`` dispatches on its shape: a ``gr:<sha>`` bind id
      (or bare sha) reconstructs from a review-bind commit (the former ``open-gr``,
      now a hidden alias) -- needs ``--lane-dir`` and ``--enter``; anything else is a
      project-review id (``open-project``, hidden alias). A lone PR number is refused
      because a PR-head lane needs the OWNER_UNIT and REPO positionals too.

    A wrong head REFUSES (never warns); import resolution is printed so the run
    cannot silently import a machine-wide install. ``gr2 review close`` drops the lane.
    """
    from . import review_dispatch

    # Positionals decide first. The PR-head form's first positional is an owner_unit
    # (an arbitrary word that classifies as "project"), so classifying the target
    # before reading REPO/PR_NUMBER would refuse every legacy PR-head open.
    pr_head_positional = repo is not None and pr_number is not None
    if not pr_head_positional:
        kind = review_dispatch.classify_open_target(target)
        if kind == "gr":
            # dispatch to the reconstruction path (open-gr); target is the bind commit
            if lane_dir is None:
                raise typer.BadParameter("--lane-dir is required to open a gr:<sha> reconstruction")
            return review_open_gr(
                workspace_root, target, key=repo_key, lane_dir=lane_dir, enter=enter, json_output=json_output
            )
        if kind == "project":
            raise typer.BadParameter(
                "project-review open is not yet wired into the collapsed `open` (use the hidden `open-project` alias for now)"
            )
        # kind == "pr": a lone PR number cannot open a PR-head lane by itself.
        raise typer.BadParameter("a PR-head open needs OWNER_UNIT REPO PR_NUMBER (target is the owner_unit)")

    # PR-head path: the collapsed target IS the owner_unit; repo and pr_number follow.
    owner_unit = target

    from . import review as review_mod

    workspace_root = workspace_root.resolve()
    resolved_lane = lane_name or f"review-{pr_number}"
    # Portable-component validation before any path is composed from these values
    # (they build the lane directory that `close` later deletes).
    lane_proto.validate_lane_path_component(owner_unit, "owner_unit")
    lane_proto.validate_lane_path_component(repo, "repo")
    lane_proto.validate_lane_path_component(resolved_lane, "lane_name")

    repo_spec = _workspace_repo_spec(workspace_root, repo)
    source_repo_root = (workspace_root / str(repo_spec["path"])).resolve()
    if not source_repo_root.exists():
        raise SystemExit(
            f"shared repo missing for review open: {source_repo_root}\n"
            f"run `gr2 apply {workspace_root} --yes` first"
        )

    # Bind the expected head from the HOST's own advertisement of the PR head,
    # BEFORE fetching — so the fetch that brings the bytes down is compared
    # against an independent authority, not against itself.
    expected_head = review_mod.host_pr_head_oid(source_repo_root, pr_number)

    # Fetch the PR head into the source as pr/<n>; the core compares the fetched
    # ref against expected_head and refuses a wrong/tampered fetch before the seam.
    review_branch = _prepare_review_branch(workspace_root, repo, pr_number, None)

    # Base pin = merge-base(head, base-branch tip). The base branch comes from the
    # PR itself, so the pin is what the PR is actually measured against.
    repo_slug = _repo_slug_from_url(remote_origin_url(source_repo_root) or "", repo)
    base_branch = get_platform_adapter(platform).pr_status(repo_slug, pr_number).ref.base_branch or "main"
    git(source_repo_root, "fetch", "--quiet", "origin", base_branch)
    base_tip = git(source_repo_root, "rev-parse", "FETCH_HEAD").stdout.strip()
    merged = git(source_repo_root, "merge-base", expected_head, base_tip)
    base_sha = merged.stdout.strip() if merged.returncode == 0 else base_tip

    lane_repo_root = _lane_repo_root(workspace_root, owner_unit, resolved_lane, repo)

    record = review_mod.open_review_lane(
        source_repo_root=source_repo_root,
        review_branch=review_branch,
        expected_head_sha=expected_head,
        base_sha=base_sha,
        lane_repo_root=lane_repo_root,
        workspace_root=workspace_root,
        echo=typer.echo,
    )

    if run:
        import shlex

        review_mod.run_in_review_lane(lane_repo_root, shlex.split(run), echo=typer.echo)

    payload = {
        "workspace_root": str(workspace_root),
        "owner_unit": owner_unit,
        "repo": repo,
        "pr_number": pr_number,
        "lane_name": resolved_lane,
        "lane_repo_root": str(lane_repo_root),
        "review_record": record.to_dict(),
    }
    if json_output:
        typer.echo(json.dumps(payload, indent=2))


@review_app.command("close")
def review_close(
    target: Path = typer.Argument(..., help="What to close: a reconstruction lane dir (gr, read from its marker), or the WORKSPACE_ROOT of a PR-head lane"),
    owner_unit: Optional[str] = typer.Argument(None, help="PR-head only: owner unit"),
    repo: Optional[str] = typer.Argument(None, help="PR-head only: repository key"),
    pr_number: Optional[int] = typer.Argument(None, help="PR-head only: PR number"),
    lane_name: Optional[str] = typer.Option(None, "--lane", help="Override the review lane name"),
    json_output: bool = typer.Option(False, "--json", help="gr reconstruction only: machine-readable JSON"),
) -> None:
    """Drop a review lane. ``close`` reads the lane's marker to tell a reconstruction
    lane from a PR lane: a directory carrying open-gr's
    reconstruct marker is reclaimed via the former ``close-gr`` (hidden alias); anything
    else is treated as a PR-head lane (target is the WORKSPACE_ROOT, then owner/repo/pr).
    The base workspace is untouched.
    """
    from . import review_dispatch

    if review_dispatch.classify_close_lane(target) == "reconstruction":
        return review_close_gr(target, json_output=json_output)

    # PR-head path: target is the WORKSPACE_ROOT.
    workspace_root = target
    if owner_unit is None or repo is None or pr_number is None:
        raise typer.BadParameter("a PR-head close needs WORKSPACE_ROOT OWNER_UNIT REPO PR_NUMBER (target is the workspace_root)")

    from . import review as review_mod

    workspace_root = workspace_root.resolve()
    resolved_lane = lane_name or f"review-{pr_number}"
    lane_proto.validate_lane_path_component(owner_unit, "owner_unit")
    lane_proto.validate_lane_path_component(repo, "repo")
    lane_proto.validate_lane_path_component(resolved_lane, "lane_name")
    review_lane_root = lane_proto.lane_dir(workspace_root, owner_unit, resolved_lane)
    lane_repo_root = _lane_repo_root(workspace_root, owner_unit, resolved_lane, repo)
    review_mod.close_review_lane(
        lane_repo_root=lane_repo_root,
        review_lane_root=review_lane_root,
        echo=typer.echo,
    )


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
    # A bound lane's PR is opened FROM the author's worktree, not a materialized
    # clone, so it routes to the push-from-worktree path (verb #4): push the
    # reviewed head, refusing an empty range. It does not use the group/adapter
    # flow, which assumes materialized per-repo clones.
    if lane_doc.get("lane_kind") == "bound":
        try:
            receipt = lane_proto.pr_create_bound_lane(workspace_root, owner_unit, resolved_lane)
        except SystemExit as exc:
            typer.echo(str(exc), err=True)
            raise typer.Exit(code=2)
        pushed = {
            "lane_kind": "bound", "lane": resolved_lane, "remote": receipt.remote,
            "branch": receipt.branch, "head": receipt.remote_sha,
        }
        typer.echo(json.dumps(pushed, indent=2))
        return
    spec = lane_proto.load_workspace_spec(workspace_root)
    adapter = get_platform_adapter(platform)
    branch_map = dict(lane_doc.get("branch_map", {}))
    repos: list[str] = []
    for repo_name in lane_doc.get("repos", []):
        repo_spec = next(repo for repo in spec.get("repos", []) if repo.get("name") == repo_name)
        repos.append(_repo_slug_from_url(str(repo_spec.get("url", "")), repo_name))
    payload = pr_ops.create_pr_group(
        workspace_root=workspace_root,
        owner_unit=owner_unit,
        lane_name=resolved_lane,
        title=resolved_lane,
        base_branch=base_branch,
        head_branch=str(branch_map.get(next(iter(lane_doc.get("repos", [])), resolved_lane), resolved_lane)),
        repos=repos,
        adapter=adapter,
        actor=f"agent:{owner_unit}",
        body=f"gr2 PR group for {owner_unit}/{resolved_lane}",
        draft=draft,
    )
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload, indent=2))


@review_app.command("create-project")
def review_create_project(
    workspace_root: Path,
    owner_unit: str = typer.Argument(..., help="Owner unit whose materialized lane to pin"),
    lane_name: str = typer.Argument(..., help="Materialized lane whose repos to pin at base..head"),
    carry_range: bool = typer.Option(False, "--carry-range", help="Also record each repo's base..head range INSIDE the gr commit, so a pre-push head reconstructs from the commit alone (self-describing). open-project then rebuilds it blobless+sparse without the head on any remote."),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """CREATE a project-review-KIND gr commit from a materialized lane and print the
    ``gr:<sha>`` that `review open-project` consumes.

    This is the producer half of "one gr commit opens one exact multi-repo review":
    each repo of the materialized lane (see `lane create`, which records the fork
    base) is pinned at its RECORDED fork base .. current head (the fork-base ruling,
    never HEAD^), so the review measures exactly what the lane changed. Then open it:

        gr2 review open-project <workspace> gr:<sha> <owner> <review-lane> --enter

    With ``--carry-range`` the commit ALSO carries each repo's range (format-patch
    base..head, captured from the lane), so a reviewer with neither the pre-push head
    nor the lane can reconstruct it from the commit alone; open-project reconstructs
    the head and asserts its TREE equals the pinned head's tree.

    A lane with no recorded fork base, or a repo not materialized, is refused (never
    silently pinned against a guessed base).
    """
    from . import workspace_snapshot as ws_snap
    ws = workspace_root.resolve()
    try:
        pins = project_review.pins_from_lane(ws, owner_unit, lane_name)
        ranges: Optional[dict[str, str]] = None
        committers: Optional[dict[str, str]] = None
        if carry_range:
            ranges = {}
            committers = {}
            for p in pins:
                lane_repo = _lane_repo_root(ws, owner_unit, lane_name, p.key)
                cap = git(lane_repo, "format-patch", f"{p.base}..{p.head}", "--stdout")
                if cap.returncode != 0 or not cap.stdout.strip():
                    raise ValueError(f"cannot capture range for {p.key} from {lane_repo}: {cap.stderr.strip() or 'empty range'}")
                ranges[p.key] = cap.stdout
                # Capture each commit's committer identity+date in apply order (oldest
                # first, the order format-patch/mailsplit use) so reconstruction can
                # re-stamp and reproduce the pinned head SHA, not merely its tree. The
                # lane repo holds the pre-push head; the remote does not.
                cm = git(lane_repo, "log", "--reverse", "--format=%cn%x09%ce%x09%cI", f"{p.base}..{p.head}")
                if cm.returncode != 0:
                    raise ValueError(f"cannot capture committer metadata for {p.key} from {lane_repo}: {cm.stderr.strip()}")
                committers[p.key] = cm.stdout
        spec = project_review.make_spec(ws, pins, ranges=ranges, committers=committers)
    except (ValueError, ws_snap.WorkspaceSnapshotError) as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2)
    if json_output:
        typer.echo(json.dumps({
            "gr_commit": f"gr:{spec.grip_commit}",
            "sha": spec.grip_commit,
            "pins": [
                {"key": p.key, "repo": p.repo, "path": p.path, "base": p.base, "head": p.head}
                for p in spec.pins
            ],
        }, indent=2))
    else:
        typer.echo(f"gr:{spec.grip_commit}")
        for p in spec.pins:
            typer.echo(f"  {p.key}: {p.base[:12]}..{p.head[:12]} {p.repo}")


@review_app.command("open-project", hidden=True)  # hidden alias for one release, dropped at 2.0 GA
def review_open_project(
    workspace_root: Path,
    commit: str = typer.Argument(..., help="The project-review-KIND gr commit (gr:<sha> or bare sha); create one with `review create-project`"),
    owner_unit: str = typer.Argument(..., help="Owner unit whose lane the review enters"),
    lane_name: str = typer.Argument(..., help="Review lane name to materialize into and enter"),
    enter: bool = typer.Option(False, "--enter", help="Materialize the pinned heads and enter the review lane (the only open mode)"),
    sources_json: Optional[Path] = typer.Option(None, "--sources-json", help="Pre-push key -> {source, branch} JSON; clones NORMALLY (full). Prefer --local-source, which keeps the blobless+sparse path"),
    local_source: list[str] = typer.Option(None, "--local-source", help="key=PATH: a local clone that holds a pre-push head. Tops the shared mirror up from it so the blobless+sparse ephemeral path runs without the head on the remote. Repeatable"),
    prior_cwd: Optional[Path] = typer.Option(None, "--prior-cwd", help="Directory to restore on `review exit-gr` (defaults to the current directory)"),
    allow_local: bool = typer.Option(False, "--allow-local", help="Permit filesystem repository identities (fixtures/tests)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """MATERIALIZE a project review from a project-review-KIND gr commit and enter it.

    Resolves each pinned head from its RECORDED REMOTE through the shared bare mirror
    and clones the review lane blobless + sparse (the ephemeral path). For a PRE-PUSH
    head — a gated review head is absent on the remote by design — pass `--local-source
    <key>=<path>` naming a local clone that holds it; the mirror is topped up from that
    clone and the SAME blobless+sparse path runs (no full clone). `--sources-json` is
    the older escape hatch that clones normally (full) and is kept for compatibility.
    Writes a receipt so `review exit-gr` restores the prior lane and cwd and removes the
    disposable ephemeral tree. Contrast `review open-gr`, which RECONSTRUCTS a
    review-BIND commit by `git am` over a carried range and asserts tree equality. A
    commit of the wrong kind is refused, naming the kind it found.
    """
    if not enter:
        raise typer.BadParameter("--enter is required (materialization is the only open mode)")
    if sources_json is not None and local_source:
        raise typer.BadParameter("--sources-json (full clone) and --local-source (sparse) are mutually exclusive")
    from . import open_gr_review
    sha = _strip_gr_prefix(commit)
    sources = None
    if sources_json is not None:
        source_rows = json.loads(sources_json.read_text())
        sources = {key: (Path(row["source"]), row["branch"]) for key, row in source_rows.items()}
    local_sources: Optional[dict[str, Path]] = None
    if local_source:
        local_sources = {}
        for item in local_source:
            if "=" not in item:
                raise typer.BadParameter(f"--local-source must be key=PATH, got {item!r}")
            key, _, path = item.partition("=")
            local_sources[key.strip()] = Path(path.strip())
    outcome = _review_call(
        open_gr_review.open_gr_enter,
        workspace_root.resolve(), owner_unit, lane_name, sha, sources,
        prior_cwd=(prior_cwd.resolve() if prior_cwd is not None else Path.cwd()),
        allow_local=allow_local,
        local_sources=local_sources,
    )
    if json_output:
        typer.echo(json.dumps(project_review.outcome_payload(outcome), indent=2))
    else:
        typer.echo(f"status={outcome.status} lane={lane_name} review_root={outcome.review_root}")
        # A refusal a stranger cannot read is the worst exit point: print each
        # failure's reason on the DEFAULT output, not only under --json.
        for failure in outcome.failures:
            typer.echo(f"  refused[{failure.key}]: {failure.reason}")
    if outcome.status != "opened":
        raise typer.Exit(code=1)


@review_app.command("exit-gr", hidden=True)  # hidden alias for one release, dropped at 2.0 GA
def review_exit_gr(
    workspace_root: Path,
    owner_unit: str = typer.Argument(..., help="Owner unit whose review lane to exit"),
    review_root: Path = typer.Argument(..., help="The review lane root written by `open-project --enter` (holds .grip-open-gr.json)"),
    actor: str = typer.Option("agent:cli", "--actor", help="Actor recorded for the lane exit"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Exit a MATERIALIZED project review opened by `open-project --enter`: pop the
    review lane and restore the prior lane, returning the prior cwd from the receipt.
    This is the project-tier exit; `review close` drops a single-repo `review open` lane.
    """
    from . import open_gr_review
    result = open_gr_review.exit_gr_review(
        workspace_root.resolve(), owner_unit, review_root.resolve(), actor=actor
    )
    if json_output:
        typer.echo(json.dumps({
            "restored_lane": result.restored_lane,
            "restored_cwd": result.restored_cwd,
            "gr_commit": result.gr_commit,
        }, indent=2))
    else:
        typer.echo(f"restored_lane={result.restored_lane} restored_cwd={result.restored_cwd}")


def _strip_gr_prefix(commit: str) -> str:
    """A review commit is addressed as ``gr:<sha>``; accept a bare sha too."""
    return commit[3:] if commit.startswith("gr:") else commit


def _review_call(fn, *args, **kwargs):
    """Run an engine review function, converting a refusal or corruption into a
    clean nonzero exit (code 2) with real error text on stderr — never a
    traceback. The thin CLI is glue over the engine, and glue is exactly where a
    caught exception can turn into a silent success; this makes the loud engine
    failure surface loudly, not swallowed and not as a Python stack trace."""
    try:
        return fn(*args, **kwargs)
    except grip.GripReviewRefused as exc:
        typer.echo(
            f"refused: {exc.refusal}: expected {exc.expected!r}, observed {exc.observed!r}",
            err=True,
        )
        raise typer.Exit(code=2)
    except grip.GripCorruptError as exc:
        typer.echo(f"corrupt: {exc}", err=True)
        raise typer.Exit(code=2)
    except grip.GripInitError as exc:
        # The engine's own message already names what's missing and where
        # (workspace path included); add the remediation verb rather than
        # re-deriving the diagnosis, since _validate_grip_repo already did
        # the naming precisely.
        typer.echo(f"not_initialized: {exc} Run `gr2 grip init` to create it.", err=True)
        raise typer.Exit(code=2)


_ROW_REQUIRED = ("key", "remote", "base", "head")
# Every field of a rows-json entry is a string; a JSON number/bool/null/object
# in any of them must refuse cleanly, not traceback downstream on a string op.
_ROW_STRING_FIELDS = ("key", "remote", "base", "head", "path", "ref", "title", "body", "source", "range")


def _normalize_review_row(raw: object) -> dict:
    """Fill a rows-json entry to the shape create_review_bind_commit expects:
    key/remote/base/head required; path defaults to key, ref to refs/heads/dev,
    title/body to empty, source resolved to an absolute path when present."""
    if not isinstance(raw, dict):
        raise typer.BadParameter(f"each rows-json entry must be a JSON object, got {type(raw).__name__}")
    # Type BEFORE presence: a number in a string field is a wrong-type error, not
    # a missing one, and must be named before the string ops downstream see it.
    for f in _ROW_STRING_FIELDS:
        if f in raw and not isinstance(raw[f], str):
            raise typer.BadParameter(
                f"rows-json entry {raw.get('key', '?')!r} field {f!r} must be a string, "
                f"got {type(raw[f]).__name__}"
            )
    missing = [f for f in _ROW_REQUIRED if not raw.get(f)]
    if missing:
        raise typer.BadParameter(f"rows-json entry {raw.get('key', '?')!r} is missing {', '.join(missing)}")
    row = {
        "key": raw["key"], "remote": raw["remote"], "base": raw["base"], "head": raw["head"],
        "path": raw.get("path") or raw["key"], "ref": raw.get("ref", "refs/heads/dev"),
        "title": raw.get("title", ""), "body": raw.get("body", ""),
    }
    if raw.get("source") and raw.get("range"):
        raise typer.BadParameter(
            f"rows-json entry {raw['key']!r}: 'source' and 'range' are mutually exclusive"
        )
    if raw.get("source"):
        row["source"] = str(Path(raw["source"]).resolve())
    if raw.get("range"):
        range_path = Path(raw["range"])
        try:
            row["range_patch"] = range_path.read_text()
        except OSError as exc:
            raise typer.BadParameter(
                f"rows-json entry {raw['key']!r} range {range_path}: {exc.strerror or exc}"
            )
    return row


@review_app.command("bind")
def review_bind(
    workspace_root: Path,
    key: Optional[str] = typer.Option(None, "--repo", help="Repository key for a single bound row"),
    remote: Optional[str] = typer.Option(None, "--remote", help="Remote URL or path of the row"),
    base: Optional[str] = typer.Option(None, "--base", help="Base SHA (must be the live remote head of --ref)"),
    head: Optional[str] = typer.Option(None, "--head", help="Reviewed head SHA (the pre-push head under review)"),
    ref: str = typer.Option("refs/heads/dev", "--ref", help="Target ref whose live head must equal --base"),
    path: Optional[str] = typer.Option(None, "--path", help="Workspace path for the row (defaults to --repo)"),
    source: Optional[Path] = typer.Option(None, "--source", help="Author clone holding the pre-push head; required to carry the range so open-gr can reconstruct"),
    from_range: Optional[Path] = typer.Option(None, "--from-range", help="A frozen range.patch (freeze-public-range.sh output). Carries the range so open-gr reconstructs, deriving the head-tree by applying it over --base in a throwaway clone — NO author clone that holds the head is needed. Exclusive with --source."),
    title: str = typer.Option("", "--title", help="Platform title text (NORM-hashed into the object)"),
    body: str = typer.Option("", "--body", help="Platform body text (NORM-hashed into the object)"),
    rows_json: Optional[Path] = typer.Option(None, "--rows-json", help="A JSON file with a list of row objects (key/remote/base/head, optional path/ref/title/body/source); binds ALL rows into ONE gr commit. Exclusive with the single-row flags."),
    ratified: Optional[str] = typer.Option(None, "--ratified", help="Named ratify receipt id: the sanctioned fix-forward when a --head is already on the remote"),
) -> None:
    """Bind a review gr commit for one or more repository rows; print ``gr:<commit>``.

    One row from --repo/--remote/--base/--head, or many from --rows-json (all in
    ONE commit). For every row, reads the live remote head of its ref and refuses
    before writing if base is not that head (behind-must-be-0) or if head is
    already on the remote without --ratified. That printed id is the whole artifact.
    """
    single = any(v is not None for v in (key, remote, base, head))
    if rows_json is not None:
        if single or source is not None or from_range is not None:
            raise typer.BadParameter("--rows-json is exclusive with --repo/--remote/--base/--head/--source/--from-range")
        try:
            text = rows_json.read_text()
        except OSError as exc:
            raise typer.BadParameter(f"--rows-json {rows_json}: {exc.strerror or exc}")
        try:
            raw = json.loads(text)
        except json.JSONDecodeError as exc:
            raise typer.BadParameter(f"--rows-json {rows_json}: invalid JSON ({exc})")
        if not isinstance(raw, list) or not raw:
            raise typer.BadParameter("--rows-json must be a non-empty JSON list of row objects")
        rows = [_normalize_review_row(r) for r in raw]
    else:
        missing = [f"--{n}" for n, v in (("repo", key), ("remote", remote), ("base", base), ("head", head)) if not v]
        if missing:
            raise typer.BadParameter(f"{', '.join(missing)} required without --rows-json")
        if source is not None and from_range is not None:
            raise typer.BadParameter("--source and --from-range are mutually exclusive")
        row = {
            "key": key, "remote": remote, "path": path or key,
            "head": head, "base": base, "ref": ref, "title": title, "body": body,
        }
        if source is not None:
            row["source"] = str(source.resolve())
        if from_range is not None:
            try:
                row["range_patch"] = from_range.read_text()
            except OSError as exc:
                raise typer.BadParameter(f"--from-range {from_range}: {exc.strerror or exc}")
        rows = [row]
    commit = _review_call(grip.create_review_bind_commit, workspace_root.resolve(), rows, ratified=ratified)
    typer.echo(f"gr:{commit}")


@review_app.command("open-gr", hidden=True)  # hidden alias for one release, dropped at 2.0 GA
def review_open_gr(
    workspace_root: Path,
    commit: str = typer.Argument(..., help="The review bind commit, as gr:<sha> or a bare sha"),
    key: Optional[str] = typer.Option(None, "--repo", help="Repository key to materialize; omit to materialize every bound row into <lane-dir>/<key>"),
    lane_dir: Path = typer.Option(..., "--lane-dir", help="Directory to materialize into (the row's clone for one --repo, or a parent holding one subdir per row)"),
    enter: bool = typer.Option(False, "--enter", help="Materialize the reconstruction (the only open mode)"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """RECONSTRUCT a review lane from a review-BIND commit: clone the recorded remote,
    check out the bound base, ``git am`` the carried range, and assert the resulting
    tree equals the bound head-tree. A tree mismatch refuses. With no --repo, every
    bound row is reconstructed into its own subdirectory of --lane-dir. Contrast
    `review open-project`, which MATERIALIZES a project-review-KIND commit's pinned
    heads from their recorded remotes and enters the review lane."""
    if not enter:
        raise typer.BadParameter("--enter is required (reconstruction is the only open mode)")
    sha = _strip_gr_prefix(commit)
    root = lane_dir.resolve()
    # close-gr reclaims the WHOLE --lane-dir, so open-gr must OWN it: refuse a
    # pre-existing non-empty dir rather than write a lane into someone's files and
    # let teardown remove them. An absent or empty dir is fine (open-gr fills it).
    if root.exists() and any(root.iterdir()):
        typer.echo(
            f"refused: lane_dir_not_empty: --lane-dir {root} exists and is not empty; "
            "pass a fresh directory (close-gr reclaims the whole lane)",
            err=True,
        )
        raise typer.Exit(code=2)
    if key is None:
        keys = _review_call(grip.review_row_keys, workspace_root.resolve(), sha)
        if not keys:
            typer.echo("refused: no_rows: the gr commit binds no repository rows", err=True)
            raise typer.Exit(code=2)
        if len(keys) == 1:
            # review-run door 3: a SINGLE bound row with no --repo materializes into
            # --lane-dir ITSELF (the lane IS the clone), exactly as `--repo <key>` would,
            # so the marker sits AT the tree root and `review run <lane-dir>` works.
            # Laying the one row out under <lane-dir>/<key> put the marker one level
            # ABOVE the only tree, after which neither `review run <lane-dir>` (no git
            # repo there) nor `review run <lane-dir>/<key>` (no marker there) could run.
            key = keys[0]
        else:
            results = {
                row_key: _review_call(
                    grip.reconstruct_review_lane, workspace_root.resolve(), sha, row_key, root / row_key
                )
                for row_key in keys
            }
            from . import open_gr_review
            open_gr_review.write_open_gr_marker(root, sha, results)
            if json_output:
                typer.echo(json.dumps(results, indent=2))
            else:
                for row_key, res in results.items():
                    match = res["bound_head_tree"] == res["reconstructed_tree"]
                    typer.echo(f"{row_key}: lane={res['lane']} tree_match={match}")
            return
    result = _review_call(
        grip.reconstruct_review_lane, workspace_root.resolve(), sha, key, root
    )
    from . import open_gr_review
    open_gr_review.write_open_gr_marker(root, sha, {key: result})
    if json_output:
        typer.echo(json.dumps(result, indent=2))
    else:
        typer.echo(f"lane: {result['lane']}")
        typer.echo(f"bound_head: {result['bound_head']}")
        typer.echo(f"reconstructed_head: {result['reconstructed_head']}")
        typer.echo(f"tree_match: {result['bound_head_tree'] == result['reconstructed_tree']}")


@review_app.command("close-gr", hidden=True)  # hidden alias for one release, dropped at 2.0 GA
def review_close_gr(
    lane_dir: Path = typer.Argument(..., help="The open-gr reconstruction lane (the --lane-dir from `review open-gr --enter`) to reclaim"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Reclaim an `open-gr --enter` reconstruction lane: verify the open-gr marker and
    remove the disposable tree. The teardown counterpart to `open-gr --enter`; unlike
    `exit-gr` (the open-PROJECT pop) it needs no OWNER_UNIT, because open-gr pushes no
    lane and changes no cwd. Refuses a directory without an open-gr marker rather than
    remove an arbitrary path."""
    from . import open_gr_review
    try:
        result = open_gr_review.close_open_gr_lane(lane_dir.resolve())
    except open_gr_review.OpenGrReviewError as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2)
    if json_output:
        typer.echo(json.dumps(result, indent=2))
    else:
        typer.echo(f"reclaimed {result['reclaimed']} (gr:{result['gr_commit']})")
        preserved = result.get("preserved_run")
        if preserved:
            typer.echo(f"review-run receipt kept at {preserved['receipt']}")
            if preserved.get("log"):
                typer.echo(f"review-run output log kept at {preserved['log']}")


@review_app.command("run")
def review_run(
    lane_dir: Path = typer.Argument(..., help="The open-gr reconstruction lane (the --lane-dir from `review open-gr --enter`)"),
    package: Optional[str] = typer.Option(None, "--package", help="Importable package name to bind the install to the lane (its __file__ must resolve under the lane). Optional if the lane's .review-install declares `package`."),
    python: Optional[str] = typer.Option(None, "--python", help="Interpreter to build the lane venv from; defaults to the running interpreter. Recorded in the receipt."),
    system_site_packages: bool = typer.Option(False, "--system-site-packages", help="Create the lane venv with --system-site-packages (host tools visible)"),
    install: Optional[str] = typer.Option(None, "--install", help="Install command (shell-split); `{venv}` and `{lane}` are substituted per token, same as the .review-install hint. Defaults to the lane's .review-install hint, else `<venv python> -m pip install -e <lane>`"),
    runner: Optional[str] = typer.Option(None, "--runner", help="Test runner: pytest (default), cargo, or jest. With a non-pytest runner the venv/install/import steps are skipped; counts come from that runner's summary line. Defaults to the lane's .review-install `runner`."),
    test: Optional[str] = typer.Option(None, "--test", help="Test command (shell-split) for a non-pytest runner, e.g. `cargo test` or `npx jest`. Defaults to the lane's .review-install `test` line, so a stranger types nothing."),
    json_output: bool = typer.Option(False, "--json", help="Emit the receipt as JSON"),
    pytest_args: Optional[List[str]] = typer.Argument(None, help="Args passed to pytest after `--` (every -k/-p/path filter is recorded)"),
) -> None:
    """The review-owned in-lane test run. For the default pytest runner: create
    `<lane>/.venv`, install the reconstructed tree, and run pytest — only after the
    lane's tree is proven to equal the bound head-tree and the import resolves under the
    lane. For a non-pytest runner (`--runner cargo|jest`, or the lane's `.review-install`
    declares one), the language-agnostic tree checks still run, then the declared test
    command runs in the lane. Counts always come from the runner's own summary line,
    never the exit code; a zero-test or unparseable run is a refusal, not a green."""
    import shlex

    from . import review_run as rr

    # Resolve runner + test command from the flags, else the lane's .review-install hint,
    # so a stranger who cloned a repo that declares itself types nothing.
    try:
        hint = rr.read_install_hint(lane_dir.resolve()) or {}
    except rr.ReviewRunRefused as exc:
        typer.echo(f"refused: {exc}", err=True)
        raise typer.Exit(code=2)
    eff_runner = runner or hint.get("runner") or "pytest"
    eff_test = test or hint.get("test")

    try:
        if eff_runner == "pytest" and test is not None:
            # Refuse rather than silently ignore: a --test command with the pytest
            # runner means the caller expected that command to run, and dropping it would
            # run pytest instead and call the result a green about the wrong thing.
            raise rr.ReviewRunRefused(
                "test_with_pytest",
                "--test is for a non-pytest runner; the pytest runner builds its own "
                "pytest invocation. Pass --runner cargo|jest with --test, or drop --test.",
            )
        if eff_runner != "pytest":
            if not eff_test:
                raise rr.ReviewRunRefused(
                    "no_test_command",
                    f"runner {eff_runner!r} needs a test command; pass --test "
                    "or declare `test = …` in the lane's .review-install",
                )
            receipt = rr.run_test_command_in_lane(
                lane_dir.resolve(), runner=eff_runner, test_command=shlex.split(eff_test)
            )
        else:
            install_cmd = shlex.split(install) if install else None
            receipt = rr.run_review_lane(
                lane_dir.resolve(),
                package=package,
                pytest_args=list(pytest_args or []),
                python=python,
                install=install_cmd,
                system_site_packages=system_site_packages,
            )
    except rr.ReviewRunRefused as exc:
        typer.echo(f"refused: {exc}", err=True)
        if json_output:
            # review-run door 2: a refusal is machine-readable too, mirroring the
            # refusal receipt the run wrote into the lane. Exit stays 2.
            typer.echo(json.dumps({
                "kind": "review-run",
                "result": "refused",
                "refusal_code": exc.code,
                "refusal_detail": exc.detail,
            }, indent=2))
        raise typer.Exit(code=2)
    if json_output:
        typer.echo(json.dumps(receipt, indent=2))
    elif receipt.get("runner"):  # non-pytest runner receipt (no venv/import fields)
        typer.echo(
            f"{receipt['result']} ({receipt['runner']}): selected={receipt['selected']} "
            f"passed={receipt['passed']} failed={receipt['failed']} "
            f"skipped={receipt['skipped']} errors={receipt['errors']}"
        )
        typer.echo(f"bound_head_tree: {receipt['bound_head_tree']}")
    else:
        typer.echo(
            f"{receipt['result']}: selected={receipt['selected']} "
            f"passed={receipt['passed']} failed={receipt['failed']} "
            f"skipped={receipt['skipped']} xfailed={receipt['xfailed']} "
            f"errors={receipt['errors']}"
        )
        typer.echo(f"bound_head_tree: {receipt['bound_head_tree']}")
        typer.echo(f"install resolved: {receipt['resolved_install_path']}")
    if receipt["result"] != "green":
        raise typer.Exit(code=1)


@review_app.command("verify")
def review_verify(
    workspace_root: Path,
    commit: str = typer.Argument(..., help="The review bind commit, as gr:<sha> or a bare sha"),
    json_output: bool = typer.Option(False, "--json", help="Emit machine-readable JSON"),
) -> None:
    """Recompute the review gr commit tree from its own objects; a mismatch is
    corruption, not drift."""
    result = _review_call(grip.verify_review_commit, workspace_root.resolve(), _strip_gr_prefix(commit))
    if json_output:
        typer.echo(json.dumps(result, indent=2, default=str))
    else:
        typer.echo(f"tree_matches: {result['tree_matches']}")
    if not result.get("tree_matches"):
        raise typer.Exit(code=1)


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
    group = pr_ops.check_pr_group_status(
        workspace_root=workspace_root,
        pr_group_id=str(group["pr_group_id"]),
        adapter=adapter,
        actor=f"agent:{owner_unit}",
    )
    statuses = []
    for pr_info in group.get("prs", []):
        repo = str(pr_info["repo"])
        number = int(pr_info["pr_number"])
        statuses.append(adapter.pr_status(repo, number).as_dict())
    payload = {
        "pr_group_id": group["pr_group_id"],
        "owner_unit": owner_unit,
        "lane_name": resolved_lane,
        "group_state": _group_state_from_statuses(statuses),
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
    for pr_info in group.get("prs", []):
        ref = PRRef(repo=str(pr_info["repo"]), number=int(pr_info["pr_number"]), url=pr_info.get("url"))
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
    method: Optional[str] = typer.Option(
        None,
        "--method",
        "-m",
        help="merge/squash/rebase. Defaults to a merge commit.",
    ),
) -> None:
    """Merge grouped PRs for a lane."""
    workspace_root = workspace_root.resolve()
    resolved_lane = _resolve_lane_name(workspace_root, owner_unit, lane_name)
    group_path, group = _find_pr_group(workspace_root, owner_unit, resolved_lane)
    adapter = get_platform_adapter(str(group.get("platform", "github")))
    merged: list[str] = []
    failed: list[dict[str, object]] = []
    if failed:
        group["group_state"] = "partially_merged" if merged else "merge_failed"
        group["merged"] = merged
        group_path.write_text(json.dumps(group, indent=2) + "\n")
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
    try:
        result = pr_ops.merge_pr_group(
            workspace_root=workspace_root,
            pr_group_id=str(group["pr_group_id"]),
            adapter=adapter,
            actor=f"agent:{owner_unit}",
            method=pr_ops.resolve_merge_method(
                explicit=method,
                configured=_configured_merge_method(workspace_root),
            ),
            verification_targets=_merge_verification_targets(
                workspace_root,
            ),
            report=lambda message: typer.echo(message, err=True),
        )
        completed = list(result.get("completed", []))
        merged = [str(item["repo"]) for item in completed]
        payload = {
            "pr_group_id": group["pr_group_id"],
            "owner_unit": owner_unit,
            "lane_name": resolved_lane,
            "merged": merged,
            "merged_receipts": completed,
            "state_path": str(group_path),
        }
    except pr_ops.PRMergeError as exc:
        completed = [item.as_dict() for item in exc.completed]
        merged = [str(item["repo"]) for item in completed]
        if exc.outcome_unknown:
            group["group_state"] = "merge_outcome_unknown"
        elif exc.operation_acknowledged:
            group["group_state"] = "merge_postcondition_failed"
        else:
            group["group_state"] = "partially_merged" if merged else "merge_failed"
        group["merged"] = merged
        group["completed"] = completed
        group_path.write_text(json.dumps(group, indent=2) + "\n")
        payload = {
            "status": (
                "outcome_unknown"
                if exc.outcome_unknown
                else (
                    "postcondition_failed"
                    if exc.operation_acknowledged
                    else ("partial_failure" if merged else "failed")
                )
            ),
            "pr_group_id": group["pr_group_id"],
            "owner_unit": owner_unit,
            "lane_name": resolved_lane,
            "merged": merged,
            "merged_receipts": completed,
            "failed": [
                {
                    "repo": exc.repo,
                    "number": exc.pr_number,
                    "reason": exc.reason,
                    "operation_acknowledged": exc.operation_acknowledged,
                }
            ],
            "state_path": str(group_path),
        }
        if json_output:
            typer.echo(json.dumps(payload, indent=2))
        else:
            typer.echo(json.dumps(payload, indent=2))
        raise typer.Exit(code=1)
    if json_output:
        typer.echo(json.dumps(payload, indent=2))
    else:
        typer.echo(json.dumps(payload, indent=2))


def main() -> None:
    app()


if __name__ == "__main__":
    app()
