#!/usr/bin/env python3
"""Prototype lane metadata and lane-aware execution planning for gr2.

This prototype does not mutate git state. It proves the lane model is useful by
persisting explicit lane metadata and generating execution plans scoped by lane.
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import shlex
import sys
import tomllib
from pathlib import Path


LANE_SCHEMA_VERSION = 1


@dataclasses.dataclass
class LaneMetadata:
    schema_version: int
    lane_name: str
    owner_unit: str
    lane_type: str
    repos: list[str]
    branch_map: dict[str, str]
    pr_associations: list[str]
    shared_context_roots: list[str]
    private_context_roots: list[str]
    exec_defaults: dict[str, object]
    creation_source: str

    def as_toml(self) -> str:
        lines = [
            f"schema_version = {self.schema_version}",
            f'lane_name = "{self.lane_name}"',
            f'owner_unit = "{self.owner_unit}"',
            f'lane_type = "{self.lane_type}"',
            f'creation_source = "{self.creation_source}"',
            "",
            f"repos = [{', '.join(f'\"{r}\"' for r in self.repos)}]",
            "",
            "[branch_map]",
        ]
        for repo, branch in sorted(self.branch_map.items()):
            lines.append(f'{repo} = "{branch}"')

        lines.extend(
            [
                "",
                "[context]",
                "shared_roots = ["
            ]
        )
        for root in self.shared_context_roots:
            lines.append(f'  "{root}",')

        lines.extend(
            [
                "]",
                "private_roots = [",
            ]
        )
        for root in self.private_context_roots:
            lines.append(f'  "{root}",')

        lines.extend(
            [
                "]",
                "",
                "[exec_defaults]",
            ]
        )
        for key, value in self.exec_defaults.items():
            if isinstance(value, bool):
                encoded = str(value).lower()
            elif isinstance(value, int):
                encoded = str(value)
            elif isinstance(value, list):
                encoded = "[" + ", ".join(f'"{item}"' for item in value) + "]"
            else:
                encoded = f'"{value}"'
            lines.append(f"{key} = {encoded}")

        if self.pr_associations:
            lines.extend(["", "[[pr_associations]]"])
            for assoc in self.pr_associations:
                lines.append(f'ref = "{assoc}"')

        return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prototype gr2 lane metadata + exec planner")
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-lane")
    create.add_argument("workspace_root", type=Path)
    create.add_argument("owner_unit")
    create.add_argument("lane_name")
    create.add_argument("--type", default="feature")
    create.add_argument("--repos", required=True, help="comma-separated repo names")
    create.add_argument("--branch", required=True, help="default branch for included repos")
    create.add_argument("--source", default="manual")

    show = sub.add_parser("show-lane")
    show.add_argument("workspace_root", type=Path)
    show.add_argument("owner_unit")
    show.add_argument("lane_name")

    plan = sub.add_parser("plan-exec")
    plan.add_argument("workspace_root", type=Path)
    plan.add_argument("owner_unit")
    plan.add_argument("lane_name")
    plan.add_argument("command_text")
    plan.add_argument("--repos", help="optional comma-separated repo subset")
    plan.add_argument("--json", action="store_true")

    return parser.parse_args()


def lane_dir(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return workspace_root / "agents" / owner_unit / "lanes" / lane_name


def lane_file(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return lane_dir(workspace_root, owner_unit, lane_name) / "lane.toml"


def load_workspace_spec(workspace_root: Path) -> dict:
    with (workspace_root / ".grip" / "workspace_spec.toml").open("rb") as fh:
        return tomllib.load(fh)


def create_lane(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    spec = load_workspace_spec(workspace_root)
    repo_names = [item["name"] for item in spec.get("repos", [])]
    repos = [repo.strip() for repo in args.repos.split(",") if repo.strip()]

    missing = [repo for repo in repos if repo not in repo_names]
    if missing:
        raise SystemExit(f"unknown repos for lane: {', '.join(missing)}")

    lane_root = lane_dir(workspace_root, args.owner_unit, args.lane_name)
    lane_root.mkdir(parents=True, exist_ok=True)
    (lane_root / "repos").mkdir(exist_ok=True)
    (lane_root / "context").mkdir(exist_ok=True)

    metadata = LaneMetadata(
        schema_version=LANE_SCHEMA_VERSION,
        lane_name=args.lane_name,
        owner_unit=args.owner_unit,
        lane_type=args.type,
        repos=repos,
        branch_map={repo: args.branch for repo in repos},
        pr_associations=[],
        shared_context_roots=["config", ".grip/context/shared"],
        private_context_roots=[
            f"agents/{args.owner_unit}/home/context",
            f"agents/{args.owner_unit}/lanes/{args.lane_name}/context",
        ],
        exec_defaults={
            "parallelism": "workspace-default",
            "fail_fast": True,
            "default_command_family": ["build", "test"],
        },
        creation_source=args.source,
    )
    lane_file(workspace_root, args.owner_unit, args.lane_name).write_text(metadata.as_toml())
    print(lane_file(workspace_root, args.owner_unit, args.lane_name))
    return 0


def show_lane(args: argparse.Namespace) -> int:
    print(lane_file(args.workspace_root.resolve(), args.owner_unit, args.lane_name).read_text())
    return 0


def plan_exec(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    lane_doc = tomllib.loads(
        lane_file(workspace_root, args.owner_unit, args.lane_name).read_text()
    )

    selected_repos = lane_doc["repos"]
    if args.repos:
        requested = [repo.strip() for repo in args.repos.split(",") if repo.strip()]
        selected_repos = [repo for repo in selected_repos if repo in requested]

    command_argv = shlex.split(args.command_text)
    rows = []
    for repo in selected_repos:
        rows.append(
            {
                "lane": lane_doc["lane_name"],
                "owner_unit": lane_doc["owner_unit"],
                "repo": repo,
                "branch": lane_doc["branch_map"].get(repo),
                "cwd": str(workspace_root / "agents" / args.owner_unit / "lanes" / args.lane_name / "repos" / repo),
                "command": command_argv,
                "shared_context_roots": lane_doc.get("context", {}).get("shared_roots", []),
                "private_context_roots": lane_doc.get("context", {}).get("private_roots", []),
                "fail_fast": lane_doc["exec_defaults"]["fail_fast"],
            }
        )

    if args.json:
        print(json.dumps(rows, indent=2))
    else:
        print("gr2 lane-exec prototype")
        print("LANE\tREPO\tBRANCH\tCWD\tCOMMAND")
        for row in rows:
            print(
                f"{row['lane']}\t{row['repo']}\t{row['branch']}\t{row['cwd']}\t{' '.join(row['command'])}"
            )
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "create-lane":
        return create_lane(args)
    if args.command == "show-lane":
        return show_lane(args)
    if args.command == "plan-exec":
        return plan_exec(args)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
