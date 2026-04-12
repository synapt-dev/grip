#!/usr/bin/env python3
"""Prototype channel bridge for gr2 lane events.

This treats lane events as the durable source of truth and derives channel
notifications from the append-only log. The prototype keeps both delivery
models visible:

- watcher: resumable, cursor-based replay from the lane event log
- sync: immediate transformation of the current log without cursor state
"""

from __future__ import annotations

import argparse
import json
from datetime import UTC, datetime
from pathlib import Path


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Prototype gr2 channel-lane bridge")
    sub = parser.add_subparsers(dest="command", required=True)

    bridge = sub.add_parser("bridge-events")
    bridge.add_argument("workspace_root", type=Path)
    bridge.add_argument(
        "--delivery",
        choices=["watcher", "sync"],
        default="watcher",
        help="watcher is the recommended durable mode",
    )
    bridge.add_argument("--json", action="store_true")

    show = sub.add_parser("show-outbox")
    show.add_argument("workspace_root", type=Path)
    show.add_argument("--json", action="store_true")

    recommend = sub.add_parser("recommend-delivery")
    recommend.add_argument("--json", action="store_true")

    return parser.parse_args()


def events_dir(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "events"


def lane_events_file(workspace_root: Path) -> Path:
    return events_dir(workspace_root) / "lane_events.jsonl"


def channel_outbox_file(workspace_root: Path) -> Path:
    return events_dir(workspace_root) / "channel_outbox.jsonl"


def channel_cursor_file(workspace_root: Path) -> Path:
    return events_dir(workspace_root) / "channel_bridge.cursor.json"


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


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


def load_cursor(workspace_root: Path) -> dict:
    path = channel_cursor_file(workspace_root)
    if not path.exists():
        return {"delivered_event_ids": [], "last_delivered_at": None}
    return json.loads(path.read_text())


def write_cursor(workspace_root: Path, cursor: dict) -> None:
    path = channel_cursor_file(workspace_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cursor, indent=2) + "\n")


def should_notify(event: dict) -> bool:
    if event["type"] in {"lane_enter", "lane_exit"}:
        return True
    if event["type"] == "lease_acquire":
        return event.get("lease_mode") in {"edit", "review"}
    if event["type"] == "lease_release":
        return True
    return False


def render_message(event: dict) -> str:
    if event.get("channel_message"):
        return event["channel_message"]
    actor = event.get("agent", "unknown")
    owner_unit = event.get("owner_unit", "unknown")
    lane = event.get("lane", "unknown")
    lane_type = event.get("lane_type", "feature")
    repos = ",".join(event.get("repos", []))
    if event["type"] == "lane_enter":
        return f"{actor} entered {owner_unit}/{lane} [{lane_type}] repos={repos}"
    if event["type"] == "lane_exit":
        return f"{actor} exited {owner_unit}/{lane} [{lane_type}]"
    if event["type"] == "lease_acquire":
        return f"{actor} claimed {event.get('lease_mode','unknown')} on {owner_unit}/{lane}"
    if event["type"] == "lease_release":
        return f"{actor} released lease on {owner_unit}/{lane}"
    raise SystemExit(f"unsupported lane event type for channel bridge: {event['type']}")


def to_channel_event(event: dict, *, delivery: str) -> dict:
    return {
        "type": "channel_post",
        "channel": "#dev",
        "delivery": delivery,
        "source_event_id": event["event_id"],
        "source_event_type": event["type"],
        "agent": event.get("agent"),
        "agent_id": event.get("agent_id"),
        "owner_unit": event.get("owner_unit"),
        "lane": event.get("lane"),
        "lane_type": event.get("lane_type"),
        "repos": event.get("repos", []),
        "message": render_message(event),
        "timestamp": now_utc(),
    }


def bridge_events(workspace_root: Path, *, delivery: str) -> dict:
    events = load_jsonl(lane_events_file(workspace_root))
    eligible = [event for event in events if should_notify(event)]
    cursor = load_cursor(workspace_root)
    delivered_ids = set(cursor.get("delivered_event_ids", []))

    if delivery == "watcher":
        pending = [event for event in eligible if event["event_id"] not in delivered_ids]
    else:
        pending = eligible

    outbox_rows = [to_channel_event(event, delivery=delivery) for event in pending]
    for row in outbox_rows:
        append_jsonl(channel_outbox_file(workspace_root), row)

    if delivery == "watcher":
        next_ids = list(delivered_ids)
        next_ids.extend(event["event_id"] for event in pending if event["event_id"] not in delivered_ids)
        write_cursor(
            workspace_root,
            {
                "delivered_event_ids": next_ids,
                "last_delivered_at": now_utc(),
            },
        )

    return {
        "delivery": delivery,
        "event_count": len(events),
        "eligible_count": len(eligible),
        "bridged_count": len(outbox_rows),
        "bridged_events": outbox_rows,
    }


def show_outbox(workspace_root: Path) -> list[dict]:
    return load_jsonl(channel_outbox_file(workspace_root))


def recommend_delivery() -> dict:
    return {
        "recommended": "watcher",
        "alternatives": ["sync"],
        "rationale": [
            "lane transitions stay durable and local even if channel delivery is down",
            "watcher mode resumes cleanly from the append-only event log with a cursor",
            "channel posting should not block lane transitions",
            "replay and dedupe semantics are simpler when the event log is the source of truth",
        ],
    }


def main() -> int:
    args = parse_args()
    if args.command == "bridge-events":
        result = bridge_events(args.workspace_root.resolve(), delivery=args.delivery)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(
                f"delivery={result['delivery']} events={result['event_count']} "
                f"eligible={result['eligible_count']} bridged={result['bridged_count']}"
            )
        return 0
    if args.command == "show-outbox":
        rows = show_outbox(args.workspace_root.resolve())
        if args.json:
            print(json.dumps(rows, indent=2))
        else:
            print("TIMESTAMP\tCHANNEL\tSOURCE\tMESSAGE")
            for row in rows:
                print(
                    f"{row['timestamp']}\t{row['channel']}\t{row['source_event_type']}\t{row['message']}"
                )
        return 0
    if args.command == "recommend-delivery":
        result = recommend_delivery()
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            print(f"recommended={result['recommended']}")
            for item in result["rationale"]:
                print(f"- {item}")
        return 0
    raise SystemExit(f"unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
