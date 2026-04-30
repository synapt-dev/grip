#!/usr/bin/env python3
"""Validate the Python-first gr2 surface against real GitHub remotes.

This harness does not try to prove every future workflow. It validates the
current product-critical path against the real synapt-dev playground repos:

- bootstrap from a real workspace spec
- feature-lane coexistence across multiple repos
- review-lane isolation from feature work
- exec status honesty against actual checkout state
- dirty-state detection and recovery
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
PLAYGROUND_REPOS = {
    "app": {
        "ssh": "git@github.com:synapt-dev/gr2-playground-app.git",
        "https": "https://github.com/synapt-dev/gr2-playground-app.git",
    },
    "api": {
        "ssh": "git@github.com:synapt-dev/gr2-playground-api.git",
        "https": "https://github.com/synapt-dev/gr2-playground-api.git",
    },
    "web": {
        "ssh": "git@github.com:synapt-dev/gr2-playground-web.git",
        "https": "https://github.com/synapt-dev/gr2-playground-web.git",
    },
}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Validate gr2 against real synapt-dev playground repos.")
    parser.add_argument("workspace_root", type=Path)
    parser.add_argument("--owner-unit", default="atlas")
    parser.add_argument("--workspace-name", default="gr2-real-git-playground")
    parser.add_argument("--transport", choices=["ssh", "https"], default="https")
    parser.add_argument("--keep-existing", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser.parse_args()


def run(args: list[str], *, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    env = os.environ.copy()
    env["PYTHONPATH"] = str(ROOT)
    proc = subprocess.run(args, cwd=cwd, env=env, text=True, capture_output=True)
    if check and proc.returncode != 0:
        raise SystemExit(f"command failed: {args}\nstdout={proc.stdout}\nstderr={proc.stderr}")
    return proc


def pygr2(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["python3", "-m", "gr2.python_cli", *args], cwd=cwd, check=check)


def git(*args: str, cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess[str]:
    return run(["git", *args], cwd=cwd, check=check)


def repo_url(repo_name: str, transport: str) -> str:
    return PLAYGROUND_REPOS[repo_name][transport]


def write_workspace_spec(workspace_root: Path, owner_unit: str, workspace_name: str, transport: str) -> None:
    grip_dir = workspace_root / ".grip"
    grip_dir.mkdir(parents=True, exist_ok=True)
    (grip_dir / "workspace_spec.toml").write_text(
        "\n".join(
            [
                "schema_version = 1",
                f'workspace_name = "{workspace_name}"',
                "",
                "[[repos]]",
                'name = "app"',
                'path = "repos/app"',
                f'url = "{repo_url("app", transport)}"',
                "",
                "[[repos]]",
                'name = "api"',
                'path = "repos/api"',
                f'url = "{repo_url("api", transport)}"',
                "",
                "[[repos]]",
                'name = "web"',
                'path = "repos/web"',
                f'url = "{repo_url("web", transport)}"',
                "",
                "[[units]]",
                f'name = "{owner_unit}"',
                f'path = "agents/{owner_unit}/home"',
                'repos = ["app", "api", "web"]',
                "",
            ]
        )
    )


def ensure_workspace_root(path: Path, *, keep_existing: bool) -> None:
    if path.exists():
        if keep_existing:
            return
        if any(path.iterdir()):
            shutil.rmtree(path)
        else:
            path.rmdir()
    path.mkdir(parents=True, exist_ok=True)


def lane_repo_root(workspace_root: Path, owner_unit: str, lane_name: str, repo_name: str) -> Path:
    return workspace_root / "agents" / owner_unit / "lanes" / lane_name / "repos" / repo_name


def read_json(proc: subprocess.CompletedProcess[str]) -> object:
    return json.loads(proc.stdout)


def branch_name(repo_root: Path) -> str:
    return git("branch", "--show-current", cwd=repo_root).stdout.strip()


def remote_origin(repo_root: Path) -> str:
    return git("config", "--get", "remote.origin.url", cwd=repo_root).stdout.strip()


def stash_entries(repo_root: Path) -> list[str]:
    return [line for line in git("stash", "list", cwd=repo_root).stdout.splitlines() if line.strip()]


def repo_status_row(rows: list[dict[str, object]], *, scope: str, repo: str) -> dict[str, object]:
    for row in rows:
        if row["scope"] == scope and row["repo"] == repo:
            return row
    raise SystemExit(f"repo status row not found for scope={scope} repo={repo}")


def scenario_real_remote_bootstrap(workspace_root: Path, owner_unit: str, transport: str) -> dict[str, object]:
    payload = read_json(pygr2("apply", str(workspace_root), "--yes", "--json"))
    shared_roots = {name: workspace_root / "repos" / name for name in PLAYGROUND_REPOS}
    cache_roots = {
        name: workspace_root / ".grip" / "cache" / "repos" / f"{name}.git"
        for name in PLAYGROUND_REPOS
    }
    holds = "workspace_root" in payload and "operation_count" in payload
    for repo_name, repo_root in shared_roots.items():
        holds = holds and repo_root.joinpath(".git").exists()
        holds = holds and remote_origin(repo_root) == repo_url(repo_name, transport)
        holds = holds and cache_roots[repo_name].exists()
    return {
        "name": "real-remote-bootstrap",
        "holds": holds,
        "payload": payload,
    }


def scenario_multi_lane_coexistence(workspace_root: Path, owner_unit: str) -> dict[str, object]:
    pygr2(
        "lane",
        "create",
        str(workspace_root),
        owner_unit,
        "feat-auth",
        "--repos",
        "app,api",
        "--branch",
        "feat/auth",
    )
    pygr2("lane", "enter", str(workspace_root), owner_unit, "feat-auth", "--actor", "agent:atlas")
    pygr2(
        "lane",
        "create",
        str(workspace_root),
        owner_unit,
        "feat-web",
        "--repos",
        "web",
        "--branch",
        "feat/web",
    )
    pygr2("lane", "enter", str(workspace_root), owner_unit, "feat-web", "--actor", "agent:atlas")
    current = read_json(pygr2("lane", "current", str(workspace_root), owner_unit, "--json"))
    feat_auth_app = lane_repo_root(workspace_root, owner_unit, "feat-auth", "app")
    feat_auth_api = lane_repo_root(workspace_root, owner_unit, "feat-auth", "api")
    feat_web = lane_repo_root(workspace_root, owner_unit, "feat-web", "web")
    return {
        "name": "multi-lane-coexistence",
        "holds": current["current"]["lane_name"] == "feat-web"
        and feat_auth_app.exists()
        and feat_auth_api.exists()
        and feat_web.exists()
        and branch_name(feat_auth_app) == "feat/auth"
        and branch_name(feat_auth_api) == "feat/auth"
        and branch_name(feat_web) == "feat/web",
        "payload": {
            "current": current,
            "feat_auth_app": str(feat_auth_app),
            "feat_auth_api": str(feat_auth_api),
            "feat_web": str(feat_web),
        },
    }


def scenario_review_lane_isolation(workspace_root: Path, owner_unit: str) -> dict[str, object]:
    shared_app = workspace_root / "repos" / "app"
    git("checkout", "-B", "validation/base", cwd=shared_app)
    payload = read_json(
        pygr2(
            "review",
            "checkout-pr",
            str(workspace_root),
            owner_unit,
            "app",
            "101",
            "--branch",
            "main",
            "--lane",
            "review-app-101",
            "--enter",
            "--actor",
            "agent:atlas",
            "--json",
        )
    )
    current = read_json(pygr2("lane", "current", str(workspace_root), owner_unit, "--json"))
    feat_auth_app = lane_repo_root(workspace_root, owner_unit, "feat-auth", "app")
    review_app = lane_repo_root(workspace_root, owner_unit, "review-app-101", "app")
    return {
        "name": "review-lane-isolation",
        "holds": payload["lane_name"] == "review-app-101"
        and current["current"]["lane_name"] == "review-app-101"
        and branch_name(feat_auth_app) == "feat/auth"
        and branch_name(review_app) == "main"
        and feat_auth_app != review_app,
        "payload": payload,
    }


def scenario_exec_status_honest(workspace_root: Path, owner_unit: str) -> dict[str, object]:
    pygr2("lane", "enter", str(workspace_root), owner_unit, "feat-auth", "--actor", "agent:atlas")
    payload = read_json(pygr2("exec", "status", str(workspace_root), owner_unit, "--json"))
    rows = {row["repo"]: row for row in payload["rows"]}
    app_root = lane_repo_root(workspace_root, owner_unit, "feat-auth", "app")
    api_root = lane_repo_root(workspace_root, owner_unit, "feat-auth", "api")
    holds = payload["status"] == "ready" and payload["lane"] == "feat-auth"
    holds = holds and rows["app"]["cwd"] == str(app_root) and rows["api"]["cwd"] == str(api_root)
    holds = holds and rows["app"]["exists"] is True and rows["api"]["exists"] is True
    holds = holds and rows["app"]["branch"] == "feat/auth" and rows["api"]["branch"] == "feat/auth"
    holds = holds and branch_name(app_root) == "feat/auth" and branch_name(api_root) == "feat/auth"
    return {
        "name": "exec-status-honest",
        "holds": holds,
        "payload": payload,
    }


def scenario_dirty_state_and_recovery(workspace_root: Path, owner_unit: str) -> dict[str, object]:
    shared_app = workspace_root / "repos" / "app"
    shared_readme = shared_app / "README.md"
    shared_readme.write_text(shared_readme.read_text() + "\nreal-git dirty check\n")
    repo_status = read_json(pygr2("repo", "status", str(workspace_root), "--json"))
    app_status = repo_status_row(repo_status, scope="shared", repo="app")
    sync_block = read_json(pygr2("sync", "status", str(workspace_root), "--dirty", "block", "--json"))

    pygr2("lane", "enter", str(workspace_root), owner_unit, "feat-web", "--actor", "agent:atlas")
    feat_web_repo = lane_repo_root(workspace_root, owner_unit, "feat-web", "web")
    web_readme = feat_web_repo / "README.md"
    web_readme.write_text(web_readme.read_text() + "\nexit should stash this\n")
    pygr2("lane", "exit", str(workspace_root), owner_unit, "--actor", "human:layne")

    sync_run = read_json(pygr2("sync", "run", str(workspace_root), "--dirty", "stash", "--json"))
    dirty_issues = {item["code"] for item in sync_block["issues"]}
    return {
        "name": "dirty-state-and-recovery",
        "holds": app_status["action"] == "block_dirty"
        and app_status["status"]["dirty"] is True
        and sync_block["status"] == "blocked"
        and "dirty_shared_repo" in dirty_issues
        and sync_run["status"] == "success"
        and not bool(git("status", "--porcelain", cwd=shared_app).stdout.strip())
        and len(stash_entries(shared_app)) >= 1
        and not bool(git("status", "--porcelain", cwd=feat_web_repo).stdout.strip())
        and len(stash_entries(feat_web_repo)) >= 1,
        "payload": {
            "repo_status": app_status,
            "sync_block": sync_block,
            "sync_run": sync_run,
            "shared_stash": stash_entries(shared_app),
            "lane_stash": stash_entries(feat_web_repo),
        },
    }


def main() -> int:
    args = parse_args()
    workspace_root = args.workspace_root.resolve()
    ensure_workspace_root(workspace_root, keep_existing=args.keep_existing)
    write_workspace_spec(workspace_root, args.owner_unit, args.workspace_name, args.transport)

    results = [
        scenario_real_remote_bootstrap(workspace_root, args.owner_unit, args.transport),
        scenario_multi_lane_coexistence(workspace_root, args.owner_unit),
        scenario_review_lane_isolation(workspace_root, args.owner_unit),
        scenario_exec_status_honest(workspace_root, args.owner_unit),
        scenario_dirty_state_and_recovery(workspace_root, args.owner_unit),
    ]
    verdict = "holds" if all(item["holds"] for item in results) else "gaps"
    payload = {
        "verdict": verdict,
        "workspace_root": str(workspace_root),
        "transport": args.transport,
        "results": results,
    }

    if args.json:
        print(json.dumps(payload, indent=2))
    else:
        print(f"real-git playground verdict: {verdict}")
        print(f"workspace_root = {workspace_root}")
        for item in results:
            print(f"- {item['name']}: {'holds' if item['holds'] else 'gaps'}")
    return 0 if verdict == "holds" else 1


if __name__ == "__main__":
    raise SystemExit(main())
