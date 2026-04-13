#!/usr/bin/env python3
"""Prototype recall-friendly indexing over gr2 lane event history."""

from __future__ import annotations

import argparse
import json
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Prototype recall lane history surface"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    demo = sub.add_parser("demo-data")
    demo.add_argument("workspace_root", type=Path)

    query = sub.add_parser("query")
    query.add_argument("workspace_root", type=Path)
    query.add_argument("--lane")
    query.add_argument("--actor")
    query.add_argument("--repo")
    query.add_argument("--start")
    query.add_argument("--end")
    query.add_argument("--json", action="store_true")

    return parser.parse_args()


def events_dir(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "events"


def lane_events_file(workspace_root: Path) -> Path:
    return events_dir(workspace_root) / "lane_events.jsonl"


def append_jsonl(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(payload) + "\n")


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows: list[dict] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        rows.append(json.loads(line))
    return rows


def parse_ts(raw: str) -> datetime:
    return datetime.fromisoformat(raw)


def now_utc() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def event_key(event: dict) -> tuple:
    return (
        event.get("timestamp", ""),
        event.get("lane", ""),
        event.get("type", ""),
        event.get("agent", ""),
    )


def build_index(events: list[dict]) -> dict[str, Any]:
    by_lane: dict[str, list[dict]] = defaultdict(list)
    by_actor: dict[str, list[dict]] = defaultdict(list)
    by_repo: dict[str, list[dict]] = defaultdict(list)

    for event in sorted(events, key=event_key):
        lane = event.get("lane")
        actor = event.get("agent")
        repos = event.get("repos", [])
        if lane:
            by_lane[lane].append(event)
        if actor:
            by_actor[actor].append(event)
        for repo in repos:
            by_repo[repo].append(event)

    return {
        "by_lane": dict(by_lane),
        "by_actor": dict(by_actor),
        "by_repo": dict(by_repo),
        "all": sorted(events, key=event_key),
    }


def lane_history(index: dict[str, Any], lane_name: str) -> dict[str, Any]:
    rows = index["by_lane"].get(lane_name, [])
    return {
        "query": {"lane": lane_name},
        "count": len(rows),
        "timeline": rows,
    }


def actor_history(index: dict[str, Any], actor: str) -> dict[str, Any]:
    rows = index["by_actor"].get(actor, [])
    touched_lanes = sorted({row.get("lane") for row in rows if row.get("lane")})
    return {
        "query": {"actor": actor},
        "count": len(rows),
        "lanes": touched_lanes,
        "timeline": rows,
    }


def repo_activity(index: dict[str, Any], repo: str) -> dict[str, Any]:
    rows = index["by_repo"].get(repo, [])
    actors = sorted({row.get("agent") for row in rows if row.get("agent")})
    return {
        "query": {"repo": repo},
        "count": len(rows),
        "actors": actors,
        "timeline": rows,
    }


def time_range(index: dict[str, Any], start: str, end: str) -> dict[str, Any]:
    start_dt = parse_ts(start)
    end_dt = parse_ts(end)
    rows = [
        event
        for event in index["all"]
        if start_dt <= parse_ts(event["timestamp"]) <= end_dt
    ]
    return {
        "query": {"start": start, "end": end},
        "count": len(rows),
        "timeline": rows,
    }


def demo_events() -> list[dict]:
    base = now_utc() - timedelta(days=7)

    def at(minutes: int) -> str:
        return (base + timedelta(minutes=minutes)).isoformat()

    return [
        {
            "type": "lane_enter",
            "agent": "agent:atlas",
            "agent_id": "agent_atlas_ghi789",
            "owner_unit": "design-research",
            "lane": "auth-refactor",
            "lane_type": "feature",
            "repos": ["grip", "premium"],
            "timestamp": at(0),
        },
        {
            "type": "lease_acquire",
            "agent": "agent:atlas",
            "agent_id": "agent_atlas_ghi789",
            "owner_unit": "design-research",
            "lane": "auth-refactor",
            "lane_type": "feature",
            "lease_mode": "edit",
            "repos": ["grip", "premium"],
            "timestamp": at(2),
        },
        {
            "type": "lane_enter",
            "agent": "agent:sentinel",
            "agent_id": "agent_sentinel_def456",
            "owner_unit": "qa-sentinel",
            "lane": "backend-review",
            "lane_type": "review",
            "repos": ["tests", "grip"],
            "timestamp": at(5),
        },
        {
            "type": "lease_acquire",
            "agent": "agent:sentinel",
            "agent_id": "agent_sentinel_def456",
            "owner_unit": "qa-sentinel",
            "lane": "backend-review",
            "lane_type": "review",
            "lease_mode": "review",
            "repos": ["tests", "grip"],
            "timestamp": at(7),
        },
        {
            "type": "lease_release",
            "agent": "agent:atlas",
            "agent_id": "agent_atlas_ghi789",
            "owner_unit": "design-research",
            "lane": "auth-refactor",
            "lane_type": "feature",
            "repos": ["grip", "premium"],
            "timestamp": at(45),
        },
        {
            "type": "lane_exit",
            "agent": "agent:atlas",
            "agent_id": "agent_atlas_ghi789",
            "owner_unit": "design-research",
            "lane": "auth-refactor",
            "lane_type": "feature",
            "repos": ["grip", "premium"],
            "timestamp": at(47),
        },
        {
            "type": "lane_enter",
            "agent": "agent:opus",
            "agent_id": "agent_opus_abc123",
            "owner_unit": "release-control",
            "lane": "auth-refactor",
            "lane_type": "review",
            "repos": ["grip", "premium"],
            "timestamp": at(60),
        },
        {
            "type": "lease_acquire",
            "agent": "agent:opus",
            "agent_id": "agent_opus_abc123",
            "owner_unit": "release-control",
            "lane": "auth-refactor",
            "lane_type": "review",
            "lease_mode": "review",
            "repos": ["grip", "premium"],
            "timestamp": at(62),
        },
    ]


def write_demo_data(workspace_root: Path) -> dict[str, Any]:
    path = lane_events_file(workspace_root)
    rows = demo_events()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("")
    for row in rows:
        append_jsonl(path, row)
    return {"path": str(path), "count": len(rows)}


def render_result(result: dict[str, Any], as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, indent=2))
        return 0
    print(json.dumps(result, indent=2))
    return 0


def main() -> int:
    args = parse_args()
    if args.command == "demo-data":
        result = write_demo_data(args.workspace_root.resolve())
        print(json.dumps(result, indent=2))
        return 0

    events = load_jsonl(lane_events_file(args.workspace_root.resolve()))
    index = build_index(events)

    if args.command == "query":
        if args.lane:
            return render_result(lane_history(index, args.lane), args.json)
        if args.actor:
            return render_result(actor_history(index, args.actor), args.json)
        if args.repo:
            return render_result(repo_activity(index, args.repo), args.json)
        if args.start and args.end:
            return render_result(time_range(index, args.start, args.end), args.json)
        raise SystemExit("query requires one of --lane, --actor, --repo, or --start/--end")

    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
