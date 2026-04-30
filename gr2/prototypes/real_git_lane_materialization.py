#!/usr/bin/env python3
"""Prototype real-git same-repo multi-agent lane materialization."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import tempfile
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Verify real-git same-repo lane materialization"
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        help="optional workspace root; defaults to a temporary workspace",
    )
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def lane_proto(root: Path) -> Path:
    return root / "gr2" / "prototypes" / "lane_workspace_prototype.py"


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        argv,
        cwd=cwd,
        check=check,
        text=True,
        capture_output=capture,
    )


def init_workspace(workspace_root: Path, bare_remote: Path) -> None:
    (workspace_root / ".grip").mkdir(parents=True, exist_ok=True)
    (workspace_root / "agents").mkdir(exist_ok=True)
    spec = f"""schema_version = 1
workspace_name = "real-git-lane-materialization"

[cache]
root = ".grip/cache"

[[repos]]
name = "app"
path = "repos/app"
url = "{bare_remote}"

[[units]]
name = "atlas"
path = "agents/atlas"
agent_id = "atlas-agent"
repos = ["app"]

[[units]]
name = "apollo"
path = "agents/apollo"
agent_id = "apollo-agent"
repos = ["app"]
"""
    (workspace_root / ".grip" / "workspace_spec.toml").write_text(spec)


def seed_bare_remote(root: Path) -> Path:
    remotes = root / "remotes"
    work = root / "seed-work"
    remotes.mkdir(parents=True, exist_ok=True)
    run(["git", "init", "--bare", str(remotes / "app.git")])
    run(["git", "init", str(work)])
    run(["git", "config", "user.name", "Playground User"], cwd=work)
    run(["git", "config", "user.email", "playground@example.com"], cwd=work)
    (work / "README.md").write_text("# app\n")
    run(["git", "add", "README.md"], cwd=work)
    run(["git", "commit", "-m", "Initial commit"], cwd=work)
    run(["git", "branch", "-M", "main"], cwd=work)
    run(["git", "remote", "add", "origin", str(remotes / "app.git")], cwd=work)
    run(["git", "push", "-u", "origin", "main"], cwd=work)
    return remotes / "app.git"


def create_lane(
    root: Path, workspace_root: Path, owner_unit: str, lane_name: str, branch: str
) -> None:
    run(
        [
            "python3",
            str(lane_proto(root)),
            "create-lane",
            str(workspace_root),
            owner_unit,
            lane_name,
            "--repos",
            "app",
            "--branch",
            branch,
        ]
    )


def plan_exec_json(
    root: Path, workspace_root: Path, owner_unit: str, lane_name: str
) -> list[dict]:
    proc = run(
        [
            "python3",
            str(lane_proto(root)),
            "plan-exec",
            str(workspace_root),
            owner_unit,
            lane_name,
            "git status",
            "--json",
        ],
        capture=True,
    )
    return json.loads(proc.stdout)


def configure_checkout(repo_path: Path, user_name: str, user_email: str) -> None:
    run(["git", "config", "user.name", user_name], cwd=repo_path)
    run(["git", "config", "user.email", user_email], cwd=repo_path)


def clone_into_lane(
    remote: Path, cwd_path: Path, branch: str, user_name: str, user_email: str
) -> None:
    cwd_path.parent.mkdir(parents=True, exist_ok=True)
    run(["git", "clone", str(remote), str(cwd_path)])
    configure_checkout(cwd_path, user_name, user_email)
    run(["git", "checkout", "-b", branch], cwd=cwd_path)


def commit_lane_change(
    repo_path: Path, filename: str, contents: str, message: str
) -> str:
    (repo_path / filename).write_text(contents)
    run(["git", "add", filename], cwd=repo_path)
    run(["git", "commit", "-m", message], cwd=repo_path)
    proc = run(["git", "rev-parse", "HEAD"], cwd=repo_path, capture=True)
    return proc.stdout.strip()


def verify(workspace_root: Path) -> dict:
    root = repo_root()
    remote = seed_bare_remote(workspace_root / ".tmp")
    init_workspace(workspace_root, remote)

    create_lane(root, workspace_root, "atlas", "feat-router", "feat/router")
    create_lane(root, workspace_root, "apollo", "feat-materialize", "feat/materialize")

    atlas_plan = plan_exec_json(root, workspace_root, "atlas", "feat-router")
    apollo_plan = plan_exec_json(root, workspace_root, "apollo", "feat-materialize")

    atlas_cwd = Path(atlas_plan[0]["cwd"])
    apollo_cwd = Path(apollo_plan[0]["cwd"])

    clone_into_lane(remote, atlas_cwd, "feat/router", "Atlas User", "atlas@example.com")
    clone_into_lane(
        remote,
        apollo_cwd,
        "feat/materialize",
        "Apollo User",
        "apollo@example.com",
    )

    atlas_commit = commit_lane_change(
        atlas_cwd, "atlas.txt", "atlas lane change\n", "atlas lane commit"
    )
    apollo_commit = commit_lane_change(
        apollo_cwd, "apollo.txt", "apollo lane change\n", "apollo lane commit"
    )

    atlas_status = run(["git", "status", "--short"], cwd=atlas_cwd, capture=True).stdout.strip()
    apollo_status = run(["git", "status", "--short"], cwd=apollo_cwd, capture=True).stdout.strip()

    result = {
        "atlas_cwd": str(atlas_cwd),
        "apollo_cwd": str(apollo_cwd),
        "cwd_collision": atlas_cwd == apollo_cwd,
        "atlas_commit": atlas_commit,
        "apollo_commit": apollo_commit,
        "commit_collision": atlas_commit == apollo_commit,
        "atlas_has_apollo_file": (atlas_cwd / "apollo.txt").exists(),
        "apollo_has_atlas_file": (apollo_cwd / "atlas.txt").exists(),
        "atlas_clean": atlas_status == "",
        "apollo_clean": apollo_status == "",
    }
    result["verdict"] = (
        "holds"
        if not result["cwd_collision"]
        and not result["commit_collision"]
        and not result["atlas_has_apollo_file"]
        and not result["apollo_has_atlas_file"]
        and result["atlas_clean"]
        and result["apollo_clean"]
        else "fails"
    )
    return result


def main() -> int:
    args = parse_args()
    if args.workspace_root:
        workspace_root = args.workspace_root.resolve()
        if workspace_root.exists():
            shutil.rmtree(workspace_root)
        workspace_root.mkdir(parents=True, exist_ok=True)
        result = verify(workspace_root)
    else:
        with tempfile.TemporaryDirectory(prefix="gr2-real-git-lanes-") as tmp:
            workspace_root = Path(tmp)
            result = verify(workspace_root)

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print("gr2 real-git same-repo lane materialization")
        print(f'workspace: {workspace_root}')
        print(f'verdict: {result["verdict"]}')
        print(f'atlas cwd: {result["atlas_cwd"]}')
        print(f'apollo cwd: {result["apollo_cwd"]}')
        print(f'cwd collision: {result["cwd_collision"]}')
        print(f'commit collision: {result["commit_collision"]}')
        print(f'atlas sees apollo file: {result["atlas_has_apollo_file"]}')
        print(f'apollo sees atlas file: {result["apollo_has_atlas_file"]}')
        print(f'atlas clean: {result["atlas_clean"]}')
        print(f'apollo clean: {result["apollo_clean"]}')
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
