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
from datetime import UTC, datetime, timedelta
from pathlib import Path


LANE_SCHEMA_VERSION = 1
SCRATCHPAD_SCHEMA_VERSION = 1


@dataclasses.dataclass
class LaneMetadata:
    schema_version: int
    lane_name: str
    owner_unit: str
    agent_id: str | None
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
            f'agent_id = "{self.agent_id or ""}"',
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
    created_at: str
    updated_at: str

    def as_toml(self) -> str:
        lines = [
            f"schema_version = {self.schema_version}",
            f'name = "{self.name}"',
            f'kind = "{self.kind}"',
            f'purpose = "{self.purpose}"',
            f'lifecycle = "{self.lifecycle}"',
            f'creation_source = "{self.creation_source}"',
            f'created_at = "{self.created_at}"',
            f'updated_at = "{self.updated_at}"',
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

    enter = sub.add_parser("enter-lane")
    enter.add_argument("workspace_root", type=Path)
    enter.add_argument("owner_unit")
    enter.add_argument("lane_name")
    enter.add_argument("--actor", required=True, help="actor label, e.g. human:layne or agent:atlas")
    enter.add_argument("--notify-channel", action="store_true")
    enter.add_argument("--recall", action="store_true")

    exit_lane = sub.add_parser("exit-lane")
    exit_lane.add_argument("workspace_root", type=Path)
    exit_lane.add_argument("owner_unit")
    exit_lane.add_argument("--actor", required=True)
    exit_lane.add_argument("--notify-channel", action="store_true")
    exit_lane.add_argument("--recall", action="store_true")

    current = sub.add_parser("current-lane")
    current.add_argument("workspace_root", type=Path)
    current.add_argument("owner_unit")
    current.add_argument("--json", action="store_true")

    history = sub.add_parser("lane-history")
    history.add_argument("workspace_root", type=Path)
    history.add_argument("owner_unit")
    history.add_argument("--json", action="store_true")

    lease = sub.add_parser("acquire-lane-lease")
    lease.add_argument("workspace_root", type=Path)
    lease.add_argument("owner_unit")
    lease.add_argument("lane_name")
    lease.add_argument("--actor", required=True)
    lease.add_argument("--mode", choices=["edit", "exec", "review"], required=True)
    lease.add_argument(
        "--ttl-seconds",
        type=int,
        default=900,
        help="lease TTL in seconds before it is considered stale",
    )
    lease.add_argument(
        "--force",
        action="store_true",
        help="break conflicting stale leases with a warning",
    )

    release = sub.add_parser("release-lane-lease")
    release.add_argument("workspace_root", type=Path)
    release.add_argument("owner_unit")
    release.add_argument("lane_name")
    release.add_argument("--actor", required=True)

    show_leases = sub.add_parser("show-lane-leases")
    show_leases.add_argument("workspace_root", type=Path)
    show_leases.add_argument("owner_unit")
    show_leases.add_argument("lane_name")
    show_leases.add_argument("--json", action="store_true")

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

    scratch_audit = sub.add_parser("audit-shared-scratchpads")
    scratch_audit.add_argument("workspace_root", type=Path)
    scratch_audit.add_argument(
        "--stale-days",
        type=int,
        default=7,
        help="mark scratchpads as stale when untouched for this many days",
    )

    promote = sub.add_parser("plan-promote-scratchpad")
    promote.add_argument("workspace_root", type=Path)
    promote.add_argument("name")
    promote.add_argument("--target-repo", required=True)
    promote.add_argument("--target-path", required=True)
    promote.add_argument("--owner-unit", required=True)
    promote.add_argument("--lane", help="optional lane that should carry the promotion")

    recommend = sub.add_parser("recommend-surface")
    recommend.add_argument("--kind", choices=["code", "doc", "review", "planning"], required=True)
    recommend.add_argument("--collaborative", action="store_true")
    recommend.add_argument("--formal-review", action="store_true")
    recommend.add_argument("--repos", type=int, default=1)
    recommend.add_argument("--shared-draft", action="store_true")

    return parser.parse_args()


def lane_dir(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return workspace_root / "agents" / owner_unit / "lanes" / lane_name


def lane_file(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return lane_dir(workspace_root, owner_unit, lane_name) / "lane.toml"


def shared_scratchpad_dir(workspace_root: Path, name: str) -> Path:
    return workspace_root / "shared" / "scratchpads" / name


def shared_scratchpad_file(workspace_root: Path, name: str) -> Path:
    return shared_scratchpad_dir(workspace_root, name) / "scratchpad.toml"


def current_lane_file(workspace_root: Path, owner_unit: str) -> Path:
    return workspace_root / ".grip" / "state" / "current_lane" / f"{owner_unit}.json"


def lane_leases_file(workspace_root: Path, owner_unit: str, lane_name: str) -> Path:
    return lane_dir(workspace_root, owner_unit, lane_name) / "leases.json"


def events_dir(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "events"


def lane_events_file(workspace_root: Path) -> Path:
    return events_dir(workspace_root) / "lane_events.jsonl"


def recall_lane_events_file(workspace_root: Path) -> Path:
    return events_dir(workspace_root) / "recall_lane_history.jsonl"


def load_workspace_spec(workspace_root: Path) -> dict:
    with (workspace_root / ".grip" / "workspace_spec.toml").open("rb") as fh:
        return tomllib.load(fh)


def find_unit_spec(workspace_root: Path, owner_unit: str) -> dict:
    spec = load_workspace_spec(workspace_root)
    for unit in spec.get("units", []):
        if unit.get("name") == owner_unit:
            return unit
    raise SystemExit(f"unit not found in workspace spec: {owner_unit}")


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


def load_current_lane_doc(workspace_root: Path, owner_unit: str) -> dict:
    path = current_lane_file(workspace_root, owner_unit)
    if not path.exists():
        raise SystemExit(f"no current lane recorded for unit: {owner_unit}")
    return json.loads(path.read_text())


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def emit_lane_event(workspace_root: Path, payload: dict) -> None:
    append_jsonl(lane_events_file(workspace_root), payload)


def emit_recall_lane_event(workspace_root: Path, payload: dict) -> None:
    append_jsonl(recall_lane_events_file(workspace_root), payload)


def iter_lane_events(workspace_root: Path) -> list[dict]:
    path = lane_events_file(workspace_root)
    if not path.exists():
        return []
    items: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        items.append(json.loads(line))
    return items


def load_lane_leases(workspace_root: Path, owner_unit: str, lane_name: str) -> list[dict]:
    path = lane_leases_file(workspace_root, owner_unit, lane_name)
    if not path.exists():
        return []
    return json.loads(path.read_text())


def write_lane_leases(workspace_root: Path, owner_unit: str, lane_name: str, leases: list[dict]) -> None:
    path = lane_leases_file(workspace_root, owner_unit, lane_name)
    path.write_text(json.dumps(leases, indent=2) + "\n")


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


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def parse_utc(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def is_stale_lease(lease: dict) -> bool:
    expires_at = lease.get("expires_at")
    if not expires_at:
        return False
    return parse_utc(expires_at) <= datetime.now(UTC)


def build_lease(actor: str, mode: str, ttl_seconds: int) -> dict:
    acquired = datetime.now(UTC).replace(microsecond=0)
    expires = acquired + timedelta(seconds=ttl_seconds)
    return {
        "actor": actor,
        "mode": mode,
        "ttl_seconds": ttl_seconds,
        "acquired_at": acquired.isoformat(),
        "expires_at": expires.isoformat(),
    }


def lease_conflicts(existing_mode: str, requested_mode: str) -> bool:
    matrix = {
        "edit": {"edit", "exec", "review"},
        "exec": {"edit", "review"},
        "review": {"edit", "exec", "review"},
    }
    return requested_mode in matrix.get(existing_mode, set())


def conflicting_leases(leases: list[dict], actor: str, requested_mode: str) -> tuple[list[dict], list[dict]]:
    active: list[dict] = []
    stale: list[dict] = []
    for lease in leases:
        if lease["actor"] == actor:
            continue
        if not lease_conflicts(lease["mode"], requested_mode):
            continue
        if is_stale_lease(lease):
            stale.append(lease)
        else:
            active.append(lease)
    return active, stale


def age_days(path: Path) -> int:
    modified = datetime.fromtimestamp(path.stat().st_mtime, UTC)
    return max(0, int((datetime.now(UTC) - modified).total_seconds() // 86400))


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
    unit_spec = find_unit_spec(workspace_root, args.owner_unit)
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
        agent_id=unit_spec.get("agent_id"),
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


def enter_lane(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    lane_doc = load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    unit_spec = find_unit_spec(workspace_root, args.owner_unit)
    path = current_lane_file(workspace_root, args.owner_unit)
    path.parent.mkdir(parents=True, exist_ok=True)

    previous: list[dict] = []
    if path.exists():
        old = json.loads(path.read_text())
        previous = old.get("recent", [])
        current = old.get("current")
        if current:
            previous.insert(0, current)

    deduped: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for item in previous:
        key = (item["owner_unit"], item["lane_name"])
        if key in seen or key == (args.owner_unit, args.lane_name):
            continue
        seen.add(key)
        deduped.append(item)
    deduped = deduped[:5]

    doc = {
        "current": {
            "owner_unit": args.owner_unit,
            "agent_id": unit_spec.get("agent_id"),
            "lane_name": args.lane_name,
            "lane_type": lane_doc["lane_type"],
            "repos": lane_doc.get("repos", []),
            "actor": args.actor,
            "entered_at": now_utc(),
        },
        "recent": deduped,
    }
    path.write_text(json.dumps(doc, indent=2) + "\n")
    event = {
        "type": "lane_enter",
        "agent": args.actor,
        "agent_id": unit_spec.get("agent_id"),
        "owner_unit": args.owner_unit,
        "lane": args.lane_name,
        "lane_type": lane_doc["lane_type"],
        "repos": lane_doc.get("repos", []),
        "timestamp": now_utc(),
    }
    emit_lane_event(workspace_root, event)
    if args.notify_channel:
        event["channel_message"] = (
            f'{args.actor} entered {args.owner_unit}/{args.lane_name} '
            f'[{lane_doc["lane_type"]}] repos={",".join(lane_doc.get("repos", []))}'
        )
    if args.recall:
        emit_recall_lane_event(
            workspace_root,
            {
                "kind": "lane_transition",
                "action": "enter",
                "owner_unit": args.owner_unit,
                "agent_id": unit_spec.get("agent_id"),
                "actor": args.actor,
                "lane": args.lane_name,
                "lane_type": lane_doc["lane_type"],
                "repos": lane_doc.get("repos", []),
                "timestamp": event["timestamp"],
            },
        )
    print(path)
    return 0


def exit_lane(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    doc = load_current_lane_doc(workspace_root, args.owner_unit)
    current_doc = doc.get("current")
    if not current_doc:
        raise SystemExit(f"no current lane to exit for unit: {args.owner_unit}")
    event = {
        "type": "lane_exit",
        "agent": args.actor,
        "agent_id": current_doc.get("agent_id"),
        "owner_unit": args.owner_unit,
        "lane": current_doc["lane_name"],
        "lane_type": current_doc["lane_type"],
        "repos": current_doc.get("repos", []),
        "timestamp": now_utc(),
    }
    emit_lane_event(workspace_root, event)
    if args.notify_channel:
        event["channel_message"] = (
            f'{args.actor} exited {args.owner_unit}/{current_doc["lane_name"]} '
            f'[{current_doc["lane_type"]}]'
        )
    if args.recall:
        emit_recall_lane_event(
            workspace_root,
            {
                "kind": "lane_transition",
                "action": "exit",
                "owner_unit": args.owner_unit,
                "agent_id": current_doc.get("agent_id"),
                "actor": args.actor,
                "lane": current_doc["lane_name"],
                "lane_type": current_doc["lane_type"],
                "repos": current_doc.get("repos", []),
                "timestamp": event["timestamp"],
            },
        )

    recent = doc.get("recent", [])
    next_current = recent[0] if recent else None
    updated = {
        "current": next_current,
        "recent": recent[1:] if next_current else [],
    }
    current_lane_file(workspace_root, args.owner_unit).write_text(json.dumps(updated, indent=2) + "\n")
    print(current_lane_file(workspace_root, args.owner_unit))
    return 0


def current_lane(args: argparse.Namespace) -> int:
    doc = load_current_lane_doc(args.workspace_root.resolve(), args.owner_unit)
    if args.json:
        print(json.dumps(doc, indent=2))
        return 0
    current_doc = doc["current"]
    print("gr2 prototype current-lane")
    print(f'owner={current_doc["owner_unit"]} lane={current_doc["lane_name"]} type={current_doc["lane_type"]} actor={current_doc["actor"]}')
    print(f'entered_at={current_doc["entered_at"]}')
    recent = doc.get("recent", [])
    if recent:
        print("recent:")
        for item in recent:
            print(f'  - {item["owner_unit"]}/{item["lane_name"]} ({item["lane_type"]})')
    return 0


def lane_history(args: argparse.Namespace) -> int:
    rows = [
        event for event in iter_lane_events(args.workspace_root.resolve())
        if event.get("owner_unit") == args.owner_unit
    ]
    if args.json:
        print(json.dumps(rows, indent=2))
        return 0
    print("TIMESTAMP\tTYPE\tACTOR\tAGENT_ID\tLANE\tREPOS")
    for row in rows:
        print(
            f'{row.get("timestamp","-")}\t{row.get("type","-")}\t{row.get("agent","-")}\t{row.get("agent_id","-")}\t{row.get("lane","-")}\t{",".join(row.get("repos", []))}'
        )
    return 0


def acquire_lane_lease(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    leases = load_lane_leases(workspace_root, args.owner_unit, args.lane_name)
    retained = [lease for lease in leases if lease["actor"] != args.actor]
    active_conflicts, stale_conflicts = conflicting_leases(retained, args.actor, args.mode)

    if active_conflicts:
        payload = {
            "status": "blocked",
            "reason": "conflicting-active-lease",
            "lane": args.lane_name,
            "owner_unit": args.owner_unit,
            "requested": {"actor": args.actor, "mode": args.mode},
            "conflicting_leases": active_conflicts,
        }
        print(json.dumps(payload, indent=2))
        return 1

    if stale_conflicts and not args.force:
        payload = {
            "status": "blocked",
            "reason": "stale-conflicting-lease",
            "lane": args.lane_name,
            "owner_unit": args.owner_unit,
            "requested": {"actor": args.actor, "mode": args.mode},
            "conflicting_leases": stale_conflicts,
            "hint": "rerun with --force to break stale conflicting leases",
        }
        print(json.dumps(payload, indent=2))
        return 1

    if stale_conflicts and args.force:
        print(
            json.dumps(
                {
                    "status": "warning",
                    "reason": "breaking-stale-conflicting-leases",
                    "broken_leases": stale_conflicts,
                },
                indent=2,
            )
        )
        stale_actors = {lease["actor"] for lease in stale_conflicts}
        retained = [lease for lease in retained if lease["actor"] not in stale_actors]

    retained.append(build_lease(args.actor, args.mode, args.ttl_seconds))
    write_lane_leases(workspace_root, args.owner_unit, args.lane_name, retained)
    lane_doc = load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    unit_spec = find_unit_spec(workspace_root, args.owner_unit)
    emit_lane_event(
        workspace_root,
        {
            "type": "lease_acquire",
            "agent": args.actor,
            "agent_id": unit_spec.get("agent_id"),
            "owner_unit": args.owner_unit,
            "lane": args.lane_name,
            "lane_type": lane_doc["lane_type"],
            "lease_mode": args.mode,
            "ttl_seconds": args.ttl_seconds,
            "repos": lane_doc.get("repos", []),
            "timestamp": now_utc(),
        },
    )
    print(lane_leases_file(workspace_root, args.owner_unit, args.lane_name))
    return 0


def release_lane_lease(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    lane_doc = load_lane_doc(workspace_root, args.owner_unit, args.lane_name)
    unit_spec = find_unit_spec(workspace_root, args.owner_unit)
    leases = load_lane_leases(workspace_root, args.owner_unit, args.lane_name)
    retained = [lease for lease in leases if lease["actor"] != args.actor]
    write_lane_leases(workspace_root, args.owner_unit, args.lane_name, retained)
    emit_lane_event(
        workspace_root,
        {
            "type": "lease_release",
            "agent": args.actor,
            "agent_id": unit_spec.get("agent_id"),
            "owner_unit": args.owner_unit,
            "lane": args.lane_name,
            "lane_type": lane_doc["lane_type"],
            "repos": lane_doc.get("repos", []),
            "timestamp": now_utc(),
        },
    )
    print(lane_leases_file(workspace_root, args.owner_unit, args.lane_name))
    return 0


def show_lane_leases(args: argparse.Namespace) -> int:
    leases = load_lane_leases(args.workspace_root.resolve(), args.owner_unit, args.lane_name)
    if args.json:
        print(json.dumps(leases, indent=2))
        return 0
    print("ACTOR\tMODE\tTTL\tACQUIRED_AT\tEXPIRES_AT\tSTATE")
    for lease in leases:
        state = "stale" if is_stale_lease(lease) else "active"
        print(
            f'{lease["actor"]}\t{lease["mode"]}\t{lease.get("ttl_seconds", "-")}\t{lease["acquired_at"]}\t{lease.get("expires_at", "-")}\t{state}'
        )
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
        created_at=now_utc(),
        updated_at=now_utc(),
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
    print("NAME\tKIND\tLIFECYCLE\tAGE_DAYS\tPARTICIPANTS\tPURPOSE")
    for path in iter_shared_scratchpad_files(workspace_root):
        doc = tomllib.loads(path.read_text())
        participants = ",".join(doc.get("participants", [])) or "-"
        print(
            f'{doc["name"]}\t{doc["kind"]}\t{doc["lifecycle"]}\t{age_days(path)}\t{participants}\t{doc["purpose"]}'
        )
    return 0


def audit_shared_scratchpads(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    print("NAME\tSTATUS\tAGE_DAYS\tISSUES")
    for path in iter_shared_scratchpad_files(workspace_root):
        doc = tomllib.loads(path.read_text())
        root = path.parent
        issues: list[str] = []
        days = age_days(path)
        docs_root = root / "docs"
        notes_root = root / "notes"
        context_root = root / "context"

        if days >= args.stale_days and doc.get("lifecycle") not in {"done", "paused"}:
            issues.append("stale-active")
        if not doc.get("participants"):
            issues.append("no-participants")
        if not doc.get("linked_refs"):
            issues.append("no-refs")
        if not docs_root.exists():
            issues.append("missing-docs-root")
        if not notes_root.exists():
            issues.append("missing-notes-root")
        if not context_root.exists():
            issues.append("missing-context-root")
        if doc.get("kind") == "doc" and not any(docs_root.iterdir()):
            issues.append("empty-docs")

        status = "ok" if not issues else "needs-attention"
        print(f'{doc["name"]}\t{status}\t{days}\t{",".join(issues) or "-"}')
    return 0


def plan_promote_scratchpad(args: argparse.Namespace) -> int:
    workspace_root = args.workspace_root.resolve()
    doc = load_shared_scratchpad_doc(workspace_root, args.name)
    lane_name = args.lane or f"promote-{args.name}"
    print("gr2 prototype scratchpad-promotion plan")
    print(f'scratchpad: {doc["name"]}')
    print(f'kind: {doc["kind"]}')
    print(f'lifecycle: {doc["lifecycle"]}')
    print(f'target repo: {args.target_repo}')
    print(f'target path: {args.target_path}')
    print(f'owner unit: {args.owner_unit}')
    print(f'suggested lane: {lane_name}')
    print("recommended:")
    print(
        f"  1. create or reuse a feature lane for {args.target_repo} under {args.owner_unit}"
    )
    print(
        f"  2. copy content from shared/scratchpads/{doc['name']}/docs into {args.target_repo}:{args.target_path}"
    )
    print(f"  3. branch and commit in lane {lane_name}")
    print("  4. open a PR once the artifact is ready for formal review")
    if not doc.get("linked_refs"):
        print("warning: scratchpad has no linked refs; traceability should be added before promotion")
    return 0


def recommend_surface(args: argparse.Namespace) -> int:
    recommendation = "feature-lane"
    rationale: list[str] = []

    if args.kind == "review" or args.formal_review:
        recommendation = "review-lane"
        rationale.append("formal review or PR inspection should stay isolated")
    elif args.kind in {"doc", "planning"} and args.collaborative:
        recommendation = "shared-scratchpad"
        rationale.append("shared drafting is lighter than a PR and should not invade private lanes")
    elif args.shared_draft:
        recommendation = "shared-scratchpad"
        rationale.append("explicit shared draft requested")
    elif args.kind == "code" and args.repos > 1:
        recommendation = "feature-lane"
        rationale.append("cross-repo implementation needs one named task context")
    elif args.kind == "code":
        recommendation = "feature-lane"
        rationale.append("private implementation should start in an isolated lane")
    else:
        recommendation = "feature-lane"
        rationale.append("default safe choice is an isolated lane")

    print("gr2 prototype surface recommendation")
    print(f"recommended: {recommendation}")
    print(f"why: {'; '.join(rationale)}")
    print("rules:")
    print("  - use a review lane for formal PR inspection")
    print("  - use a shared scratchpad for collaborative drafting")
    print("  - use a feature lane for implementation work")
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
    leases = load_lane_leases(workspace_root, args.owner_unit, args.lane_name)
    active_conflicts, stale_conflicts = conflicting_leases(leases, "agent:exec-planner", "exec")
    if active_conflicts:
        payload = {
            "status": "blocked",
            "reason": "conflicting-active-lease",
            "lane": lane_doc["lane_name"],
            "owner_unit": lane_doc["owner_unit"],
            "requested_mode": "exec",
            "conflicting_leases": active_conflicts,
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("gr2 lane-exec prototype")
            print("status=blocked reason=conflicting-active-lease")
            for lease in active_conflicts:
                print(f'conflict: actor={lease["actor"]} mode={lease["mode"]} acquired_at={lease["acquired_at"]}')
        return 0
    if stale_conflicts:
        payload = {
            "status": "blocked",
            "reason": "stale-conflicting-lease",
            "lane": lane_doc["lane_name"],
            "owner_unit": lane_doc["owner_unit"],
            "requested_mode": "exec",
            "conflicting_leases": stale_conflicts,
            "hint": "break stale leases with acquire-lane-lease --force or clean them up first",
        }
        if args.json:
            print(json.dumps(payload, indent=2))
        else:
            print("gr2 lane-exec prototype")
            print("status=blocked reason=stale-conflicting-lease")
            for lease in stale_conflicts:
                print(f'stale-conflict: actor={lease["actor"]} mode={lease["mode"]} expires_at={lease.get("expires_at", "-")}')
        return 0

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
    if args.command == "enter-lane":
        return enter_lane(args)
    if args.command == "exit-lane":
        return exit_lane(args)
    if args.command == "current-lane":
        return current_lane(args)
    if args.command == "lane-history":
        return lane_history(args)
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
    if args.command == "acquire-lane-lease":
        return acquire_lane_lease(args)
    if args.command == "release-lane-lease":
        return release_lane_lease(args)
    if args.command == "show-lane-leases":
        return show_lane_leases(args)
    if args.command == "create-shared-scratchpad":
        return create_shared_scratchpad(args)
    if args.command == "show-shared-scratchpad":
        return show_shared_scratchpad(args)
    if args.command == "list-shared-scratchpads":
        return list_shared_scratchpads(args)
    if args.command == "audit-shared-scratchpads":
        return audit_shared_scratchpads(args)
    if args.command == "plan-promote-scratchpad":
        return plan_promote_scratchpad(args)
    if args.command == "recommend-surface":
        return recommend_surface(args)
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    sys.exit(main())
