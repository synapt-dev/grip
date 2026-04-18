"""gr2 event system runtime.

Implements the event contract from HOOK-EVENT-CONTRACT.md sections 3-8:
- EventType enum (section 7.2)
- emit() function (sections 4.2, 7.1)
- Outbox management with rotation (sections 4.1-4.4)
- Cursor-based consumer model (section 5.1)
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path


_RESERVED_NAMES = frozenset(
    {
        "version",
        "event_id",
        "seq",
        "timestamp",
        "type",
        "workspace",
        "actor",
        "agent_id",
        "owner_unit",
    }
)

_ROTATION_THRESHOLD = 10 * 1024 * 1024


class EventType(str, Enum):
    LANE_CREATED = "lane.created"
    LANE_ENTERED = "lane.entered"
    LANE_EXITED = "lane.exited"
    LANE_SWITCHED = "lane.switched"
    LANE_ARCHIVED = "lane.archived"

    LEASE_ACQUIRED = "lease.acquired"
    LEASE_RELEASED = "lease.released"
    LEASE_EXPIRED = "lease.expired"
    LEASE_FORCE_BROKEN = "lease.force_broken"

    HOOK_STARTED = "hook.started"
    HOOK_COMPLETED = "hook.completed"
    HOOK_FAILED = "hook.failed"
    HOOK_SKIPPED = "hook.skipped"

    PR_CREATED = "pr.created"
    PR_STATUS_CHANGED = "pr.status_changed"
    PR_CHECKS_PASSED = "pr.checks_passed"
    PR_CHECKS_FAILED = "pr.checks_failed"
    PR_REVIEW_SUBMITTED = "pr.review_submitted"
    PR_MERGED = "pr.merged"
    PR_MERGE_FAILED = "pr.merge_failed"

    SYNC_STARTED = "sync.started"
    SYNC_CACHE_SEEDED = "sync.cache_seeded"
    SYNC_CACHE_REFRESHED = "sync.cache_refreshed"
    SYNC_REPO_UPDATED = "sync.repo_updated"
    SYNC_REPO_FETCHED = "sync.repo_fetched"
    SYNC_REPO_SKIPPED = "sync.repo_skipped"
    SYNC_CONFLICT = "sync.conflict"
    SYNC_COMPLETED = "sync.completed"

    # Execution
    EXEC_STARTED = "exec.started"
    EXEC_COMPLETED = "exec.completed"
    EXEC_FAILED = "exec.failed"

    FAILURE_RESOLVED = "failure.resolved"
    LEASE_RECLAIMED = "lease.reclaimed"

    WORKSPACE_MATERIALIZED = "workspace.materialized"
    WORKSPACE_FILE_PROJECTED = "workspace.file_projected"


def _outbox_path(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "events" / "outbox.jsonl"


def _cursors_dir(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "events" / "cursors"


def _current_seq(outbox: Path) -> int:
    if not outbox.exists():
        return 0
    try:
        text = outbox.read_text()
    except OSError:
        return 0
    last_seq = 0
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            if isinstance(obj, dict) and "seq" in obj:
                last_seq = max(last_seq, obj["seq"])
        except (json.JSONDecodeError, TypeError):
            continue
    return last_seq


def _maybe_rotate(outbox: Path) -> None:
    if not outbox.exists():
        return
    try:
        size = outbox.stat().st_size
    except OSError:
        return
    if size <= _ROTATION_THRESHOLD:
        return
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
    archive = outbox.parent / f"outbox.{ts}.jsonl"
    outbox.rename(archive)


def emit(
    event_type: EventType,
    workspace_root: Path,
    actor: str,
    owner_unit: str,
    payload: dict[str, object],
    *,
    agent_id: str | None = None,
) -> None:
    collisions = _RESERVED_NAMES & payload.keys()
    if collisions:
        raise ValueError(f"payload keys collide with reserved envelope/context names: {collisions}")

    try:
        outbox = _outbox_path(workspace_root)
        outbox.parent.mkdir(parents=True, exist_ok=True)

        seq = _current_seq(outbox) + 1
        _maybe_rotate(outbox)

        event: dict[str, object] = {
            "version": 1,
            "event_id": os.urandom(8).hex(),
            "seq": seq,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "type": str(event_type.value),
            "workspace": workspace_root.name,
            "actor": actor,
            "owner_unit": owner_unit,
        }
        if agent_id is not None:
            event["agent_id"] = agent_id
        event.update(payload)

        with outbox.open("a") as f:
            f.write(json.dumps(event, separators=(",", ":")) + "\n")
            f.flush()

    except Exception as exc:
        print(f"gr2: event emit failed: {exc}", file=sys.stderr)


def read_events(workspace_root: Path, consumer: str) -> list[dict[str, object]]:
    outbox = _outbox_path(workspace_root)
    if not outbox.exists():
        return []

    cursor = _load_cursor(workspace_root, consumer)
    last_seq = cursor.get("last_seq", 0)

    events: list[dict[str, object]] = []
    text = outbox.read_text()
    for line in text.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(obj, dict):
            continue
        if obj.get("seq", 0) <= last_seq:
            continue
        events.append(obj)

    if events:
        last_event = events[-1]
        _save_cursor(
            workspace_root,
            consumer,
            {
                "consumer": consumer,
                "last_seq": last_event["seq"],
                "last_event_id": last_event.get("event_id", ""),
                "last_read": datetime.now(timezone.utc).isoformat(),
            },
        )

    return events


def _load_cursor(workspace_root: Path, consumer: str) -> dict[str, object]:
    cursor_file = _cursors_dir(workspace_root) / f"{consumer}.json"
    if not cursor_file.exists():
        return {}
    try:
        return json.loads(cursor_file.read_text())
    except (json.JSONDecodeError, OSError):
        return {}


def _save_cursor(workspace_root: Path, consumer: str, data: dict[str, object]) -> None:
    cursors = _cursors_dir(workspace_root)
    cursors.mkdir(parents=True, exist_ok=True)
    cursor_file = cursors / f"{consumer}.json"
    tmp = cursor_file.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.rename(cursor_file)
