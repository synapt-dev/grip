from __future__ import annotations

import json
import shutil
from pathlib import Path

import yaml


def gr1_manifest_path(workspace_root: Path) -> Path:
    return workspace_root / ".gitgrip" / "spaces" / "main" / "gripspace.yml"


def gr1_agents_path(workspace_root: Path) -> Path:
    return workspace_root / ".gitgrip" / "agents.toml"


def gr1_state_paths(workspace_root: Path) -> dict[str, Path]:
    gitgrip = workspace_root / ".gitgrip"
    return {
        "state_json": gitgrip / "state.json",
        "sync_state_json": gitgrip / "sync-state.json",
        "griptrees_json": gitgrip / "griptrees.json",
        "manifest_yaml": gr1_manifest_path(workspace_root),
    }


def detect_gr1_workspace(workspace_root: Path) -> dict[str, object]:
    manifest_path = gr1_manifest_path(workspace_root)
    if not manifest_path.exists():
        return {
            "detected": False,
            "workspace_root": str(workspace_root),
            "reason": f"missing {manifest_path}",
        }

    manifest = yaml.safe_load(manifest_path.read_text()) or {}
    repos = manifest.get("repos", {}) or {}
    agents_doc = _load_agents_doc(workspace_root)
    agent_names = sorted(((agents_doc.get("agents") or {}) or {}).keys())
    reference_repos = sorted(name for name, repo in repos.items() if bool((repo or {}).get("reference", False)))
    writable_repos = sorted(name for name in repos.keys() if name not in reference_repos)
    state_paths = gr1_state_paths(workspace_root)

    return {
        "detected": True,
        "workspace_root": str(workspace_root),
        "manifest_path": str(manifest_path),
        "repo_count": len(repos),
        "reference_repo_count": len(reference_repos),
        "agent_count": len(agent_names),
        "repos": sorted(repos.keys()),
        "reference_repos": reference_repos,
        "writable_repos": writable_repos,
        "agents": agent_names,
        "state_files": {key: str(path) for key, path in state_paths.items() if path.exists()},
    }


def migrate_gr1_workspace(workspace_root: Path, *, force: bool = False) -> dict[str, object]:
    detection = detect_gr1_workspace(workspace_root)
    if not detection["detected"]:
        raise SystemExit(detection["reason"])

    grip_dir = workspace_root / ".grip"
    spec_path = grip_dir / "workspace_spec.toml"
    if spec_path.exists() and not force:
        raise SystemExit(f"refusing to overwrite existing gr2 workspace spec: {spec_path}")

    manifest = yaml.safe_load(Path(str(detection["manifest_path"])).read_text()) or {}
    agents_doc = _load_agents_doc(workspace_root)
    compiled = compile_gr1_to_workspace_spec(workspace_root, manifest, agents_doc)

    grip_dir.mkdir(parents=True, exist_ok=True)
    spec_path.write_text(render_workspace_spec(compiled))

    migration_dir = grip_dir / "migrations" / "gr1"
    migration_dir.mkdir(parents=True, exist_ok=True)
    snapshots = preserve_gr1_state(workspace_root, migration_dir)
    summary_path = migration_dir / "migration-summary.json"
    summary = {
        "source": "gr1",
        "workspace_root": str(workspace_root),
        "workspace_spec_path": str(spec_path),
        "repo_count": len(compiled["repos"]),
        "unit_count": len(compiled["units"]),
        "snapshots": snapshots,
    }
    summary_path.write_text(json.dumps(summary, indent=2) + "\n")

    return {
        **summary,
        "units": [unit["name"] for unit in compiled["units"]],
        "repos": [repo["name"] for repo in compiled["repos"]],
    }


def compile_gr1_to_workspace_spec(
    workspace_root: Path,
    manifest: dict[str, object],
    agents_doc: dict[str, object],
) -> dict[str, object]:
    repos_doc = manifest.get("repos", {}) or {}
    repos: list[dict[str, object]] = []
    writable_repo_names: list[str] = []

    for repo_name, repo_doc in repos_doc.items():
        repo_doc = repo_doc or {}
        path = str(repo_doc.get("path", "")).strip()
        normalized_path = path[2:] if path.startswith("./") else path
        repo_item = {
            "name": str(repo_name),
            "path": normalized_path,
            "url": str(repo_doc.get("url", "")).strip(),
        }
        if "revision" in repo_doc:
            repo_item["default_branch"] = str(repo_doc.get("revision") or "").strip()
        elif "default_branch" in repo_doc:
            repo_item["default_branch"] = str(repo_doc.get("default_branch") or "").strip()
        if repo_doc.get("reference", False):
            repo_item["reference"] = True
        repos.append(repo_item)
        if not repo_doc.get("reference", False):
            writable_repo_names.append(str(repo_name))

    agents = (agents_doc.get("agents") or {}) or {}
    unit_names = sorted(agents.keys()) if agents else ["default"]
    units: list[dict[str, object]] = []
    for unit_name in unit_names:
        unit_doc = (agents.get(unit_name) or {}) if agents else {}
        units.append(
            {
                "name": unit_name,
                "path": f"agents/{unit_name}/home",
                "repos": writable_repo_names,
                "agent_id": f"gr1:{unit_name}",
                "migration_source": {
                    "worktree": unit_doc.get("worktree"),
                    "channel": unit_doc.get("channel"),
                },
            }
        )

    return {
        "workspace_name": workspace_root.name,
        "repos": repos,
        "units": units,
        "workspace_constraints": {
            "migration_source": "gr1",
        },
    }


def preserve_gr1_state(workspace_root: Path, migration_dir: Path) -> dict[str, str]:
    snapshots: dict[str, str] = {}
    for name, src in gr1_state_paths(workspace_root).items():
        if not src.exists():
            continue
        dest = migration_dir / src.name
        shutil.copy2(src, dest)
        snapshots[name] = str(dest)
    return snapshots


def render_workspace_spec(compiled: dict[str, object]) -> str:
    lines = [
        f'workspace_name = "{compiled["workspace_name"]}"',
        "",
    ]
    constraints = compiled.get("workspace_constraints") or {}
    if constraints:
        lines.append("[workspace_constraints]")
        for key, value in constraints.items():
            lines.append(f'{key} = "{value}"')
        lines.append("")

    for repo in compiled["repos"]:
        lines.extend(
            [
                "[[repos]]",
                f'name = "{repo["name"]}"',
                f'path = "{repo["path"]}"',
                f'url = "{repo["url"]}"',
            ]
        )
        default_branch = str(repo.get("default_branch", "")).strip()
        if default_branch:
            lines.append(f'default_branch = "{default_branch}"')
        if repo.get("reference", False):
            lines.append("reference = true")
        lines.append("")

    for unit in compiled["units"]:
        lines.extend(
            [
                "[[units]]",
                f'name = "{unit["name"]}"',
                f'path = "{unit["path"]}"',
                "repos = [" + ", ".join(f'"{repo}"' for repo in unit["repos"]) + "]",
            ]
        )
        agent_id = str(unit.get("agent_id", "")).strip()
        if agent_id:
            lines.append(f'agent_id = "{agent_id}"')
        lines.append("")

    return "\n".join(lines)


def render_detection(payload: dict[str, object]) -> str:
    if not payload["detected"]:
        return "\n".join(["Gr1Detection", "detected = false", f"reason = {payload['reason']}"])
    lines = [
        "Gr1Detection",
        "detected = true",
        f"workspace_root = {payload['workspace_root']}",
        f"manifest_path = {payload['manifest_path']}",
        f"repo_count = {payload['repo_count']}",
        f"reference_repo_count = {payload['reference_repo_count']}",
        f"agent_count = {payload['agent_count']}",
        "REPOS",
    ]
    lines.extend(f"- {repo}" for repo in payload["repos"])
    lines.append("AGENTS")
    lines.extend(f"- {agent}" for agent in payload["agents"])
    return "\n".join(lines)


def render_migration(payload: dict[str, object]) -> str:
    lines = [
        "Gr1Migration",
        f"workspace_root = {payload['workspace_root']}",
        f"workspace_spec_path = {payload['workspace_spec_path']}",
        f"repo_count = {payload['repo_count']}",
        f"unit_count = {payload['unit_count']}",
        "UNITS",
    ]
    lines.extend(f"- {unit}" for unit in payload["units"])
    lines.append("SNAPSHOTS")
    lines.extend(f"- {name}\t{path}" for name, path in payload["snapshots"].items())
    return "\n".join(lines)


def workspace_status(workspace_root: Path) -> dict[str, object]:
    """Report workspace state: gr1-only, gr2-only, coexistence, or none."""
    has_gr1 = gr1_manifest_path(workspace_root).exists()
    gr2_spec_path = workspace_root / ".grip" / "workspace_spec.toml"
    has_gr2 = gr2_spec_path.exists()
    migration_dir = workspace_root / ".grip" / "migrations" / "gr1"
    has_migration = migration_dir.exists() and (migration_dir / "migration-summary.json").exists()

    if has_gr1 and has_gr2:
        phase = "coexistence"
    elif has_gr1:
        phase = "gr1-only"
    elif has_gr2:
        phase = "gr2-only"
    else:
        phase = "none"

    result: dict[str, object] = {
        "workspace_root": str(workspace_root),
        "gr1": has_gr1,
        "gr2": has_gr2,
        "coexistence": has_gr1 and has_gr2,
        "migration_snapshot": has_migration,
        "phase": phase,
    }

    if has_gr1:
        detection = detect_gr1_workspace(workspace_root)
        result["gr1_repo_count"] = detection.get("repo_count", 0)
        result["gr1_agents"] = detection.get("agents", [])

    if has_gr2:
        import tomllib
        with gr2_spec_path.open("rb") as fh:
            spec = tomllib.load(fh)
        repos = spec.get("repos", [])
        units = spec.get("units", [])
        result["gr2_repo_count"] = len(repos)
        result["gr2_unit_count"] = len(units)
        result["gr2_spec_path"] = str(gr2_spec_path)

    return result


def render_status(payload: dict[str, object]) -> str:
    lines = [
        "WorkspaceStatus",
        f"phase = {payload['phase']}",
        f"workspace_root = {payload['workspace_root']}",
    ]
    if payload["gr1"]:
        lines.append(f"gr1 = true (repos: {payload.get('gr1_repo_count', '?')})")
    if payload["gr2"]:
        lines.append(f"gr2 = true (repos: {payload.get('gr2_repo_count', '?')}, units: {payload.get('gr2_unit_count', '?')})")
    if payload["coexistence"]:
        lines.append("coexistence = true (both .gitgrip and .grip present)")
    if payload.get("migration_snapshot"):
        lines.append("migration_snapshot = true (.grip/migrations/gr1/ present)")
    if not payload["gr1"] and not payload["gr2"]:
        lines.append("No workspace detected. Run `gr2 workspace init` or `gr2 workspace migrate-gr1`.")
    return "\n".join(lines)


def _load_agents_doc(workspace_root: Path) -> dict[str, object]:
    path = gr1_agents_path(workspace_root)
    if not path.exists():
        return {}
    import tomllib

    with path.open("rb") as fh:
        return tomllib.load(fh)
