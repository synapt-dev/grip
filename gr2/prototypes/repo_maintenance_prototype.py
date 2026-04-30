#!/usr/bin/env python3
"""Prototype planner for gr2 repo maintenance policy.

This is intentionally separate from gr2 apply. It answers:
- which repos are missing and need clone/materialization
- which repos are safe to fast-forward
- which repos require explicit human intervention
- where autostash would be required to proceed

The goal is to keep workspace structure convergence (`gr2 apply`) separate
from repo state convergence (`gr2 repo sync` / `gr2 repo pull`).
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import subprocess
import sys
import tomllib
from pathlib import Path


@dataclasses.dataclass(frozen=True)
class RepoSpec:
    name: str
    path: str
    url: str


@dataclasses.dataclass(frozen=True)
class UnitSpec:
    name: str
    path: str
    repos: list[str]


@dataclasses.dataclass(frozen=True)
class WorkspaceSpec:
    schema_version: int
    workspace_name: str
    repos: list[RepoSpec]
    units: list[UnitSpec]


@dataclasses.dataclass(frozen=True)
class RepoTarget:
    scope: str
    target_name: str
    repo_name: str
    path: Path
    url: str


@dataclasses.dataclass(frozen=True)
class RepoPolicy:
    sync_mode: str
    dirty_policy: str
    tracked_branch: str | None


@dataclasses.dataclass(frozen=True)
class RepoStatus:
    exists: bool
    is_git_repo: bool
    branch: str | None
    upstream: str | None
    dirty: bool
    ahead: int
    behind: int
    detached: bool


@dataclasses.dataclass(frozen=True)
class PlannedAction:
    target: RepoTarget
    action: str
    reason: str
    status: RepoStatus
    policy: RepoPolicy

    def as_dict(self) -> dict[str, object]:
        return {
            "scope": self.target.scope,
            "target": self.target.target_name,
            "repo": self.target.repo_name,
            "path": str(self.target.path),
            "action": self.action,
            "reason": self.reason,
            "status": dataclasses.asdict(self.status),
            "policy": dataclasses.asdict(self.policy),
        }


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prototype gr2 repo maintenance planner")
    parser.add_argument("workspace_root", type=Path, help="gr2 workspace root")
    parser.add_argument(
        "--spec",
        type=Path,
        help="path to workspace_spec.toml (defaults to <workspace>/.grip/workspace_spec.toml)",
    )
    parser.add_argument(
        "--policy",
        type=Path,
        help="optional TOML policy file for branch/sync defaults",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="emit machine-readable JSON instead of a table",
    )
    return parser.parse_args()


def read_workspace_spec(spec_path: Path) -> WorkspaceSpec:
    with spec_path.open("rb") as fh:
        raw = tomllib.load(fh)

    repos = [
        RepoSpec(name=item["name"], path=item["path"], url=item["url"])
        for item in raw.get("repos", [])
    ]
    units = [
        UnitSpec(
            name=item["name"],
            path=item["path"],
            repos=list(item.get("repos", [])),
        )
        for item in raw.get("units", [])
    ]
    return WorkspaceSpec(
        schema_version=raw["schema_version"],
        workspace_name=raw["workspace_name"],
        repos=repos,
        units=units,
    )


def read_policy(policy_path: Path | None) -> dict[str, object]:
    if policy_path is None:
        return {}
    with policy_path.open("rb") as fh:
        return tomllib.load(fh)


def derive_targets(workspace_root: Path, spec: WorkspaceSpec) -> list[RepoTarget]:
    shared_targets = [
        RepoTarget(
            scope="shared",
            target_name=repo.name,
            repo_name=repo.name,
            path=workspace_root / repo.path,
            url=repo.url,
        )
        for repo in spec.repos
    ]

    repo_map = {repo.name: repo for repo in spec.repos}
    unit_targets: list[RepoTarget] = []
    for unit in spec.units:
        for repo_name in unit.repos:
            repo = repo_map[repo_name]
            unit_targets.append(
                RepoTarget(
                    scope="unit",
                    target_name=unit.name,
                    repo_name=repo_name,
                    path=workspace_root / unit.path / repo_name,
                    url=repo.url,
                )
            )

    return shared_targets + unit_targets


def policy_for(target: RepoTarget, policy_doc: dict[str, object]) -> RepoPolicy:
    defaults = policy_doc.get("defaults", {})
    repos = policy_doc.get("repos", {})

    if not isinstance(defaults, dict):
        defaults = {}
    if not isinstance(repos, dict):
        repos = {}

    repo_overrides = repos.get(target.repo_name, {})
    if not isinstance(repo_overrides, dict):
        repo_overrides = {}

    if target.scope == "shared":
        sync_mode = str(repo_overrides.get("sync", defaults.get("shared_sync", "ff-only")))
    else:
        sync_mode = str(repo_overrides.get("sync", defaults.get("unit_sync", "explicit")))

    dirty_policy = str(repo_overrides.get("dirty", defaults.get("dirty", "block")))
    tracked_branch = repo_overrides.get("tracked_branch", defaults.get("tracked_branch"))
    tracked_branch = str(tracked_branch) if tracked_branch is not None else None

    return RepoPolicy(
        sync_mode=sync_mode,
        dirty_policy=dirty_policy,
        tracked_branch=tracked_branch,
    )


def run_git(repo_path: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args],
        cwd=repo_path,
        check=False,
        capture_output=True,
        text=True,
    )


def inspect_repo(path: Path) -> RepoStatus:
    if not path.exists():
        return RepoStatus(
            exists=False,
            is_git_repo=False,
            branch=None,
            upstream=None,
            dirty=False,
            ahead=0,
            behind=0,
            detached=False,
        )

    git_check = run_git(path, "rev-parse", "--is-inside-work-tree")
    if git_check.returncode != 0 or git_check.stdout.strip() != "true":
        return RepoStatus(
            exists=True,
            is_git_repo=False,
            branch=None,
            upstream=None,
            dirty=False,
            ahead=0,
            behind=0,
            detached=False,
        )

    branch_proc = run_git(path, "symbolic-ref", "--quiet", "--short", "HEAD")
    detached = branch_proc.returncode != 0
    branch = None if detached else branch_proc.stdout.strip()

    upstream_proc = run_git(path, "rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{upstream}")
    upstream = upstream_proc.stdout.strip() if upstream_proc.returncode == 0 else None

    dirty_proc = run_git(path, "status", "--porcelain")
    dirty = bool(dirty_proc.stdout.strip())

    ahead = 0
    behind = 0
    if upstream:
        counts_proc = run_git(path, "rev-list", "--left-right", "--count", f"HEAD...{upstream}")
        if counts_proc.returncode == 0:
            left, right = counts_proc.stdout.strip().split()
            ahead = int(left)
            behind = int(right)

    return RepoStatus(
        exists=True,
        is_git_repo=True,
        branch=branch,
        upstream=upstream,
        dirty=dirty,
        ahead=ahead,
        behind=behind,
        detached=detached,
    )


def classify(target: RepoTarget, status: RepoStatus, policy: RepoPolicy) -> PlannedAction:
    if not status.exists:
        return PlannedAction(target, "clone_missing", "repo path is absent", status, policy)

    if not status.is_git_repo:
        return PlannedAction(
            target,
            "block_path_conflict",
            "target path exists but is not a git repo",
            status,
            policy,
        )

    if status.detached:
        return PlannedAction(
            target,
            "manual_sync",
            "repo is on a detached HEAD; do not move automatically",
            status,
            policy,
        )

    if policy.tracked_branch and status.branch != policy.tracked_branch:
        if target.scope == "shared" and not status.dirty and status.ahead == 0:
            return PlannedAction(
                target,
                "checkout_branch",
                f"shared repo should be on {policy.tracked_branch}, found {status.branch}",
                status,
                policy,
            )
        return PlannedAction(
            target,
            "manual_sync",
            f"branch mismatch: expected {policy.tracked_branch}, found {status.branch}",
            status,
            policy,
        )

    if status.dirty:
        if policy.dirty_policy == "autostash":
            return PlannedAction(
                target,
                "autostash_then_sync",
                "working tree is dirty and policy allows preservation",
                status,
                policy,
            )
        return PlannedAction(
            target,
            "block_dirty",
            "working tree is dirty; stop by default",
            status,
            policy,
        )

    if not status.upstream:
        return PlannedAction(
            target,
            "manual_sync",
            "repo has no upstream tracking branch",
            status,
            policy,
        )

    if status.behind == 0 and status.ahead == 0:
        return PlannedAction(
            target,
            "no_change",
            "repo is already aligned with upstream",
            status,
            policy,
        )

    if status.behind > 0 and status.ahead == 0:
        if policy.sync_mode == "ff-only":
            return PlannedAction(
                target,
                "fast_forward",
                f"repo is behind upstream by {status.behind} commit(s) and can fast-forward",
                status,
                policy,
            )
        return PlannedAction(
            target,
            "manual_sync",
            f"repo is behind upstream by {status.behind} commit(s), but policy requires explicit sync",
            status,
            policy,
        )

    if status.behind > 0 and status.ahead > 0:
        return PlannedAction(
            target,
            "manual_sync",
            f"repo diverged from upstream (ahead {status.ahead}, behind {status.behind})",
            status,
            policy,
        )

    if status.ahead > 0:
        return PlannedAction(
            target,
            "manual_sync",
            f"repo has {status.ahead} local commit(s) ahead of upstream",
            status,
            policy,
        )

    return PlannedAction(target, "manual_sync", "unclassified repo state", status, policy)


def render_table(actions: list[PlannedAction]) -> str:
    lines = [
        "gr2 repo-maintenance prototype",
        "SCOPE\tTARGET\tREPO\tACTION\tBRANCH\tUPSTREAM\tSTATE\tREASON",
    ]
    for item in actions:
        state_bits = []
        if item.status.dirty:
            state_bits.append("dirty")
        if item.status.ahead:
            state_bits.append(f"ahead={item.status.ahead}")
        if item.status.behind:
            state_bits.append(f"behind={item.status.behind}")
        if item.status.detached:
            state_bits.append("detached")
        if not state_bits:
            state_bits.append("clean")

        lines.append(
            "\t".join(
                [
                    item.target.scope,
                    item.target.target_name,
                    item.target.repo_name,
                    item.action,
                    item.status.branch or "-",
                    item.status.upstream or "-",
                    ",".join(state_bits),
                    item.reason,
                ]
            )
        )
    return "\n".join(lines)


def main() -> int:
    args = parse_args()
    workspace_root = args.workspace_root.resolve()
    spec_path = (args.spec or workspace_root / ".grip" / "workspace_spec.toml").resolve()
    spec = read_workspace_spec(spec_path)
    policy_doc = read_policy(args.policy)

    actions = []
    for target in derive_targets(workspace_root, spec):
        status = inspect_repo(target.path)
        policy = policy_for(target, policy_doc)
        actions.append(classify(target, status, policy))

    if args.json:
        print(json.dumps([item.as_dict() for item in actions], indent=2))
    else:
        print(render_table(actions))

    return 0


if __name__ == "__main__":
    sys.exit(main())
