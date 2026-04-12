#!/usr/bin/env python3
"""Prototype lane metadata, execution planning, and shared scratchpads for gr2.

This prototype does not mutate git state. It explores three UX questions:

1. are lane records legible enough to guide multi-repo work?
2. can lightweight shared scratchpads fill the collaboration gap without
   violating private-workspace rules?
3. can the tool tell the user what to do next instead of forcing them to infer
   the workflow?
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
SCRATCHPAD_SCHEMA_VERSION = 1


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

        lines.extend(["", "[context]", "shared_roots = ["])
        for root in self.shared_context_roots:
            lines.append(f'  "{root}",')
        lines.extend(["]", "private_roots = ["])
        for root in self.private_context_roots:
            lines.append(f'  "{root}",')

        lines.extend(["]", "", "[exec_defaults]"])
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

        for assoc in self.pr_associations:
            lines.extend(["", "[[pr_associations]]", f'ref = "{assoc}"'])

        return "\n".join(lines) + "\n"


@dataclasses.dataclass
class SharedScratchpad:
    schema_version: int
    name: str
    kind: str
    purpose: str
    participants: list[str]
    linked_refs: list[str]
    lifecycle: str
    creation_source: str
    docs_root: str
    notes_root: str
    context_root: str

    def as_toml(self) -> str:
        lines = [
            f"schema_version = {self.schema_version}",
            f'name = "{self.name}"',
            f'kind = "{self.kind}"',
            f'purpose = "{self.purpose}"',
            f'lifecycle = "{self.lifecycle}"',
            f'creation_source = "{self.creation_source}"',
            "",
            f'participants = [{", ".join(f"\"{p}\"" for p in self.participants)}]',
            f'linked_refs = [{", ".join(f"\"{r}\"" for r in self.linked_refs)}]',
            "",
            "[paths]",
            f'docs_root = "{self.docs_root}"',
            f'notes_root = "{self.notes_root}"',
            f'context_root = "{self.context_root}"',
        ]
        return "\n".join(lines) + "\n"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prototype gr2 lanes + shared scratchpads"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    create = sub.add_parser("create-lane")
    create.add_argument("workspace_root", type=Path)
    create.add_argument("owner_unit")
    create.add_argument("lane_name")
    create.add_argument("--type", default="feature")
    create.add_argument("--repos", required=True, help="comma-separated repo names")
    create.add_argument(
        "--branch",
        required=True,
        help="default branch or repo=branch mappings separated by commas",
    )
    create.add_argument("--source", default="manual")
    create.add_argument(
        "--command",
        dest="default_commands",
        action="append",
        default=[],
        help="default lane command",
    )

    review = sub.add_parser("create-review-lane")
    review.add_argument("workspace_root", type=Path)
    review.add_argument("owner_unit")
    review.add_argument("repo")
    review.add_argument("pr_number", type=int)
    review.add_argument("--lane-name")
    review.add_argument("--branch")

    show = sub.add_parser("show-lane")
    show.add_argument("workspace_root", type=Path)
    show.add_argument("owner_unit")
    show.add_argument("lane_name")

    lane_list = sub.add_parser("list-lanes")
    lane_list.add_argument("workspace_root", type=Path)
    lane_list.add_argument("--owner-unit")

    next_step = sub.add_parser("next-step")
    next_step.add_argument("workspace_root", type=Path)
    next_step.add_argument("owner_unit")
    next_step.add_argument("lane_name")

    plan = sub.add_parser("plan-exec")
    plan.add_argument("workspace_root", type=Path)
    plan.add_argument("owner_unit")
    plan.add_argument("lane_name")
    plan.add_argument("command_text")
    plan.add_argument("--repos", help="optional comma-separated repo subset")
    plan.add_argument("--json", action="store_true")

    scratch = sub.add_parser("create-shared-scratchpad")
    scratch.add_argument("workspace_root", type=Path)
    scratch.add_argument("name")
    scratch.add_argument("--kind", default="doc")
    scratch.add_argument("--purpose", required=True)
    scratch.add_argument("--participant", action="append", default=[])
    scratch.add_argument("--ref", action="append", default=[])
    scratch.add_argument("--source", default="manual")

    scratch_show = sub.add_parser("show-shared-scratchpad")
    scratch_show.add_argument("workspace_root", type=Path)
    scratch_show.add_argument("name")

    scratch_list = sub.add_parser("list-shared-scratchpads")
    scratch_list.add_argument("workspace_root", type=Path)

    return parser.parse_args()


def lane_dir(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return workspace_root / "agents" / owner_unit / "lanes" / lane_name


def lane_file(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return lane_dir(workspace_root, owner_unit, lane_name) / "lane.toml"


def shared_scratchpad_dir(workspace_root: Path, name: str) -> Path:
    return workspace_root / "shared" / "scratchpads" / name


def shared_scratchpad_file(workspace_root: Path, name: str) -> Path:
    return shared_scratchpad_dir(workspace_root, name) / "scratchpad.toml"


def load_workspace_spec(workspace_root: Path) -> dict:
    with (workspace_root / ".grip" / "workspace_spec.toml").open("rb") as fh:
        return tomllib.load(fh)


def load_lane_doc(workspace_root: Path, owner_unit: str, lane_name: str) -> dict:
    path = lane_file(workspace_root, owner_unit, lane_name)
    if not path.exists():
        raise SystemExit(f"lane not found: {owner_unit}/{lane_name}")
    return tomllib.loads(path.read_text())


def load_shared_scratchpad_doc(workspace_root: Path, name: str) -> dict:
    path = shared_scratchpad_file(workspace_root, name)
    if not path.exists():
        raise SystemExit(f"shared scratchpad not found: {name}")
    return tomllib.loads(path.read_text())


def iter_lane_files(workspace_root: Path, owner_unit: str | None = None) -> list[Path]:
    agents_root = workspace_root / "agents"
    if owner_unit:
        lane_roots = [agents_root / owner_unit / "lanes"]
    else:
        lane_roots = [path / "lanes" for path in agents_root.iterdir() if path.is_dir()]

    files: list[Path] = []
    for root in lane_roots:
        if not root.exists():
            continue
        files.extend(sorted(root.glob("*/lane.toml")))
    return files


def iter_shared_scratchpad_files(workspace_root: Path) -> list[Path]:
    root = workspace_root / "shared" / "scratchpads"
    if not root.exists():
        return []
    return sorted(root.glob("*/scratchpad.toml"))


def parse_repo_list(raw: str) -> list[str]:
    return [repo.strip() for repo in raw.split(",") if repo.strip()]


def parse_branch_arg(raw: str, repos: list[str]) -> dict[str, str]:
    if "=" not in raw:
        return {repo: raw for repo in repos}

    branch_map: dict[str, str] = {}
    for item in raw.split(","):
        item = item.strip()
        if not item:
            continue
        repo, branch = item.split("=", 1)
        repo = repo.strip()
        branch = branch.strip()
        if repo not in repos:
            raise SystemExit(f"branch mapping references repo outside lane: {repo}")
        if not branch:
            raise SystemExit(f"empty branch in mapping: {item}")
        branch_map[repo] = branch

    missing = [repo for repo in repos if repo not in branch_map]
    if missing:
        raise SystemExit("missing branch mapping for repos: " + ", ".join(missing))

    return branch_map


def create_lane(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    spec = load_workspace_spec(workspace_root)
    repo_names = [item["name"] for item in spec.get("repos", [])]
    repos = parse_repo_list(args.repos)

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
        branch_map=parse_branch_arg(args.branch, repos),
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
            "commands": args.default_commands,
        },
        creation_source=args.source,
    )
    lane_file(workspace_root, args.owner_unit, args.lane_name).write_text(metadata.as_toml())
    print(lane_file(workspace_root, args.owner_unit, args.lane_name))
    return 0


def create_review_lane(args: argparse.Namespace) -> int:
    lane_name = args.lane_name or f"review-{args.pr_number}"
    branch = args.branch or f"pr/{args.pr_number}"
    create_args = argparse.Namespace(
        workspace_root=args.workspace_root,
        owner_unit=args.owner_unit,
        lane_name=lane_name,
        type="review",
        repos=args.repo,
        branch=f"{args.repo}={branch}",
        source="pull-request",
        default_commands=[],
    )
    create_lane(create_args)
    lane_path = lane_file(args.workspace_root.resolve(), args.owner_unit, lane_name)
    content = lane_path.read_text().rstrip()
    content += f'\n\n[[pr_associations]]\nref = "{args.repo}#{args.pr_number}"\n'
    lane_path.write_text(content)
    print(f"created review lane {args.owner_unit}/{lane_name} for {args.repo}#{args.pr_number}")
    return 0


def create_shared_scratchpad(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    root = shared_scratchpad_dir(workspace_root, args.name)
    root.mkdir(parents=True, exist_ok=True)
    (root / "docs").mkdir(exist_ok=True)
    (root / "notes").mkdir(exist_ok=True)
    (root / "context").mkdir(exist_ok=True)

    scratchpad = SharedScratchpad(
        schema_version=SCRATCHPAD_SCHEMA_VERSION,
        name=args.name,
        kind=args.kind,
        purpose=args.purpose,
        participants=sorted(set(args.participant)),
        linked_refs=args.ref,
        lifecycle="draft",
        creation_source=args.source,
        docs_root=f"shared/scratchpads/{args.name}/docs",
        notes_root=f"shared/scratchpads/{args.name}/notes",
        context_root=f"shared/scratchpads/{args.name}/context",
    )
    shared_scratchpad_file(workspace_root, args.name).write_text(scratchpad.as_toml())
    readme = root / "docs" / "README.md"
    if not readme.exists():
        readme.write_text(
            f"# {args.name}\n\nPurpose: {args.purpose}\n\nParticipants: "
            + (", ".join(scratchpad.participants) if scratchpad.participants else "unassigned")
            + "\n"
        )
    print(shared_scratchpad_file(workspace_root, args.name))
    return 0


def list_lanes(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    print("OWNER\tLANE\tTYPE\tREPOS\tPRS")
    for path in iter_lane_files(workspace_root, args.owner_unit):
        doc = tomllib.loads(path.read_text())
        refs = ",".join(item["ref"] for item in doc.get("pr_associations", [])) or "-"
        print(
            f'{doc["owner_unit"]}\t{doc["lane_name"]}\t{doc["lane_type"]}\t{len(doc.get("repos", []))}\t{refs}'
        )
    return 0


def show_lane(args: argparse.Namespace) -> int:
    print(lane_file(args.workspace_root.resolve(), args.owner_unit, args.lane_name).read_text())
    return 0


def show_shared_scratchpad(args: argparse.Namespace) -> int:
    print(shared_scratchpad_file(args.workspace_root.resolve(), args.name).read_text())
    return 0


def list_shared_scratchpads(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    print("NAME\tKIND\tLIFECYCLE\tPARTICIPANTS\tPURPOSE")
    for path in iter_shared_scratchpad_files(workspace_root):
        doc = tomllib.loads(path.read_text())
        participants = ",".join(doc.get("participants", [])) or "-"
        print(
            f'{doc["name"]}\t{doc["kind"]}\t{doc["lifecycle"]}\t{participants}\t{doc["purpose"]}'
        )
    return 0


def next_step(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    lane_doc = load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    print("gr2 prototype next-step")
    print(f'lane: {args.owner_unit}/{lane_doc["lane_name"]}')
    print(f'type: {lane_doc["lane_type"]}')
    print(f'repos: {", ".join(lane_doc["repos"])}')
    if lane_doc.get("pr_associations"):
        print("mode: review")
        print("recommended:")
        print(
            f"  python3 gr2/prototypes/lane_workspace_prototype.py plan-exec {workspace_root} {args.owner_unit} {args.lane_name} 'cargo test'"
        )
        print("  inspect the review lane, then return to your feature or home lane")
    elif lane_doc["lane_type"] == "feature":
        print("mode: feature")
        print("recommended:")
        print(
            f"  python3 gr2/prototypes/lane_workspace_prototype.py plan-exec {workspace_root} {args.owner_unit} {args.lane_name} 'cargo test'"
        )
        print(
            f"  python3 gr2/prototypes/lane_workspace_prototype.py list-shared-scratchpads {workspace_root}"
        )
    else:
        print("mode: general")
        print("recommended:")
        print(
            f"  python3 gr2/prototypes/lane_workspace_prototype.py show-lane {workspace_root} {args.owner_unit} {args.lane_name}"
        )
    return 0


def plan_exec(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    lane_doc = load_lane_doc(workspace_root, args.owner_unit, args.lane_name)

    selected_repos = lane_doc["repos"]
    if args.repos:
        requested = parse_repo_list(args.repos)
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
                "cwd": str(
                    workspace_root
                    / "agents"
                    / args.owner_unit
                    / "lanes"
                    / args.lane_name
                    / "repos"
                    / repo
                ),
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
        print(
            f'owner={lane_doc["owner_unit"]} lane={lane_doc["lane_name"]} type={lane_doc["lane_type"]} fail_fast={lane_doc["exec_defaults"]["fail_fast"]}'
        )
        print("LANE\tREPO\tBRANCH\tCWD\tCOMMAND")
        for row in rows:
            print(
                f'{row["lane"]}\t{row["repo"]}\t{row["branch"]}\t{row["cwd"]}\t{" ".join(row["command"])}'
            )
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "create-lane":
        return create_lane(args)
    if args.command == "create-review-lane":
        return create_review_lane(args)
    if args.command == "show-lane":
        return show_lane(args)
    if args.command == "list-lanes":
        return list_lanes(args)
    if args.command == "next-step":
        return next_step(args)
    if args.command == "plan-exec":
        return plan_exec(args)
    if args.command == "create-shared-scratchpad":
        return create_shared_scratchpad(args)
    if args.command == "show-shared-scratchpad":
        return show_shared_scratchpad(args)
    if args.command == "list-shared-scratchpads":
        return list_shared_scratchpads(args)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
