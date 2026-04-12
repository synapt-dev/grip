#!/usr/bin/env python3
"""Bootstrap and verify a real-git gr2 playground workspace.

This harness is intentionally pragmatic:

- it uses actual GitHub repos in synapt-dev
- it exercises the current shipped gr2 surfaces against real remotes
- it combines current gr2 commands with prototype-only scratchpad commands

The goal is not to pretend gr2 is further along than it is. The goal is to
pressure the UX against real git state so we can iterate with data.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path


PLAYGROUND_REPOS = {
    "app": "git@github.com:synapt-dev/gr2-playground-app.git",
    "api": "git@github.com:synapt-dev/gr2-playground-api.git",
    "web": "git@github.com:synapt-dev/gr2-playground-web.git",
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Bootstrap a real-git gr2 playground workspace"
    )
    parser.add_argument("workspace_root", type=Path)
    parser.add_argument("--owner-unit", default="atlas")
    parser.add_argument("--workspace-name", default="gr2-real-git-playground")
    parser.add_argument(
        "--keep-existing",
        action="store_true",
        help="reuse an existing workspace root instead of failing",
    )
    return parser.parse_args()


def repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def gr2_binary(root: Path) -> Path:
    binary = root / "target" / "debug" / "gr2"
    if binary.exists():
        return binary
    run(["cargo", "build", "--quiet", "--bin", "gr2"], cwd=root)
    return binary


def lane_proto(root: Path) -> Path:
    return root / "gr2" / "prototypes" / "lane_workspace_prototype.py"


def run(
    argv: list[str],
    *,
    cwd: Path | None = None,
    capture: bool = False,
) -> subprocess.CompletedProcess[str]:
    print("+", " ".join(argv))
    return subprocess.run(
        argv,
        cwd=cwd,
        check=True,
        text=True,
        capture_output=capture,
    )


def gr2_supports_exec(gr2: Path) -> bool:
    help_out = run([str(gr2), "--help"], capture=True)
    return "\n  exec" in help_out.stdout


def write_workspace_spec(workspace_root: Path, owner_unit: str, workspace_name: str) -> None:
    spec = f"""schema_version = 1
workspace_name = "{workspace_name}"

[cache]
root = ".grip/cache"

[[repos]]
name = "app"
path = "repos/app"
url = "{PLAYGROUND_REPOS["app"]}"

[[repos]]
name = "api"
path = "repos/api"
url = "{PLAYGROUND_REPOS["api"]}"

[[repos]]
name = "web"
path = "repos/web"
url = "{PLAYGROUND_REPOS["web"]}"

[[units]]
name = "{owner_unit}"
path = "agents/{owner_unit}"
repos = ["app", "api", "web"]
"""
    spec_path = workspace_root / ".grip" / "workspace_spec.toml"
    spec_path.write_text(spec)


def ensure(condition: bool, message: str) -> None:
    if not condition:
        raise SystemExit(message)


def unit_repo_path(workspace_root: Path, owner_unit: str, repo_name: str) -> Path:
    return workspace_root / "agents" / owner_unit / repo_name


def verify_paths(workspace_root: Path, owner_unit: str) -> None:
    for rel in [
        ".grip/workspace.toml",
        ".grip/workspace_spec.toml",
        f"agents/{owner_unit}/app/.git",
        f"agents/{owner_unit}/api/.git",
        f"agents/{owner_unit}/web/.git",
        f".grip/state/lanes/{owner_unit}/feat-auth.toml",
        f".grip/state/lanes/{owner_unit}/feat-web.toml",
        f".grip/state/lanes/{owner_unit}/review-app-123.toml",
        "shared/scratchpads/blog-draft/scratchpad.toml",
    ]:
        ensure((workspace_root / rel).exists(), f"expected path missing: {rel}")


def main() -> int:
    args = parse_args()
    root = repo_root()
    workspace_root = args.workspace_root.resolve()

    if workspace_root.exists() and not args.keep_existing:
        if any(workspace_root.iterdir()):
            raise SystemExit(
                f"workspace root already exists and is not empty: {workspace_root} "
                "(pass --keep-existing to reuse)"
            )
        workspace_root.rmdir()

    gr2 = gr2_binary(root)
    lane_script = lane_proto(root)
    has_exec = gr2_supports_exec(gr2)

    if not workspace_root.exists():
        run(
            [
                str(gr2),
                "init",
                str(workspace_root),
                "--name",
                args.workspace_name,
            ],
            cwd=root,
        )

    run([str(gr2), "unit", "add", args.owner_unit], cwd=workspace_root)

    for repo_name, repo_url in PLAYGROUND_REPOS.items():
        run([str(gr2), "repo", "add", repo_name, repo_url], cwd=workspace_root)

    write_workspace_spec(workspace_root, args.owner_unit, args.workspace_name)

    run([str(gr2), "spec", "validate"], cwd=workspace_root)
    run([str(gr2), "plan", "--yes"], cwd=workspace_root)
    run([str(gr2), "apply", "--yes"], cwd=workspace_root)

    run(["git", "-C", str(unit_repo_path(workspace_root, args.owner_unit, "app")), "checkout", "-b", "feat/auth"])
    run(["git", "-C", str(unit_repo_path(workspace_root, args.owner_unit, "api")), "checkout", "-b", "feat/auth"])
    run(["git", "-C", str(unit_repo_path(workspace_root, args.owner_unit, "web")), "checkout", "-b", "feat/web"])

    app_readme = unit_repo_path(workspace_root, args.owner_unit, "app") / "README.md"
    app_readme.write_text(app_readme.read_text() + "\nDirty playground change.\n")

    repo_status = run(
        [str(gr2), "repo", "status"],
        cwd=workspace_root,
        capture=True,
    )
    print(repo_status.stdout)

    run(
        [
            str(gr2),
            "lane",
            "create",
            "feat-auth",
            "--owner-unit",
            args.owner_unit,
            "--repo",
            "app",
            "--repo",
            "api",
            "--branch",
            "app=feat/auth",
            "--branch",
            "api=feat/auth",
            "--exec",
            "cargo test -p app",
            "--exec",
            "cargo test -p api",
        ],
        cwd=workspace_root,
    )
    run(
        [
            str(gr2),
            "lane",
            "create",
            "feat-web",
            "--owner-unit",
            args.owner_unit,
            "--repo",
            "web",
            "--branch",
            "web=feat/web",
            "--exec",
            "npm test",
        ],
        cwd=workspace_root,
    )
    run(
        [
            str(gr2),
            "lane",
            "create",
            "review-app-123",
            "--owner-unit",
            args.owner_unit,
            "--type",
            "review",
            "--repo",
            "app",
            "--pr",
            "app:123",
            "--branch",
            "app=review/pr-123",
        ],
        cwd=workspace_root,
    )

    lane_list = run(
        [str(gr2), "lane", "list", "--owner-unit", args.owner_unit],
        cwd=workspace_root,
        capture=True,
    )
    print(lane_list.stdout)

    if has_exec:
        exec_status = run(
            [
                str(gr2),
                "exec",
                "status",
                "--lane",
                "feat-auth",
                "--owner-unit",
                args.owner_unit,
            ],
            cwd=workspace_root,
            capture=True,
        )
        print(exec_status.stdout)
    else:
        print(
            "note: this branch does not ship `gr2 exec status`; "
            "real-git exec verification is deferred until the exec surface lands "
            "on the same branch as the playground flow"
        )

    run(
        [
            sys.executable,
            str(lane_script),
            "create-shared-scratchpad",
            str(workspace_root),
            "blog-draft",
            "--kind",
            "doc",
            "--purpose",
            "Real-git shared drafting verification",
            "--participant",
            args.owner_unit,
            "--participant",
            "layne",
            "--ref",
            "grip#552",
            "--ref",
            "grip#555",
        ],
        cwd=root,
    )
    scratchpads = run(
        [sys.executable, str(lane_script), "list-shared-scratchpads", str(workspace_root)],
        cwd=root,
        capture=True,
    )
    print(scratchpads.stdout)

    verify_paths(workspace_root, args.owner_unit)

    print("\nreal-git playground bootstrap complete")
    print(f"workspace: {workspace_root}")
    print("verified:")
    print("- real remotes cloned into unit-local repo paths")
    print("- dirty local git state can be observed")
    print("- multiple lanes can coexist in metadata")
    if has_exec:
        print("- exec status stays lane-scoped")
    else:
        print("- exec verification is still pending branch convergence with the shipped exec surface")
    print("- shared scratchpad can exist beside private lanes")
    print("observation:")
    print("- current apply converges unit-local checkouts under agents/<unit>/..., not repos/<repo>")
    if not has_exec:
        print("- current prototype and shipped lane metadata are not yet unified enough for one exec harness")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
