from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

from .events import append_outbox_event


def _now_utc() -> str:
    return datetime.now(UTC).isoformat()


def failures_root(workspace_root: Path) -> Path:
    return workspace_root / ".grip" / "state" / "failures"


def failure_marker_path(workspace_root: Path, operation_id: str) -> Path:
    return failures_root(workspace_root) / f"{operation_id}.json"


def unresolved_lane_failure(workspace_root: Path, owner_unit: str, lane_name: str) -> dict[str, object] | None:
    root = failures_root(workspace_root)
    if not root.exists():
        return None
    for path in sorted(root.glob("*.json")):
        doc = json.loads(path.read_text())
        if doc.get("resolved") is True:
            continue
        if doc.get("owner_unit") == owner_unit and doc.get("lane_name") == lane_name:
            return doc
    return None


def write_failure_marker(
    workspace_root: Path,
    *,
    operation: str,
    stage: str,
    hook_name: str,
    repo: str,
    owner_unit: str,
    lane_name: str,
    partial_state: dict[str, object] | None = None,
    event_id: str | None = None,
) -> dict[str, object]:
    operation_id = f"op_{os.urandom(4).hex()}"
    marker = {
        "operation_id": operation_id,
        "operation": operation,
        "stage": stage,
        "hook_name": hook_name,
        "repo": repo,
        "owner_unit": owner_unit,
        "lane_name": lane_name,
        "failed_at": _now_utc(),
        "event_id": event_id,
        "partial_state": partial_state or {},
        "resolved": False,
    }
    path = failure_marker_path(workspace_root, operation_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(marker, indent=2) + "\n")
    return marker


def resolve_failure_marker(
    workspace_root: Path,
    *,
    operation_id: str,
    resolved_by: str,
    resolution: str,
    owner_unit: str,
) -> dict[str, object]:
    path = failure_marker_path(workspace_root, operation_id)
    if not path.exists():
        raise SystemExit(f"failure marker not found: {operation_id}")
    marker = json.loads(path.read_text())
    path.unlink()
    event = append_outbox_event(
        workspace_root,
        {
            "type": "failure.resolved",
            "workspace": workspace_root.name,
            "actor": resolved_by,
            "owner_unit": owner_unit,
            "operation_id": operation_id,
            "resolved_by": resolved_by,
            "resolution": resolution,
            "lane_name": marker.get("lane_name", ""),
        },
    )
    return {
        "operation_id": operation_id,
        "resolved_by": resolved_by,
        "resolution": resolution,
        "lane_name": marker.get("lane_name", ""),
        "event_id": None if event is None else event["event_id"],
    }
